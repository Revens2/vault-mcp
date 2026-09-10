#!/usr/bin/env python3
"""Reconstruit l'index vectoriel et lexical a partir du miroir du vault.

Source : `VAULT_MCP_VAULT` (defaut `/srv/vault-mirror`), le miroir rclone du vault
Obsidian. C'est la seule source de verite ; CouchDB `vault_rag` est un snapshot mort
de juin 2026 et n'est plus lu.

Ce full reste la RECONCILIATION ultime du RAG : il est declenche une fois par jour
par `vault-reindex.timer` (03:30) et a la demande via l'intention `admin/reindex`.
L'indexation courante, elle, passe par `scripts/index_worker.py` (incremental).

Usage :
    reindex.py            reconstruit l'index
    reindex.py --check    ne reconstruit rien, compare l'index au vault et sort en
                          1 si l'ecart n'est pas nul
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import numpy as np

from vault_mcp.chunk import fragmenter
from vault_mcp.embed import vectoriser
from vault_mcp.index import (
    MetaFragment,
    VerrouOccupe,
    extraire_wikilinks,
    repertoire_index,
    sauvegarder,
    verrou_writers,
)
from vault_mcp.selection import EXCLUS, FICHIERS_EXCLUS, VAULT_DEFAUT, notes, repertoire_vault

APERCU_CARACTERES = 240
LOT = 128

# Attente du verrou writer. Un lot incremental dure quelques secondes : attendre
# est normal. Au-dela, quelque chose est coince et il vaut mieux echouer
# bruyamment (OnFailure=notify-failure@) que publier en concurrence.
ATTENTE_VERROU_S = float(os.environ.get("VAULT_MCP_ATTENTE_VERROU_S", "1800"))

__all__ = ["EXCLUS", "FICHIERS_EXCLUS", "VAULT_DEFAUT", "notes", "repertoire_vault"]


def construire(racine: Path) -> tuple[np.ndarray, list[MetaFragment], dict[str, list[str]]]:
    fichiers = notes(racine)
    textes: list[str] = []
    metas: list[MetaFragment] = []
    backlinks: dict[str, list[str]] = {}

    for fichier in fichiers:
        relatif = fichier.relative_to(racine).as_posix()
        try:
            contenu = fichier.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # Une note illisible ne doit pas interrompre une reindexation de 5 000
            # fichiers : on la signale et on continue.
            print(f"  ignoree ({type(exc).__name__}) : {relatif}", file=sys.stderr)
            continue

        for cible in extraire_wikilinks(contenu):
            backlinks.setdefault(cible, [])
            if relatif not in backlinks[cible]:
                backlinks[cible].append(relatif)

        for fragment in fragmenter(relatif, contenu):
            textes.append(fragment.texte)
            metas.append(
                MetaFragment(
                    chemin=fragment.chemin,
                    rang=fragment.rang,
                    titre=fragment.titre,
                    apercu=fragment.texte[:APERCU_CARACTERES].replace("\n", " "),
                )
            )

    if not textes:
        return np.empty((0, 384), dtype=np.float32), [], backlinks

    morceaux: list[np.ndarray] = []
    for debut in range(0, len(textes), LOT):
        morceaux.append(vectoriser(textes[debut : debut + LOT]))
        fait = min(debut + LOT, len(textes))
        print(f"  vectorises {fait}/{len(textes)}", end="\r", file=sys.stderr)
    print(file=sys.stderr)
    return np.vstack(morceaux), metas, backlinks


def verifier(racine: Path, repertoire: Path) -> int:
    from vault_mcp.index import Index

    index = Index(repertoire)
    if not index.disponible:
        print("index absent")
        return 1
    dans_vault = {f.relative_to(racine).as_posix() for f in notes(racine)}
    dans_index = {m.chemin for m in index.metas}
    manquantes = dans_vault - dans_index
    orphelines = dans_index - dans_vault
    print(f"notes dans le vault : {len(dans_vault)}")
    print(f"notes dans l'index  : {len(dans_index)}  ({len(index.metas)} fragments)")
    print(f"manquantes          : {len(manquantes)}")
    print(f"orphelines          : {len(orphelines)}")
    for chemin in sorted(manquantes)[:5]:
        print(f"  manquante : {chemin}")
    for chemin in sorted(orphelines)[:5]:
        print(f"  orpheline : {chemin}")
    return 0 if not manquantes and not orphelines else 1


def main() -> int:
    analyseur = argparse.ArgumentParser(description=__doc__)
    analyseur.add_argument("--check", action="store_true", help="verifier sans reconstruire")
    options = analyseur.parse_args()

    racine = repertoire_vault()
    repertoire = repertoire_index()
    if not racine.is_dir():
        print(f"vault introuvable : {racine}", file=sys.stderr)
        return 1

    if options.check:
        return verifier(racine, repertoire)

    debut = time.time()
    # Le verrou couvre la LECTURE du miroir autant que la publication. Le prendre
    # seulement au moment de publier laisserait le scenario que l'on veut fermer :
    # le full lit le miroir a T0, un lot incremental publie N+1 a T0+1 h, le full
    # publie a T0+2 h un etat calcule sur N -- l'incremental disparait en silence.
    try:
        with verrou_writers(repertoire, ATTENTE_VERROU_S):
            vecteurs, metas, backlinks = construire(racine)
            sauvegarder(repertoire, vecteurs, metas, backlinks)
    except VerrouOccupe:
        print(
            f"verrou writer non obtenu apres {ATTENTE_VERROU_S:.0f}s : full abandonne",
            file=sys.stderr,
        )
        return 1
    duree = time.time() - debut
    distinctes = len({m.chemin for m in metas})
    print(
        f"index reconstruit : {len(metas)} fragments sur {distinctes} notes, "
        f"{len(backlinks)} cibles de backlinks, {duree:.0f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
