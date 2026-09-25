"""Selection des notes indexables du miroir.

Extrait de `scripts/reindex.py` pour que le full ET le worker incremental
appliquent EXACTEMENT les memes regles. Deux listes d'exclusion divergentes
produiraient un worker qui reindexe en boucle des notes que le full retire --
et un `reindex.py --check` qui ne converge jamais.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

VAULT_DEFAUT = Path("/srv/vault-mirror")

# Repertoires sans valeur semantique : etat local d Obsidian, zones de travail
# d outils, et les journaux LiveSync (jusqu a 1,3 Mo piece de bruit pur).
EXCLUS = (
    ".trash-mcp/",
    ".obsidian/",
    ".trash/",
    # Corbeille de la publication Wiki : 2 065 fragments indexes au 2026-09-14, qui
    # remontaient devant les fiches vivantes (`entities/VPS Étude.md` supprimee).
    ".trash-wiki-publish/",
    ".temp/",
    ".git/",
    ".staging/",
    # Outillage, pas de la connaissance. Indexes par erreur au premier passage :
    # 5 185 fragments (11,5 % de l index) de configuration d agents et de commandes.
    ".claude/",
    ".hermes/",
    ".mdinbox/",
)

# Fichiers generes ou agregats : leur contenu est deja indexe ailleurs, et leur
# taille leur donne un poids sans rapport avec leur valeur (index.md = 152 fragments).
FICHIERS_EXCLUS = ("index.md", "log.md")

# Transcripts produits par les evaluations RAG elles-memes (bancs e2e qui posent
# une question golden a un modele avec un CONTEXTE injecte), re-exportes par ConvIA.
# 267 notes au 2026-09-25, souvent en 4-8 copies quasi identiques : elles
# remontaient en tete (audit 2026-09-25, n23) et contaminaient les etiquettes.
# Seul le nom est teste : `indexable` ne voit que le chemin.
EVAL_RAG = re.compile(r"^raw/assets/ConvIA/[^/]+/\d{4}-\d{2}-\d{2}_(?:rag-v2-answer-eval|question)-")


def repertoire_vault() -> Path:
    return Path(os.environ.get("VAULT_MCP_VAULT", str(VAULT_DEFAUT)))


def indexable(relatif: str) -> bool:
    """`relatif` : chemin POSIX relatif a la racine du miroir.

    Le test `.md` n'est pas redondant avec `notes()` : le worker incremental
    recoit des chemins venus du spool d'ecriture, qui accepte aussi des fichiers
    non-markdown. Sans ce test, une piece jointe deposee par le MCP serait
    vectorisee alors que le full ne la voit pas -- et `reindex.py --check` la
    signalerait comme orpheline a chaque passage.
    """
    nom = relatif.rsplit("/", 1)[-1]
    return not (
        not relatif.endswith(".md")
        or relatif.startswith(EXCLUS)
        or nom.startswith("livesync_log_")
        or relatif in FICHIERS_EXCLUS
        or EVAL_RAG.match(relatif) is not None
    )


def notes(racine: Path) -> list[Path]:
    return [
        chemin
        for chemin in sorted(racine.rglob("*.md"))
        if indexable(chemin.relative_to(racine).as_posix())
    ]


__all__ = [
    "EVAL_RAG",
    "EXCLUS",
    "FICHIERS_EXCLUS",
    "VAULT_DEFAUT",
    "indexable",
    "notes",
    "repertoire_vault",
]
