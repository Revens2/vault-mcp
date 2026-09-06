"""Tests du fragment d'identite et de l'exclusion des sections de navigation.

Cas reel : `wiki/entities/Rclone.md` est exactement la reponse a la requete « rclone »,
et aucun moteur ne la remontait. Sa section « Sources liees » -- une seule ligne de
wikilink -- diluait sa moyenne, et ses tags `[tools, synchronization, backup, cloud]`
etaient jetes avec le frontmatter.
"""

from __future__ import annotations

import pytest

from vault_mcp.chunk import est_navigation, fragmenter, metadonnees

FICHE = """---
tags: [tools, synchronization, backup, cloud]
date_added: 2026-07-14
aliases: [rclone-cli]
---

# Rclone

Outil en ligne de commande open-source pour synchroniser des fichiers vers du stockage
cloud, employe dans les scripts de sauvegarde de ce serveur.

## Sources liees
- [[1-script-de-sauvegarde|1. Script de sauvegarde]]
"""


def test_metadonnees_extraites() -> None:
    mots = metadonnees(FICHE)
    for attendu in ("tools", "synchronization", "backup", "cloud", "rclone-cli"):
        assert attendu in mots


def test_metadonnees_sans_frontmatter() -> None:
    assert metadonnees("# Titre\ncorps") == ""


def test_metadonnees_listes_vides() -> None:
    assert metadonnees("---\ntags: []\naliases: []\n---\ncorps") == ""


def test_metadonnees_sans_doublon() -> None:
    mots = metadonnees("---\ntags: [a, a, b]\naliases: [b]\n---\nx").split()
    assert mots == list(dict.fromkeys(mots))


@pytest.mark.parametrize(
    ("section", "attendu"),
    [
        ("- [[Une note]]", True),
        ("- [[Une note]]\n- [[Une autre]]", True),
        ("[[Sans puce]]", True),
        ("Un paragraphe qui cite [[Une note]] au milieu d'une phrase reelle.", False),
        ("Texte\n- [[Un lien]]\nAutre texte\nEncore du texte", False),
        ("", False),
    ],
)
def test_est_navigation(section: str, attendu: bool) -> None:
    assert est_navigation(section) is attendu


def test_fragment_didentite_en_premier() -> None:
    fragments = fragmenter("wiki/entities/Rclone.md", FICHE)
    premier = fragments[0]
    assert premier.rang == 0
    assert premier.titre == "Rclone"
    # Le nom de la note, ses tags et son debut, dans un seul fragment court et dense.
    for attendu in ("Rclone", "synchronization", "sauvegarde"):
        assert attendu in premier.texte


def test_section_de_navigation_ecartee() -> None:
    fragments = fragmenter("wiki/entities/Rclone.md", FICHE)
    titres = [f.titre for f in fragments]
    # « Sources liees » n'est que du balisage : indexee, elle tirait la moyenne de la
    # fiche vers le bas, et d'autant plus que la fiche etait courte et bien redigee.
    assert "Sources liees" not in titres


def test_le_balisage_de_titre_ne_pollue_pas_lidentite() -> None:
    fragments = fragmenter("wiki/entities/Rclone.md", FICHE)
    assert "#" not in fragments[0].texte


def test_note_sans_frontmatter_a_quand_meme_une_identite() -> None:
    fragments = fragmenter("notes/Idee.md", "# Idee\nUn contenu suffisamment long pour rester.")
    assert fragments[0].texte.startswith("Idee")


def test_rangs_toujours_consecutifs() -> None:
    fragments = fragmenter("wiki/entities/Rclone.md", FICHE)
    assert [f.rang for f in fragments] == list(range(len(fragments)))
