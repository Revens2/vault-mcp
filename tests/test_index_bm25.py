"""Hybride v2 : BM25 plein texte depuis le miroir + prior d'autorite."""

from __future__ import annotations

from pathlib import Path

import pytest

from vault_mcp.autorite import historique, rang_autorite
from vault_mcp.chunk import fragmenter
from vault_mcp.embed import vectoriser
from vault_mcp.index import APERCU_CARACTERES, Index, MetaFragment, sauvegarder

# L'identifiant est APRES le 240e caractere : invisible pour l'ancien lexical.
REMPLISSAGE = "Texte de contexte sans identifiant particulier. " * 8
NOTES = {
    "notes/infra/etat.md": f"# Etat reel\n{REMPLISSAGE}\nService ProtectSystem=strict ZX-4242.",
    "raw/assets/ConvIA/x/2026-08-01_conv.md": "# Conversation\nOn parle de cuisine et de tartes.",
    "wiki/sources/tarte.md": "# Tarte\nRecette de la tarte aux pommes.",
    ".trash-wiki-publish/2026/vieux.md": "# Vieux\nZX-4242 ZX-4242 ZX-4242 dans la corbeille.",
}


@pytest.fixture
def index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Index:
    miroir = tmp_path / "miroir"
    for chemin, contenu in NOTES.items():
        fichier = miroir / chemin
        fichier.parent.mkdir(parents=True, exist_ok=True)
        fichier.write_text(contenu, encoding="utf-8")
    monkeypatch.setenv("VAULT_MCP_VAULT", str(miroir))
    textes, metas = [], []
    for chemin, contenu in NOTES.items():
        for f in fragmenter(chemin, contenu):
            textes.append(f.texte)
            apercu = f.texte[:APERCU_CARACTERES].replace("\n", " ")
            metas.append(MetaFragment(f.chemin, f.rang, f.titre, apercu))
    repertoire = tmp_path / "index"
    sauvegarder(repertoire, vectoriser(textes), metas, {})
    return Index(repertoire)


def test_identifiant_enfoui_trouve_en_hybride(index: Index) -> None:
    ancien = [r.chemin for r in index.recherche_lexicale("ZX-4242", limit=3)]
    assert "notes/infra/etat.md" not in ancien
    resultats = index.recherche_hybride("ZX-4242", limit=3)
    assert resultats[0].chemin == "notes/infra/etat.md"


def test_corbeille_jamais_renvoyee(index: Index) -> None:
    chemins = [r.chemin for r in index.recherche_hybride("ZX-4242 corbeille", limit=10)]
    assert ".trash-wiki-publish/2026/vieux.md" not in chemins


def test_pas_de_doublon(index: Index) -> None:
    chemins = [r.chemin for r in index.recherche_hybride("tarte", limit=10)]
    assert len(chemins) == len(set(chemins))


def test_autorite_par_chemin() -> None:
    assert rang_autorite("notes/infra/etat.md") == 0
    assert rang_autorite("wiki/entities/X.md") == 1
    assert rang_autorite("wiki/sources/x.md") == 2
    assert rang_autorite("raw/assets/ConvIA-Analysis/a.md") == 3
    assert rang_autorite("raw/assets/ConvIA/a.md") == 4


def test_intention_historique() -> None:
    assert historique("conversation du 28 août sur NetBird")
    assert not historique("quels ports UFW sont ouverts")
