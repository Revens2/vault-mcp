"""Tests de `vault_mcp.safety.valider_ecriture` (adr/0020).

L ecriture est une frontiere plus grave que la lecture : une lecture non
autorisee expose le vault, une ecriture non autorisee injecte du contenu qui
sera relu comme contexte de confiance par tous les agents branches dessus.
Chaque cas de rejet ici correspond a une facon connue de faire ecrire le
service hors de son perimetre, ou d ecrire dans un endroit ou la note serait
invisible.
"""

from __future__ import annotations

import pytest

from vault_mcp.safety import (
    FICHIERS_NON_INDEXES,
    MAX_NOTE_BYTES,
    PREFIXES_EXCLUS_ECRITURE,
    PREFIXES_EXCLUS_LECTURE,
    PREFIXES_INTERDITS_EN_ECRITURE,
    CheminInvalideError,
    valider_ecriture,
    verifier_taille,
)


# --- Prefixes interdits ----------------------------------------------------
# Le test itere sur la CONSTANTE et non sur une copie litterale : une entree
# ajoutee a la liste sans test correspondant est le mode de derive classique.
@pytest.mark.parametrize("prefixe", PREFIXES_EXCLUS_ECRITURE)
def test_rejette_chaque_prefixe_exclu(prefixe: str) -> None:
    with pytest.raises(CheminInvalideError):
        valider_ecriture(f"{prefixe}note.md")


def test_l_union_couvre_bien_les_deux_listes() -> None:
    # `.claude/` n est que dans les exclusions de lecture. Si l union se met a
    # en perdre une, ce test tombe.
    assert set(PREFIXES_INTERDITS_EN_ECRITURE) <= set(PREFIXES_EXCLUS_ECRITURE)
    assert set(PREFIXES_EXCLUS_LECTURE) <= set(PREFIXES_EXCLUS_ECRITURE)
    assert ".claude/" in PREFIXES_EXCLUS_ECRITURE
    assert ".trash-mcp/" in PREFIXES_EXCLUS_ECRITURE


def test_raw_est_ecrivable_depuis_2026_09_05() -> None:
    # Decision explicite (adr/0023) : `raw/` est sorti des interdits d ecriture
    # pour que le MCP puisse y deposer des rapports. Ce test est la borne : s il
    # tombe, c est que la decision a ete annulee sans mettre a jour l ADR.
    assert "raw/" not in PREFIXES_INTERDITS_EN_ECRITURE
    assert "raw/" not in PREFIXES_EXCLUS_ECRITURE
    assert valider_ecriture("raw/assets/rapport.md").relatif == "raw/assets/rapport.md"


def test_raw_reste_lisible() -> None:
    # Regression a surveiller : faire importer l UNION par `mirror_store`
    # rendrait `raw/` illisible, ce qui n a jamais ete demande.
    assert "raw/" not in PREFIXES_EXCLUS_LECTURE


# --- Sortie du vault -------------------------------------------------------
@pytest.mark.parametrize(
    "chemin",
    [
        "../secrets.md",
        "a/../../etc/passwd.md",
        "wiki/../../../root/.ssh/id_rsa.md",
        r"C:\note.md",
        r"\\serveur\part\note.md",
        "/etc/passwd.md",
        "_design/vues.md",
        "wiki/note\x00.md",
    ],
)
def test_rejette_les_sorties_du_vault(chemin: str) -> None:
    with pytest.raises(CheminInvalideError):
        valider_ecriture(chemin)


@pytest.mark.parametrize("chemin", ["wiki/note.txt", "wiki/note", "wiki/note.MD.bak"])
def test_rejette_extension_non_md(chemin: str) -> None:
    with pytest.raises(CheminInvalideError):
        valider_ecriture(chemin)


# --- Trous noirs : ecrire la, c est perdre la note -------------------------
def test_rejette_les_journaux_livesync() -> None:
    with pytest.raises(CheminInvalideError):
        valider_ecriture("wiki/livesync_log_2026.md")


@pytest.mark.parametrize("chemin", ["wiki/.cache.md", ".cache.md", "a/b/.x.md"])
def test_rejette_les_noms_masques(chemin: str) -> None:
    with pytest.raises(CheminInvalideError):
        valider_ecriture(chemin)


@pytest.mark.parametrize("chemin", FICHIERS_NON_INDEXES)
def test_rejette_les_fichiers_exclus_de_l_indexation(chemin: str) -> None:
    with pytest.raises(CheminInvalideError):
        valider_ecriture(chemin)


def test_index_md_reste_ecrivable_ailleurs_qu_a_la_racine() -> None:
    # `FICHIERS_EXCLUS` de reindex.py compare le chemin RELATIF complet : seul
    # `index.md` a la racine est ecarte de l indexation.
    assert valider_ecriture("wiki/index.md").relatif == "wiki/index.md"


# --- Acceptations ----------------------------------------------------------
@pytest.mark.parametrize(
    "chemin",
    [
        "wiki/concepts/Nouvelle.md",
        "notes/idée éàü.md",
        "bac-a-sable/test-mcp.md",
        r"wiki\concepts\.\Portage.md",
    ],
)
def test_accepte_les_chemins_legitimes(chemin: str) -> None:
    assert valider_ecriture(chemin).relatif.endswith(".md")


def test_normalise_les_backslash() -> None:
    assert valider_ecriture(r"wiki\concepts\Portage.md").relatif == "wiki/concepts/Portage.md"


# --- Taille ----------------------------------------------------------------
def test_accepte_le_plafond_pile() -> None:
    assert valider_ecriture("wiki/n.md", "a" * MAX_NOTE_BYTES).relatif == "wiki/n.md"


def test_refuse_un_octet_de_trop() -> None:
    with pytest.raises(CheminInvalideError):
        valider_ecriture("wiki/n.md", "a" * (MAX_NOTE_BYTES + 1))


def test_taille_comptee_en_octets_utf8() -> None:
    assert verifier_taille("é") == 2


def test_contenu_absent_ne_declenche_aucun_controle_de_taille() -> None:
    # `delete` et `move` n ont pas de contenu : passer None doit rester valide.
    assert valider_ecriture("wiki/n.md", None).relatif == "wiki/n.md"


# --- Le message d erreur ne divulgue rien ----------------------------------
def test_le_message_ne_contient_aucun_chemin_systeme() -> None:
    with pytest.raises(CheminInvalideError) as capture:
        valider_ecriture("../../etc/shadow.md")
    assert "/srv" not in str(capture.value)
    assert "/opt" not in str(capture.value)
