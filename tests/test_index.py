"""Tests de l'index et de la recherche hybride.

L'index est construit a la volee dans un repertoire temporaire, avec de vrais
vecteurs : c'est la fusion et le classement qu'on verifie, pas un double de modele.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from vault_mcp.embed import DIMENSIONS, vectoriser
from vault_mcp.index import (
    Index,
    MetaFragment,
    Resultat,
    extraire_wikilinks,
    fusion_rang_reciproque,
    sauvegarder,
)

NOTES = [
    ("reseau/ufw.md", "Pare-feu UFW", "Ouvrir un port dans le pare-feu UFW du serveur"),
    ("reseau/nginx.md", "Nginx", "Configurer un hote virtuel nginx avec TLS"),
    ("cuisine/tarte.md", "Tarte aux pommes", "Recette de la tarte aux pommes de grand-mere"),
    (
        "erreurs/oracle.md",
        "Erreur ORA-01555",
        "Le message ORA-01555 signale un snapshot trop ancien",
    ),
]


@pytest.fixture(scope="module")
def index(tmp_path_factory: pytest.TempPathFactory) -> Index:
    repertoire = tmp_path_factory.mktemp("index")
    metas = [MetaFragment(chemin=c, rang=0, titre=t, apercu=a) for c, t, a in NOTES]
    vecteurs = vectoriser([f"{t}\n{a}" for _, t, a in NOTES])
    sauvegarder(repertoire, vecteurs, metas, {"reseau/nginx.md": ["reseau/ufw.md"]})
    return Index(repertoire)


def test_wikilinks_extraits() -> None:
    contenu = "Voir [[Docker]] et [[Reseau|le reseau]] et [[Notes#section]]."
    assert extraire_wikilinks(contenu) == ["Docker", "Reseau", "Notes"]


def test_wikilinks_absents() -> None:
    assert extraire_wikilinks("Aucun lien ici.") == []


def test_sauvegarde_refuse_un_index_incoherent(tmp_path: Path) -> None:
    vecteurs = np.zeros((2, DIMENSIONS), dtype=np.float32)
    # Deux vecteurs, une seule metadonnee : l'alignement ligne <-> note est rompu et
    # la recherche renverrait le mauvais chemin sans jamais lever d'erreur.
    with pytest.raises(ValueError, match="incoherent"):
        sauvegarder(tmp_path, vecteurs, [MetaFragment("a.md", 0, "", "")], {})


def test_index_disponible(index: Index) -> None:
    assert index.disponible
    assert len(index.metas) == len(NOTES)
    assert index.vecteurs.shape == (len(NOTES), DIMENSIONS)


def test_backlinks_relus(index: Index) -> None:
    assert index.backlinks["reseau/nginx.md"] == ["reseau/ufw.md"]


def test_recherche_vectorielle_trouve_par_le_sens(index: Index) -> None:
    resultats = index.recherche_vectorielle("comment autoriser un port sur le firewall", limit=2)
    # Aucun mot de la requete n'apparait tel quel dans la note : c'est exactement ce
    # que le lexical ne sait pas faire.
    assert resultats[0].chemin == "reseau/ufw.md"


def test_recherche_lexicale_trouve_un_identifiant_exact(index: Index) -> None:
    resultats = index.recherche_lexicale("ORA-01555", limit=3)
    assert resultats[0].chemin == "erreurs/oracle.md"


def test_recherche_lexicale_ignore_une_requete_sans_mot(index: Index) -> None:
    assert index.recherche_lexicale("!!! ???", limit=3) == []


def test_recherche_hybride_renvoie_les_deux_origines(index: Index) -> None:
    resultats = index.recherche_hybride("pare-feu UFW", limit=3)
    assert resultats
    assert all(r.origine == "hybride" for r in resultats)


def test_pas_de_doublon_de_note_dans_les_resultats(index: Index) -> None:
    resultats = index.recherche_hybride("serveur", limit=4)
    chemins = [r.chemin for r in resultats]
    assert len(chemins) == len(set(chemins))


def test_index_absent_ne_leve_pas(tmp_path: Path) -> None:
    vide = Index(tmp_path)
    assert vide.disponible is False
    assert vide.recherche_vectorielle("quoi que ce soit") == []


def _r(chemin: str) -> Resultat:
    return Resultat(chemin=chemin, titre="", apercu="", score=0.0, origine="x")


def test_rrf_favorise_ce_qui_sort_dans_les_deux_listes() -> None:
    a = [_r("commun.md"), _r("seul_a.md")]
    b = [_r("seul_b.md"), _r("commun.md")]
    fusion = fusion_rang_reciproque(a, b, limit=3)
    assert fusion[0].chemin == "commun.md"


def test_rrf_est_insensible_a_lechelle_des_scores() -> None:
    # Un cosinus vit dans [-1, 1], un comptage de mots dans [0, 1] : seule la
    # position dans la liste compte, jamais la valeur du score.
    a = [Resultat("x.md", "", "", 0.99, "vecteur"), Resultat("y.md", "", "", 0.98, "vecteur")]
    b = [Resultat("y.md", "", "", 1000.0, "lexical")]
    fusion = fusion_rang_reciproque(a, b, limit=2)
    assert fusion[0].chemin == "y.md"


def test_rrf_sur_deux_listes_vides() -> None:
    assert fusion_rang_reciproque([], [], limit=5) == []
