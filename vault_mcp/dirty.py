"""File durable des chemins de notes a reindexer (« dirty paths »).

POURQUOI UNE FILE SUR DISQUE
----------------------------
Avant cette mission, une ecriture de note armait un timer transitoire qui lancait
un FULL rebuild de 165 000 fragments (2 h, 2 vCPU). Le remplacement incremental a
besoin d'un etat durable : une modification ne doit pas disparaitre parce que le
worker etait arrete, parce qu'un full de 2 h tenait le verrou, ou parce que la
machine a redemarre.

CONTRAT
-------
- Une entree = UN chemin de note, nomme par `sha256(chemin)` : la deduplication est
  donc structurelle, dix ecritures de la meme note donnent une entree.
- L'entree ne porte PAS l'operation. Le worker lit l'etat courant du miroir au
  moment ou il traite : absent = suppression, present = derniere version. C'est
  exactement le last-write-wins voulu, et cela evite de rejouer des versions
  intermediaires.
- Reclamation par DEPLACEMENT vers `inflight/<jeton>/`, pas par lecture. Une
  ecriture concurrente recree alors `queue/<sha>` pendant que le worker travaille,
  et l'acquittement ne detruit que le lot reclame : la nouvelle version n'est
  jamais perdue par l'acquittement de l'ancienne.
- Acquittement APRES publication coherente seulement. En cas d'echec, le lot
  retourne dans `queue/`.
- `recuperer()` au demarrage : un lot `inflight` orphelin (crash, kill) retourne
  dans la file. Rejeu idempotent, jamais de perte.

La file vit dans le spool (`/srv/vault-spool/dirty`) parce que c'est le seul
repertoire partage en ecriture entre `juliann` (le pousseur, en bash) et
`juliann-app` (le MCP et le worker). Les repertoires sont setgid `vault-spool`.
"""

from __future__ import annotations

import hashlib
import os
import time
import uuid
from pathlib import Path

RACINE_DEFAUT = Path("/srv/vault-spool/dirty")

# Borne d'un lot. Chaque lot republie l'index complet (~320 Mo) : au-dela, on ne
# gagne plus rien a agrandir, et on allonge la fenetre pendant laquelle le verrou
# writer est tenu.
LOT_MAXIMUM = 400


def racine() -> Path:
    return Path(os.environ.get("VAULT_MCP_DIRTY", str(RACINE_DEFAUT)))


def repertoires() -> tuple[Path, Path, Path]:
    base = racine()
    return base / "queue", base / "inflight", base / "differe"


def _nom(chemin: str) -> str:
    """Meme derivation que la fonction `_nom_sale` de `vault_spool_push.sh`."""
    return hashlib.sha256(chemin.encode("utf-8")).hexdigest()[:32] + ".path"


def _fsync_repertoire(chemin: Path) -> None:
    descripteur = os.open(chemin, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(descripteur)
    finally:
        os.close(descripteur)


def salir(chemins: list[str] | tuple[str, ...] | set[str]) -> int:
    """Marque des chemins comme a reindexer. Idempotent et durable.

    Ecriture -> fsync -> rename -> fsync du repertoire : sans ces deux fsync, une
    coupure d'alimentation juste apres l'acquittement de l'ecriture de la note
    ferait disparaitre l'entree alors que la note, elle, est sur Drive. La note
    resterait alors non indexee jusqu'au full quotidien.

    Le temporaire porte un prefixe `.tmp.` : le worker ne reclame que `*.path`,
    il ne peut donc pas s'emparer d'une entree a moitie ecrite.
    """
    file_attente, _, _ = repertoires()
    file_attente.mkdir(parents=True, exist_ok=True)
    ecrits = 0
    for chemin in chemins:
        if not chemin:
            continue
        cible = file_attente / _nom(chemin)
        temporaire = file_attente / f".tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}"
        with temporaire.open("w", encoding="utf-8") as flux:
            flux.write(chemin + "\n")
            flux.flush()
            os.fsync(flux.fileno())
        temporaire.replace(cible)
        ecrits += 1
    if ecrits:
        _fsync_repertoire(file_attente)
    return ecrits


def seuil_reconciliation() -> int:
    """Horodatage (ns) du dernier parcours de reconciliation COMPLET, ou 0."""
    fichier = racine() / "reconcile.seuil"
    try:
        return int(fichier.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def poser_seuil_reconciliation(horodatage_ns: int) -> None:
    """Avance le seuil. A n'appeler QU'APRES un parcours complet dont TOUS les
    ecarts ont ete mis en file : un parcours tronque qui avancerait le seuil
    masquerait definitivement les changements qu'il n'a pas traites."""
    base = racine()
    base.mkdir(parents=True, exist_ok=True)
    fichier = base / "reconcile.seuil"
    temporaire = base / f".seuil.tmp.{os.getpid()}"
    with temporaire.open("w", encoding="utf-8") as flux:
        flux.write(str(horodatage_ns))
        flux.flush()
        os.fsync(flux.fileno())
    temporaire.replace(fichier)
    _fsync_repertoire(base)


def taille() -> int:
    file_attente, _, _ = repertoires()
    if not file_attente.is_dir():
        return 0
    return sum(1 for f in file_attente.glob("*.path"))


def en_attente() -> set[str]:
    """Chemins sales pas encore publies : file, lots en vol et differes. Lecture seule."""
    file_attente, vol, differe = repertoires()
    chemins: set[str] = set()
    for entree in (*file_attente.glob("*.path"), *vol.glob("*/*.path"), *differe.glob("*.path")):
        try:
            chemin = entree.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if chemin:
            chemins.add(chemin)
    return chemins


def recuperer() -> int:
    """Remet dans la file les lots `inflight` orphelins (crash du worker)."""
    file_attente, vol, _ = repertoires()
    if not vol.is_dir():
        return 0
    file_attente.mkdir(parents=True, exist_ok=True)
    remis = 0
    for lot in sorted(vol.iterdir()):
        if not lot.is_dir():
            continue
        for entree in lot.glob("*.path"):
            # `replace` et non `rename` : si une ecriture concurrente a deja
            # recree l'entree, les deux portent le meme chemin, l'ecraser est sans
            # consequence.
            entree.replace(file_attente / entree.name)
            remis += 1
        try:
            lot.rmdir()
        except OSError:
            pass
    return remis


def reclamer(maximum: int = LOT_MAXIMUM) -> tuple[str, dict[str, Path]]:
    """Reserve jusqu'a `maximum` chemins en les DEPLACANT hors de la file.

    Renvoie `(jeton_du_lot, {chemin: fichier_inflight})`. Lot vide -> ("", {}).
    """
    file_attente, vol, _ = repertoires()
    if not file_attente.is_dir():
        return "", {}
    candidates = sorted(file_attente.glob("*.path"))[:maximum]
    if not candidates:
        return "", {}

    jeton = f"{int(time.time())}.{os.getpid()}.{uuid.uuid4().hex[:8]}"
    lot = vol / jeton
    lot.mkdir(parents=True, exist_ok=True)

    reclames: dict[str, Path] = {}
    for entree in candidates:
        try:
            chemin = entree.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not chemin:
            entree.unlink(missing_ok=True)
            continue
        destination = lot / entree.name
        try:
            entree.replace(destination)
        except OSError:
            continue
        reclames[chemin] = destination
    if not reclames:
        try:
            lot.rmdir()
        except OSError:
            pass
        return "", {}
    return jeton, reclames


def acquitter(jeton: str) -> None:
    """Detruit le lot reclame. A n'appeler qu'APRES une publication coherente."""
    if not jeton:
        return
    _, vol, _ = repertoires()
    lot = vol / jeton
    if not lot.is_dir():
        return
    for entree in lot.iterdir():
        entree.unlink(missing_ok=True)
    try:
        lot.rmdir()
    except OSError:
        pass


def differer(chemins: list[str] | tuple[str, ...] | set[str]) -> int:
    """Range des chemins que le worker n'a PAS pu traiter (note illisible).

    Ils ne sont ni acquittes -- la modification serait perdue jusqu'au full
    quotidien -- ni remis dans `queue/`, qui est surveille par un `.path` unit :
    une note durablement illisible y ferait tourner le worker en boucle. Ils sont
    repris par `reprendre_differes()`, appele par la passe de reconciliation, donc
    au plus une fois toutes les dix minutes.
    """
    _, _, differe = repertoires()
    differe.mkdir(parents=True, exist_ok=True)
    ranges = 0
    for chemin in chemins:
        if not chemin:
            continue
        (differe / _nom(chemin)).write_text(chemin + "\n", encoding="utf-8")
        ranges += 1
    return ranges


def reprendre_differes() -> int:
    """Remet les chemins differes dans la file. Appele par la reconciliation."""
    file_attente, _, differe = repertoires()
    if not differe.is_dir():
        return 0
    file_attente.mkdir(parents=True, exist_ok=True)
    repris = 0
    for entree in differe.glob("*.path"):
        entree.replace(file_attente / entree.name)
        repris += 1
    return repris


def rendre(jeton: str) -> int:
    """Remet le lot dans la file (echec de traitement). Rejouable."""
    if not jeton:
        return 0
    file_attente, vol, _ = repertoires()
    lot = vol / jeton
    if not lot.is_dir():
        return 0
    file_attente.mkdir(parents=True, exist_ok=True)
    remis = 0
    for entree in lot.glob("*.path"):
        entree.replace(file_attente / entree.name)
        remis += 1
    try:
        lot.rmdir()
    except OSError:
        pass
    return remis


__all__ = [
    "LOT_MAXIMUM",
    "acquitter",
    "differer",
    "reprendre_differes",
    "poser_seuil_reconciliation",
    "seuil_reconciliation",
    "racine",
    "reclamer",
    "recuperer",
    "rendre",
    "salir",
    "taille",
]
