"""Tests de la vectorisation.

Ces tests chargent le vrai modele : c'est volontaire. Un double rendrait le test
vert alors que la seule chose qui compte ici -- que le modele existe, sorte 384
dimensions et soit deterministe -- ne serait pas verifiee.
"""

from __future__ import annotations

import numpy as np
import pytest

from vault_mcp.embed import DIMENSIONS, normaliser, vectoriser, vectoriser_un

PHRASE = "Le serveur MCP expose la recherche vectorielle du vault Obsidian."


def test_dimension_attendue() -> None:
    assert vectoriser_un(PHRASE).shape == (DIMENSIONS,)


def test_vecteurs_normalises() -> None:
    v = vectoriser_un(PHRASE)
    assert float(np.linalg.norm(v)) == pytest.approx(1.0, abs=1e-5)


def test_deterministe() -> None:
    a, b = vectoriser_un(PHRASE), vectoriser_un(PHRASE)
    assert float(np.dot(a, b)) == pytest.approx(1.0, abs=1e-6)


def test_lot_et_unite_coherents() -> None:
    lot = vectoriser([PHRASE, "Autre texte sans rapport."])
    assert lot.shape == (2, DIMENSIONS)
    assert float(np.dot(lot[0], vectoriser_un(PHRASE))) == pytest.approx(1.0, abs=1e-6)


def test_liste_vide() -> None:
    assert vectoriser([]).shape == (0, DIMENSIONS)


def test_proximite_semantique_plus_forte_que_le_hasard() -> None:
    v = vectoriser(
        [
            "Comment configurer le pare-feu UFW sur le serveur",
            "Ouvrir un port dans le pare-feu du VPS",
            "Recette de la tarte aux pommes de ma grand-mere",
        ]
    )
    proche = float(np.dot(v[0], v[1]))
    lointain = float(np.dot(v[0], v[2]))
    # Si cette inegalite tombe, le modele charge n'est pas celui qu'on croit.
    assert proche > lointain


def test_normaliser_supporte_un_vecteur_nul() -> None:
    m = np.zeros((1, DIMENSIONS), dtype=np.float32)
    sortie = normaliser(m)
    # Sans garde, la division par zero produirait des NaN qui contaminent le tri.
    assert not np.isnan(sortie).any()
