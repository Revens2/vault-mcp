"""L'index doit se relire quand la reindexation en ecrit un nouveau.

Sans cela, le serveur en cours d'execution sert l'ancien index indefiniment : la
reindexation nocturne n'aurait aucun effet visible jusqu'au prochain redemarrage.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np

from vault_mcp.embed import DIMENSIONS
from vault_mcp.index import Index, MetaFragment, sauvegarder


def _ecrire(repertoire: Path, chemins: list[str]) -> None:
    metas = [MetaFragment(chemin=c, rang=0, titre=c, apercu=c) for c in chemins]
    vecteurs = np.zeros((len(chemins), DIMENSIONS), dtype=np.float32)
    vecteurs[:, 0] = 1.0
    sauvegarder(repertoire, vecteurs, metas, {})


def test_relit_lindex_apres_reindexation(tmp_path: Path) -> None:
    _ecrire(tmp_path, ["avant.md"])
    index = Index(tmp_path)
    assert [m.chemin for m in index.metas] == ["avant.md"]

    _ecrire(tmp_path, ["apres.md", "autre.md"])
    # `sauvegarder` peut s'executer dans la meme seconde que la lecture precedente :
    # on force un mtime distinct pour que le test mesure la logique, pas la resolution
    # de l'horloge du systeme de fichiers.
    cible = tmp_path / "vectors.npy"
    os.utime(cible, (cible.stat().st_atime, cible.stat().st_mtime + 10))

    assert [m.chemin for m in index.metas] == ["apres.md", "autre.md"]
    assert index.vecteurs.shape[0] == 2


def test_ne_relit_pas_si_rien_na_change(tmp_path: Path) -> None:
    _ecrire(tmp_path, ["stable.md"])
    index = Index(tmp_path)
    premier = index.metas
    # Meme objet en memoire : aucune relecture inutile a chaque requete.
    assert index.metas is premier


def test_index_absent_reste_inoffensif(tmp_path: Path) -> None:
    index = Index(tmp_path)
    assert index.disponible is False
    assert index.backlinks == {}
