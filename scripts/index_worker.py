#!/usr/bin/env python3
"""Worker d'indexation incrementale du vault.

Remplace le comportement pathologique « une ecriture de note -> un FULL rebuild
de 165 000 fragments » (mesure : 2,7 full/jour, 2 h 10 chacun, 2 vCPU satures).

Ce qu'il fait, a chaque declenchement :
  1. recupere les lots `inflight` orphelins (crash d'un passage precedent) ;
  2. en mode `--reconcilier`, compare le miroir a l'index publie et salit les
     ecarts (notes arrivees par `rclone sync`, donc SANS intention de spool) ;
  3. reclame un lot borne de chemins sales ;
  4. lit l'etat COURANT de chaque chemin dans le miroir -- absent = suppression ;
  5. publie UNE seule generation d'index pour tout le lot ;
  6. acquitte le lot seulement apres cette publication.

Il ne tourne PAS dans `vault-mcp.service` : deprioriser le service exposerait
aussi les recherches interactives. Il a son unite, avec `Nice`/`CPUWeight`/
`IOSchedulingClass=idle` a lui.

Il ne publie JAMAIS en concurrence du full : il fait la queue derriere le verrou
writer, et ne lit l'etat des notes qu'une fois ce verrou obtenu. C'est ce qui rend
impossible « l'incremental publie N+1 puis le full republie N », et c'est aussi
pourquoi il attend au lieu de rendre la main -- sinon le `.path` unit le
redeclencherait en boucle pendant les deux heures du full.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from vault_mcp import dirty
from vault_mcp.index import IGNORER, Index, VerrouOccupe
from vault_mcp.selection import indexable, notes, repertoire_vault

# Fenetre de coalescence : le .path unit se declenche des la premiere intention,
# une rafale d'ecritures arrive dans les secondes qui suivent. Attendre un peu
# transforme dix publications de 320 Mo en une seule.
COALESCENCE_S = float(os.environ.get("VAULT_MCP_COALESCENCE_S", "6"))

# Borne des ecarts qu'une reconciliation peut salir d'un coup. Au-dela, ce n'est
# plus un ecart, c'est un index a reconstruire : le full quotidien s'en charge.
RECONCILIATION_MAXIMUM = int(os.environ.get("VAULT_MCP_RECONCILIATION_MAX", "2000"))

# Attente maximale du verrou writer. Doit couvrir la duree d'un full (~2 h 10
# mesure) : le worker fait la queue derriere lui plutot que de rendre la main a un
# `.path` unit qui le redeclencherait aussitot.
ATTENTE_VERROU_S = float(os.environ.get("VAULT_MCP_ATTENTE_VERROU_S", "10800"))


def journal(*message: object) -> None:
    print(*message, file=sys.stderr, flush=True)


def ecarts_miroir(index: Index, seuil_ns: int) -> set[str]:
    """Chemins ou le miroir et l'index publie divergent.

    Trois sources d'ecart :
      - note presente dans le miroir, absente de l'index (creation externe) ;
      - note presente dans l'index, absente du miroir (suppression externe) ;
      - note dont le fichier a ete ECRIT depuis le dernier parcours complet.

    Le troisieme test porte sur `st_ctime_ns`, pas `st_mtime_ns` : `rclone`
    recopie la date de modification de Drive, donc une note editee sur telephone
    a 10 h et synchronisee a 12 h garde un mtime de 10 h -- anterieur a un index
    publie a 11 h, et l'edition passerait inapercue. Le ctime, lui, est pose par
    le noyau au moment ou rclone ecrit le fichier ici.

    La reference est le seuil du DERNIER PARCOURS COMPLET, jamais la date de
    l'index publie. Le full lit le miroir pendant deux heures pendant que rclone
    continue d'y ecrire : une note lue par le full a t1, modifiee a t2, publiee a
    t3 (t1 < t2 < t3) passerait sous le radar d'une comparaison a t3.
    """
    racine = repertoire_vault()
    if not racine.is_dir():
        journal(f"miroir introuvable : {racine}")
        return set()

    dans_miroir: dict[str, int] = {}
    for fichier in notes(racine):
        relatif = fichier.relative_to(racine).as_posix()
        try:
            etat = fichier.stat()
        except OSError:
            continue
        dans_miroir[relatif] = max(etat.st_ctime_ns, etat.st_mtime_ns)

    dans_index = {meta.chemin for meta in index.metas}

    ecarts: set[str] = set()
    # Creations : dans le miroir, pas dans l'index. `notes()` a deja applique la
    # selection, tout ce qui sort de la est indexable.
    ecarts |= set(dans_miroir) - dans_index
    # Suppressions -- et residus d'une ancienne regle de selection : les salir les
    # fait retirer proprement au prochain lot (le worker lira « absent »).
    ecarts |= dans_index - set(dans_miroir)
    # Modifications arrivees depuis le dernier parcours complet. Au tout premier
    # passage il n'y a pas de seuil : on ne salit alors que les ecarts d'ensemble,
    # sinon le premier parcours declarerait les 8 500 notes modifiees.
    if seuil_ns:
        # `>=` et non `>` : un fichier dont le ctime tombe exactement sur le seuil
        # serait sinon invisible. Le cout d'un faux positif est un lot de plus,
        # celui d'un faux negatif est une note perimee jusqu'au full.
        ecarts |= {c for c, date in dans_miroir.items() if date >= seuil_ns}
    return ecarts


def lecteur_miroir(chemin: str) -> str | None | object:
    """Etat COURANT d'un chemin dans le miroir.

    `None` = absent = a retirer de l'index. `IGNORER` = illisible, on ne touche pas.
    Confondre les deux effacerait une note valide sur une erreur d'E/S transitoire.

    Cette lecture est appelee par `reindexer_chemins` SOUS le verrou writer : lire
    avant d'attendre le verrou republierait, apres un full de 2 h, un etat vieux de
    deux heures par-dessus le travail du full.
    """
    if not indexable(chemin):
        # Devenu non indexable (regle de selection changee) : on le retire.
        return None
    fichier = repertoire_vault() / chemin
    try:
        return fichier.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        journal(f"  ignore ({type(exc).__name__}) : {chemin}")
        return IGNORER


def main() -> int:
    analyseur = argparse.ArgumentParser(description=__doc__)
    analyseur.add_argument(
        "--reconcilier",
        action="store_true",
        help="comparer le miroir a l'index publie et salir les ecarts avant de vider la file",
    )
    analyseur.add_argument(
        "--sans-coalescence", action="store_true", help="ne pas attendre la fenetre de coalescence"
    )
    analyseur.add_argument("--lot", type=int, default=dirty.LOT_MAXIMUM)
    options = analyseur.parse_args()

    index = Index()
    if not index.disponible:
        journal("index absent, rien a faire (lancer scripts/reindex.py)")
        return 0

    remis = dirty.recuperer()
    if remis:
        journal(f"lot inflight orphelin recupere : {remis} chemin(s)")

    if options.reconcilier:
        # L'horodatage est pris AVANT le parcours : tout ce qui change pendant le
        # parcours sera vu au parcours suivant, jamais oublie.
        debut_parcours = time.time_ns()
        repris = dirty.reprendre_differes()
        if repris:
            journal(f"chemins differes repris : {repris}")
        seuil = dirty.seuil_reconciliation()
        if seuil > debut_parcours:
            # L'horloge a recule (correction NTP, restauration). Un seuil dans le
            # futur masquerait toute modification jusqu'a ce que l'horloge le
            # rattrape : on le desarme et on repart d'une comparaison d'ensembles.
            journal(f"seuil dans le futur ({seuil} > {debut_parcours}), desarme")
            seuil = 0
        try:
            ecarts = ecarts_miroir(index, seuil)
        except OSError as exc:
            journal(f"reconciliation impossible : {exc}")
            ecarts = None
        if ecarts is None:
            pass
        elif len(ecarts) > RECONCILIATION_MAXIMUM:
            # Le seuil N'AVANCE PAS : un parcours tronque qui l'avancerait
            # masquerait definitivement ce qu'il n'a pas mis en file.
            journal(
                f"{len(ecarts)} ecarts miroir/index : au-dela du seuil "
                f"({RECONCILIATION_MAXIMUM}), laisse au full quotidien"
            )
        else:
            if ecarts:
                dirty.salir(sorted(ecarts))
                journal(f"reconciliation : {len(ecarts)} chemin(s) salis")
            dirty.poser_seuil_reconciliation(debut_parcours)

    if dirty.taille() == 0:
        return 0

    if not options.sans_coalescence and COALESCENCE_S > 0:
        time.sleep(COALESCENCE_S)

    jeton, reclames = dirty.reclamer(options.lot)
    if not jeton:
        return 0
    restants = dirty.taille()
    journal(f"lot {jeton} : {len(reclames)} chemin(s), {restants} encore en file")

    debut = time.time()
    try:
        # On ATTEND le verrou au lieu de rendre la main : sans cela, le `.path`
        # unit redeclencherait le worker en boucle pendant les 2 h du full.
        # L'attente est sans risque puisque les contenus sont lus SOUS verrou.
        resultat = index.reindexer_chemins(
            sorted(reclames), lecteur_miroir, attente_verrou_s=ATTENTE_VERROU_S
        )
    except VerrouOccupe:
        rendus = dirty.rendre(jeton)
        journal(
            f"verrou writer toujours occupe apres {ATTENTE_VERROU_S:.0f}s : "
            f"{rendus} chemin(s) laisses en file"
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - le lot doit survivre a toute erreur
        rendus = dirty.rendre(jeton)
        journal(f"echec {type(exc).__name__}: {exc} -- {rendus} chemin(s) laisses en file")
        return 1

    dirty.acquitter(jeton)
    ignores = resultat.get("ignores") or []
    if ignores:
        # Acquitter un chemin qu'on n'a pas su lire perdrait sa modification
        # jusqu'au full quotidien. Il part dans `differe/`, repris par la
        # prochaine reconciliation -- pas dans `queue/`, surveille par le `.path`
        # unit, ou une note durablement illisible ferait boucler le worker.
        dirty.differer(ignores)
        journal(f"{len(ignores)} chemin(s) illisibles differes, non acquittes")
    if resultat["etat"] == "sans_objet":
        journal("lot sans effet (aucune note lisible)")
        return 0
    journal(
        f"publie : {resultat['notes']} note(s) dont {resultat['notes_supprimees']} retiree(s), "
        f"{resultat['fragments_apres']} fragments, {time.time() - debut:.1f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
