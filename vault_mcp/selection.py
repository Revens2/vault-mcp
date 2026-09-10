"""Selection des notes indexables du miroir.

Extrait de `scripts/reindex.py` pour que le full ET le worker incremental
appliquent EXACTEMENT les memes regles. Deux listes d'exclusion divergentes
produiraient un worker qui reindexe en boucle des notes que le full retire --
et un `reindex.py --check` qui ne converge jamais.
"""

from __future__ import annotations

import os
from pathlib import Path

VAULT_DEFAUT = Path("/srv/vault-mirror")

# Repertoires sans valeur semantique : etat local d Obsidian, zones de travail
# d outils, et les journaux LiveSync (jusqu a 1,3 Mo piece de bruit pur).
EXCLUS = (
    ".trash-mcp/",
    ".obsidian/",
    ".trash/",
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
    )


def notes(racine: Path) -> list[Path]:
    return [
        chemin
        for chemin in sorted(racine.rglob("*.md"))
        if indexable(chemin.relative_to(racine).as_posix())
    ]


__all__ = [
    "EXCLUS",
    "FICHIERS_EXCLUS",
    "VAULT_DEFAUT",
    "indexable",
    "notes",
    "repertoire_vault",
]
