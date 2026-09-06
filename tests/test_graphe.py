"""Tests de l'exploration du graphe (backlinks et liens sortants)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from vault_mcp.embed import DIMENSIONS
from vault_mcp.index import Index, MetaFragment, sauvegarder

NOTES = ["wiki/concepts/Docker.md", "wiki/entities/Nginx.md", "raw/assets/notes.md"]

# `notes.md` cite Docker et une note qui n'existe pas ; `Nginx.md` cite Docker.
BACKLINKS = {
    "Docker": ["raw/assets/notes.md", "wiki/entities/Nginx.md"],
    "Kubernetes": ["raw/assets/notes.md"],
}


@pytest.fixture(scope="module")
def index(tmp_path_factory: pytest.TempPathFactory) -> Index:
    repertoire = tmp_path_factory.mktemp("graphe")
    metas = [MetaFragment(chemin=c, rang=0, titre=c, apercu=c) for c in NOTES]
    vecteurs = np.zeros((len(NOTES), DIMENSIONS), dtype=np.float32)
    vecteurs[:, 0] = 1.0
    sauvegarder(repertoire, vecteurs, metas, BACKLINKS)
    return Index(repertoire)


def test_backlinks_dune_note_citee(index: Index) -> None:
    contexte = index.contexte_graphe("wiki/concepts/Docker.md")
    entrants: list[str] = contexte["backlinks"]  # type: ignore[assignment]
    assert set(entrants) == {"raw/assets/notes.md", "wiki/entities/Nginx.md"}
    assert contexte["nb_backlinks"] == 2


def test_liens_sortants_resolus_et_non_resolus(index: Index) -> None:
    contexte = index.contexte_graphe("raw/assets/notes.md")
    sortants: list[dict[str, Any]] = contexte["liens_sortants"]  # type: ignore[assignment]
    par_cible = {s["cible"]: s for s in sortants}
    assert par_cible["Docker"]["resolu"] is True
    assert par_cible["Docker"]["chemins"] == ["wiki/concepts/Docker.md"]
    # Un wikilink vers une note inexistante est courant dans Obsidian : le signaler
    # vaut mieux que le taire, c'est souvent une note a ecrire.
    assert par_cible["Kubernetes"]["resolu"] is False
    assert par_cible["Kubernetes"]["chemins"] == []


def test_note_citante_mais_non_citee(index: Index) -> None:
    # Nginx.md cite Docker mais personne ne la cite : sortants non vide, entrants vide.
    contexte = index.contexte_graphe("wiki/entities/Nginx.md")
    assert contexte["backlinks"] == []
    sortants: list[dict[str, Any]] = contexte["liens_sortants"]  # type: ignore[assignment]
    assert [s["cible"] for s in sortants] == ["Docker"]


def test_note_inexistante_est_signalee(index: Index) -> None:
    contexte = index.contexte_graphe("wiki/entities/Inconnue.md")
    assert contexte["existe"] is False
    assert contexte["backlinks"] == []


def test_une_note_ne_se_cite_pas_elle_meme(tmp_path: Path) -> None:
    metas = [MetaFragment(chemin="a/Docker.md", rang=0, titre="", apercu="")]
    vecteurs = np.zeros((1, DIMENSIONS), dtype=np.float32)
    vecteurs[:, 0] = 1.0
    sauvegarder(tmp_path, vecteurs, metas, {"Docker": ["a/Docker.md"]})
    contexte = Index(tmp_path).contexte_graphe("a/Docker.md")
    # Une note qui contient `[[Docker]]` et s'appelle Docker.md s'auto-citerait :
    # le backlink serait vrai mais sans aucune valeur pour le lecteur.
    assert contexte["backlinks"] == []


def test_resolution_dun_nom_partage(tmp_path: Path) -> None:
    chemins = ["wiki/entities/Docker.md", "raw/assets/Docker.md"]
    metas = [MetaFragment(chemin=c, rang=0, titre="", apercu="") for c in chemins]
    vecteurs = np.zeros((2, DIMENSIONS), dtype=np.float32)
    vecteurs[:, 0] = 1.0
    sauvegarder(tmp_path, vecteurs, metas, {"Docker": ["autre.md"]})
    index = Index(tmp_path)
    # Deux notes portent le meme nom : un wikilink `[[Docker]]` est ambigu, on renvoie
    # les deux plutot que d'en choisir une au hasard.
    assert sorted(index.noms_vers_chemins["Docker"]) == sorted(chemins)
