"""Tests du masquage des secrets en sortie.

Cas reel a l'origine du module : le mot de passe CouchDB present en clair dans trois
notes du vault, indexees et interrogeables depuis internet.
"""

from __future__ import annotations

import pytest

from vault_mcp.secrets import LONGUEUR_MIN, REMPLACEMENT, _valeurs, masquer

MOTDEPASSE = "MotDePasseCouchTresLong123"
URL = f"http://nexususer:{MOTDEPASSE}@192.0.2.9:5984"
JETON = "a" * 64


def test_extrait_le_mot_de_passe_dune_url() -> None:
    valeurs = _valeurs({"COUCH_URL": URL})
    assert MOTDEPASSE in valeurs
    assert URL in valeurs


def test_les_plus_longues_valeurs_dabord() -> None:
    # Masquer le mot de passe avant l'URL laisserait `http://user:<masque>@192.0.2.9`
    # dans le texte : l'hote et l'utilisateur resteraient lisibles.
    valeurs = _valeurs({"COUCH_URL": URL})
    assert list(valeurs) == sorted(valeurs, key=len, reverse=True)


def test_masque_un_mot_de_passe_recopie_dans_une_note() -> None:
    note = f"Pour se connecter : COUCH_URL avec le mot de passe {MOTDEPASSE} puis relancer."
    sortie = masquer(note, _valeurs({"COUCH_URL": URL}))
    assert MOTDEPASSE not in sortie
    assert REMPLACEMENT in sortie


def test_masque_une_url_complete() -> None:
    note = f"export COUCH_URL={URL}"
    sortie = masquer(note, _valeurs({"COUCH_URL": URL}))
    assert MOTDEPASSE not in sortie
    assert "192.0.2.9" not in sortie


def test_masque_le_jeton_bearer() -> None:
    note = f"Authorization: Bearer {JETON}"
    sortie = masquer(note, _valeurs({"VAULT_MCP_TOKEN": JETON}))
    assert JETON not in sortie


def test_ignore_les_valeurs_trop_courtes() -> None:
    # Masquer une valeur de 4 caracteres mutilerait le texte a chaque occurrence
    # fortuite. En dessous du seuil, on ne masque pas -- et on ne pretend pas le faire.
    court = "a" * (LONGUEUR_MIN - 1)
    assert _valeurs({"MCP_SECRET": court}) == ()
    assert masquer(f"texte {court} suite", ()) == f"texte {court} suite"


def test_environnement_vide_ne_masque_rien() -> None:
    assert _valeurs({}) == ()
    assert masquer("texte quelconque", ()) == "texte quelconque"


def test_texte_vide() -> None:
    assert masquer("", _valeurs({"COUCH_URL": URL})) == ""


@pytest.mark.parametrize("valeur", ["", "   ", "pas-une-url"])
def test_valeurs_degenerees(valeur: str) -> None:
    # Ne doit ni lever ni produire de faux positif exploitable.
    resultat = _valeurs({"COUCH_URL": valeur})
    assert all(len(v) >= LONGUEUR_MIN for v in resultat)
