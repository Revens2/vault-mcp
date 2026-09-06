"""Tests de `vault_mcp.ecriture` (adr/0020).

Module pur : chaque test est une transformation texte -> texte. Les cas sont
choisis pour couvrir les degradations SILENCIEUSES -- celles qui produisent une
note syntaxiquement valide mais fausse, et qu on ne decouvre que des mois plus
tard en relisant le vault.
"""

from __future__ import annotations

import pytest

from vault_mcp.ecriture import (
    EcritureError,
    appliquer_append,
    appliquer_frontmatter,
    appliquer_patch,
    compte_wikilinks,
    empreinte,
    reecrire_wikilinks,
    verifier_empreinte,
)


# --- Empreinte -------------------------------------------------------------
def test_empreinte_stable_et_utf8() -> None:
    assert empreinte("é") == empreinte("é")
    assert len(empreinte("x")) == 64
    assert empreinte("a") != empreinte("b")


def test_verifier_empreinte_accepte_l_identique() -> None:
    verifier_empreinte("contenu", empreinte("contenu"))


def test_verifier_empreinte_refuse_une_divergence() -> None:
    with pytest.raises(EcritureError):
        verifier_empreinte("contenu modifie dans Obsidian", empreinte("contenu"))


@pytest.mark.parametrize("attendue", [None, ""])
def test_empreinte_absente_desactive_le_controle(attendue: str | None) -> None:
    verifier_empreinte("n importe quoi", attendue)


# --- Append ----------------------------------------------------------------
def test_append_insere_exactement_un_saut_de_ligne() -> None:
    assert appliquer_append("ligne 1", "ligne 2") == "ligne 1\nligne 2"


def test_append_ne_double_pas_le_saut_de_ligne_existant() -> None:
    assert appliquer_append("ligne 1\n", "ligne 2") == "ligne 1\nligne 2"
    assert appliquer_append("ligne 1\n\n\n", "\n\nligne 2") == "ligne 1\nligne 2"


def test_append_sur_note_vide() -> None:
    assert appliquer_append("", "premier contenu") == "premier contenu"


# --- Patch -----------------------------------------------------------------
def test_patch_remplace_l_occurrence_unique() -> None:
    assert appliquer_patch("le chat dort", "chat", "chien") == "le chien dort"


def test_patch_refuse_zero_occurrence() -> None:
    with pytest.raises(EcritureError, match="introuvable"):
        appliquer_patch("le chat dort", "souris", "chien")


def test_patch_refuse_plusieurs_occurrences() -> None:
    # Le refus est le comportement voulu : un `replace` global silencieux est la
    # facon la plus simple de corrompre une note longue sans s en apercevoir.
    with pytest.raises(EcritureError, match="2 fois"):
        appliquer_patch("chat et chat", "chat", "chien")


def test_patch_refuse_une_cible_vide() -> None:
    with pytest.raises(EcritureError):
        appliquer_patch("texte", "", "x")


def test_patch_multiligne() -> None:
    ancien = "# Titre\n\nancien paragraphe\n\n## Fin\n"
    assert appliquer_patch(ancien, "ancien paragraphe", "nouveau\ntexte") == (
        "# Titre\n\nnouveau\ntexte\n\n## Fin\n"
    )


# --- Frontmatter -----------------------------------------------------------
def test_frontmatter_cree_le_bloc_s_il_est_absent() -> None:
    assert appliquer_frontmatter("# Titre\n", {"tags": "projet"}) == (
        "---\ntags: projet\n---\n# Titre\n"
    )


def test_frontmatter_fusionne_sans_toucher_au_reste() -> None:
    ancien = "---\ntitre: Ancien\ntags:\n  - a\n  - b\n# un commentaire\n---\n\nCorps.\n"
    obtenu = appliquer_frontmatter(ancien, {"titre": "Nouveau"})
    assert obtenu == "---\ntitre: Nouveau\ntags:\n  - a\n  - b\n# un commentaire\n---\n\nCorps.\n"


def test_frontmatter_preserve_l_ordre_et_ajoute_a_la_fin() -> None:
    ancien = "---\na: 1\nb: 2\n---\ncorps\n"
    assert appliquer_frontmatter(ancien, {"c": 3}) == "---\na: 1\nb: 2\nc: 3\n---\ncorps\n"


def test_frontmatter_ne_casse_pas_un_corps_contenant_des_tirets() -> None:
    # Une separation horizontale markdown dans le corps est frequente : la
    # confondre avec la fin du frontmatter tronquerait la note.
    ancien = "---\na: 1\n---\n\nDebut\n\n---\n\nFin\n"
    obtenu = appliquer_frontmatter(ancien, {"a": 2})
    assert obtenu == "---\na: 2\n---\n\nDebut\n\n---\n\nFin\n"


def test_frontmatter_ouvrant_sans_fermeture_est_traite_comme_du_corps() -> None:
    ancien = "---\nceci n est pas ferme\n"
    obtenu = appliquer_frontmatter(ancien, {"a": 1})
    assert obtenu.startswith("---\na: 1\n---\n")
    assert "ceci n est pas ferme" in obtenu


def test_frontmatter_ne_touche_pas_une_ligne_indentee_homonyme() -> None:
    ancien = "---\nmeta:\n  titre: interne\n---\ncorps\n"
    obtenu = appliquer_frontmatter(ancien, {"titre": "externe"})
    # La sous-cle indentee reste intacte ; la nouvelle cle est ajoutee au bloc.
    assert "  titre: interne" in obtenu
    assert "titre: externe" in obtenu


@pytest.mark.parametrize(
    ("valeur", "attendu"),
    [
        (True, "true"),
        (False, "false"),
        (42, "42"),
        (["a", "b"], "[a, b]"),
        ("simple", "simple"),
        ("avec: deux points", '"avec: deux points"'),
        ("true", '"true"'),
        ("12", '"12"'),
        ("", '""'),
    ],
)
def test_frontmatter_serialisation_des_scalaires(valeur: object, attendu: str) -> None:
    obtenu = appliquer_frontmatter("corps\n", {"k": valeur})
    assert obtenu.splitlines()[1] == f"k: {attendu}"


@pytest.mark.parametrize("cle", ["", "a:b", "a\nb", " a "])
def test_frontmatter_refuse_un_nom_de_cle_invalide(cle: str) -> None:
    with pytest.raises(EcritureError):
        appliquer_frontmatter("corps\n", {cle: "x"})


def test_frontmatter_refuse_une_demande_vide() -> None:
    with pytest.raises(EcritureError):
        appliquer_frontmatter("corps\n", {})


# --- Wikilinks -------------------------------------------------------------
@pytest.mark.parametrize(
    ("avant", "apres"),
    [
        ("[[Ancien]]", "[[Nouveau]]"),
        ("[[Ancien|alias]]", "[[Nouveau|alias]]"),
        ("[[Ancien#ancre]]", "[[Nouveau#ancre]]"),
        ("[[Ancien#ancre|alias]]", "[[Nouveau#ancre|alias]]"),
        ("[[ Ancien ]]", "[[Nouveau]]"),
    ],
)
def test_reecrit_toutes_les_formes_de_wikilink(avant: str, apres: str) -> None:
    assert reecrire_wikilinks(f"voir {avant} ici", "Ancien", "Nouveau") == f"voir {apres} ici"


@pytest.mark.parametrize(
    "intact",
    ["[[Ancienne chose]]", "[[Ancien2]]", "[[Prefixe Ancien]]", "Ancien", "[[AncienBis|a]]"],
)
def test_ne_touche_pas_un_nom_qui_n_est_pas_strictement_egal(intact: str) -> None:
    assert reecrire_wikilinks(intact, "Ancien", "Nouveau") == intact


def test_epargne_les_blocs_de_code_clotures() -> None:
    contenu = "avant [[Ancien]]\n```\nexemple [[Ancien]]\n```\napres [[Ancien]]\n"
    obtenu = reecrire_wikilinks(contenu, "Ancien", "Nouveau")
    assert obtenu == "avant [[Nouveau]]\n```\nexemple [[Ancien]]\n```\napres [[Nouveau]]\n"


def test_epargne_le_code_en_ligne() -> None:
    contenu = "syntaxe : `[[Ancien]]` puis [[Ancien]]"
    assert reecrire_wikilinks(contenu, "Ancien", "Nouveau") == (
        "syntaxe : `[[Ancien]]` puis [[Nouveau]]"
    )


def test_reecrit_plusieurs_occurrences() -> None:
    assert reecrire_wikilinks("[[A]] et [[A|x]]", "A", "B") == "[[B]] et [[B|x]]"


def test_renommage_identique_ne_change_rien() -> None:
    assert reecrire_wikilinks("[[A]]", "A", "A") == "[[A]]"


@pytest.mark.parametrize(("ancien", "nouveau"), [("", "B"), ("A", "")])
def test_refuse_un_nom_vide(ancien: str, nouveau: str) -> None:
    with pytest.raises(EcritureError):
        reecrire_wikilinks("[[A]]", ancien, nouveau)


def test_compte_wikilinks_hors_blocs_de_code() -> None:
    contenu = "[[A]] [[A|x]]\n```\n[[A]]\n```\n`[[A]]`"
    assert compte_wikilinks(contenu, "A") == 2
