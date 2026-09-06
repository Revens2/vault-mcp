"""Tests du decoupage."""

from __future__ import annotations

from vault_mcp.chunk import (
    RECOUVREMENT,
    TAILLE_FENETRE,
    Fragment,
    fragmenter,
    retirer_frontmatter,
)


def test_frontmatter_retire() -> None:
    contenu = "---\ntitle: X\ntags: [a]\n---\nLe corps."
    assert retirer_frontmatter(contenu) == "Le corps."


def test_frontmatter_absent_ne_casse_rien() -> None:
    assert retirer_frontmatter("Pas de frontmatter") == "Pas de frontmatter"


def test_note_vide_ne_produit_aucun_fragment() -> None:
    assert fragmenter("n.md", "---\ntitle: X\n---\n") == []


def test_note_courte_produit_un_fragment_didentite_et_son_corps() -> None:
    fragments = fragmenter("n.md", "Une note breve mais suffisante.")
    # Depuis 2026-08-15, le rang 0 est le fragment d identite (nom + tags + debut) ;
    # le corps suit. Voir tests/test_chunk_identite.py.
    assert len(fragments) == 2
    assert fragments[0].chemin == "n.md"
    assert fragments[0].rang == 0
    assert fragments[0].texte.startswith("n ")


def test_decoupe_par_titres() -> None:
    contenu = (
        "# Titre\nPreambule assez long pour compter comme un vrai fragment de texte.\n"
        "## Docker\nInstallation de Docker, avec suffisamment de texte pour etre garde.\n"
        "## Nginx\nInstallation de Nginx, avec suffisamment de texte pour etre garde.\n"
    )
    fragments = fragmenter("n.md", contenu)
    titres = [f.titre for f in fragments]
    assert "Docker" in titres
    assert "Nginx" in titres


def test_titre_prefixe_le_texte_vectorise() -> None:
    contenu = "## Docker\n" + "Installation detaillee du moteur de conteneurs. " * 3
    fragments = fragmenter("n.md", contenu)
    # Sans le titre, "Installation" sous Docker et sous Nginx donnent le meme vecteur.
    # Depuis 2026-08-15 le rang 0 est le fragment d identite : le titre s y trouve
    # aussi, mais sous une autre forme. On verifie qu il est present quelque part.
    assert any("Docker" in f.texte for f in fragments)
    assert fragments[0].rang == 0


def test_section_longue_est_fenetree_avec_recouvrement() -> None:
    section = "phrase unique. " * 400  # ~6000 caracteres
    fragments = fragmenter("n.md", "## Long\n" + section)
    assert len(fragments) >= 2
    # Le recouvrement doit etre reel : la fin du fragment n et le debut du n+1
    # partagent du texte.
    premier = fragments[0].texte
    second = fragments[1].texte
    queue = premier[-RECOUVREMENT // 2 :]
    assert queue in second


def test_rangs_consecutifs_et_uniques() -> None:
    fragments = fragmenter("n.md", "## A\n" + "texte. " * 300 + "\n## B\n" + "autre. " * 300)
    rangs = [f.rang for f in fragments]
    assert rangs == list(range(len(fragments)))


def test_fenetre_ne_depasse_jamais_la_taille_max() -> None:
    fragments = fragmenter("n.md", "## X\n" + "a" * 5000)
    for f in fragments:
        # Le titre ajoute quelques caracteres : on borne sur le texte fenetre.
        assert len(f.texte) <= TAILLE_FENETRE + len("X\n")


def test_fragments_sont_immuables() -> None:
    f = Fragment(chemin="a.md", rang=0, titre="t", texte="x")
    try:
        f.rang = 1  # type: ignore[misc]
    except Exception as exc:
        assert "frozen" in str(type(exc)).lower() or "attribute" in str(exc).lower()
    else:
        raise AssertionError("Fragment doit etre immuable")
