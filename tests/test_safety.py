"""Tests de `vault_mcp.safety`.

Le module garde la frontiere du vault : chaque cas de rejet ici correspond a une
facon connue de sortir du perimetre ou de corrompre un `_id` CouchDB.
"""

from __future__ import annotations

import pytest

from vault_mcp.safety import (
    MAX_NOTE_BYTES,
    CheminInvalideError,
    normalize_path,
    verifier_taille,
)


@pytest.mark.parametrize(
    "chemin",
    [
        "../secrets.md",
        "a/../../etc/passwd.md",
        "wiki/../../../root/.ssh/id_rsa.md",
        "..",
        "wiki/..",
    ],
)
def test_rejette_la_remontee_hors_du_vault(chemin: str) -> None:
    with pytest.raises(CheminInvalideError):
        normalize_path(chemin)


@pytest.mark.parametrize("chemin", ["/etc/passwd.md", "/note.md"])
def test_rejette_les_chemins_absolus_posix(chemin: str) -> None:
    with pytest.raises(CheminInvalideError):
        normalize_path(chemin)


@pytest.mark.parametrize("chemin", [r"C:\note.md", "c:/note.md", r"\\serveur\part\note.md"])
def test_rejette_les_chemins_absolus_windows(chemin: str) -> None:
    with pytest.raises(CheminInvalideError):
        normalize_path(chemin)


def test_rejette_octet_nul() -> None:
    with pytest.raises(CheminInvalideError):
        normalize_path("wiki/note\x00.md")


@pytest.mark.parametrize("chemin", ["wiki/note.txt", "wiki/note", "wiki/note.MD.bak"])
def test_rejette_extension_non_md(chemin: str) -> None:
    with pytest.raises(CheminInvalideError):
        normalize_path(chemin)


def test_rejette_prefixe_couchdb_reserve() -> None:
    with pytest.raises(CheminInvalideError):
        normalize_path("_design/vues.md")


@pytest.mark.parametrize("chemin", ["", "   ", "."])
def test_rejette_chemin_vide(chemin: str) -> None:
    with pytest.raises(CheminInvalideError):
        normalize_path(chemin)


def test_accepte_un_chemin_normal() -> None:
    assert normalize_path("wiki/concepts/Portage.md").relatif == "wiki/concepts/Portage.md"


def test_normalise_les_backslash_et_les_segments_redondants() -> None:
    # Un client Windows envoie volontiers des backslash ; sans uniformisation ils
    # finiraient dans l'_id CouchDB et creeraient un doublon silencieux.
    assert normalize_path(r"wiki\concepts\.\Portage.md").relatif == "wiki/concepts/Portage.md"


def test_dossier_accepte_sans_extension_quand_demande() -> None:
    assert normalize_path("wiki/concepts", exiger_md=False).relatif == "wiki/concepts"


@pytest.mark.parametrize(
    ("chemin", "ecrivable"),
    [
        ("wiki/concepts/Portage.md", True),
        ("notes/idee.md", True),
        (".hermes/commands/lint.md", False),
        ("raw/assets/dump.md", True),  # ecrivable depuis le 2026-09-05 (adr/0023)
        (".obsidian/workspace.md", False),
        (".staging/en-cours.md", False),
    ],
)
def test_zones_interdites_en_ecriture(chemin: str, ecrivable: bool) -> None:
    assert normalize_path(chemin).est_ecrivable is ecrivable


def test_taille_acceptee() -> None:
    assert verifier_taille("bonjour") == 7


def test_taille_refusee_au_dela_du_plafond() -> None:
    with pytest.raises(CheminInvalideError):
        verifier_taille("a" * (MAX_NOTE_BYTES + 1))


def test_taille_comptee_en_octets_utf8_pas_en_caracteres() -> None:
    # 'e' accentue = 2 octets. Compter les caracteres laisserait passer une note
    # deux fois trop grosse en francais, et jusqu'a trois fois en CJK.
    assert verifier_taille("é") == 2
