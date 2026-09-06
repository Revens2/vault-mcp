#!/usr/bin/env python3
"""Execute le jeu de requetes de reference sur le nouveau moteur.

Sort un JSON de meme forme que la ligne de base de l'ancien moteur, pour que la
comparaison (tache 4.6) porte sur des listes de chemins comparables.

    parite.py <queries.json> <sortie.json> [mode]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from vault_mcp.index import Index


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    jeu = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    mode = sys.argv[3] if len(sys.argv) > 3 else "hybride"

    index = Index()
    if not index.disponible:
        print("index absent", file=sys.stderr)
        return 1

    moteurs = {
        "hybride": index.recherche_hybride,
        "vecteur": index.recherche_vectorielle,
        "lexical": index.recherche_lexicale,
    }
    moteur = moteurs[mode]

    resultats = []
    for requete in jeu["requetes"]:
        trouves = moteur(requete["texte"], 5)
        resultats.append(
            {
                "id": requete["id"],
                "famille": requete["famille"],
                "texte": requete["texte"],
                "top5": [
                    {"chemin": r.chemin, "score": round(r.score, 4), "titre": r.titre}
                    for r in trouves
                ],
            }
        )
        print(".", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)

    Path(sys.argv[2]).write_text(
        json.dumps(
            {
                "moteur": "vault-mcp v2",
                "modele": "all-MiniLM-L6-v2 fp32 (fastembed)",
                "un_vecteur_par_note": False,
                "mode": mode,
                "fragments_indexes": len(index.metas),
                "notes_indexees": len({m.chemin for m in index.metas}),
                "resultats": resultats,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"ecrit : {sys.argv[2]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
