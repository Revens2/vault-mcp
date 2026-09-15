"""Voie fraiche : une note est trouvable avant son embedding, puis retiree une fois publiee."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from vault_mcp import dirty, frais
from vault_mcp.chunk import fragmenter
from vault_mcp.embed import vectoriser
from vault_mcp.index import APERCU_CARACTERES, Index, MetaFragment, sauvegarder

CONVIA = "raw/assets/ConvIA/Claude-CLI/2026-09-15_nouvelle_abcd1234.md"


@pytest.fixture
def miroir() -> Path:
    racine = Path(os.environ["VAULT_MCP_VAULT"])
    racine.mkdir(parents=True, exist_ok=True)
    return racine


def _ecrire(racine: Path, chemin: str, contenu: str) -> None:
    fichier = racine / chemin
    fichier.parent.mkdir(parents=True, exist_ok=True)
    fichier.write_text(contenu, encoding="utf-8")


def test_note_salie_trouvable_sans_embedding(miroir: Path) -> None:
    _ecrire(miroir, CONVIA, "# Conversation\nDiagnostic du service QX-7781 en production.")
    dirty.salir([CONVIA])
    assert frais.rafraichir()["ajoutees"] == 1
    resultats = frais.rechercher("QX-7781", limit=5)
    assert [r.chemin for r in resultats] == [CONVIA]
    assert resultats[0].origine == "frais"
    assert "QX-7781" in resultats[0].apercu


def test_note_publiee_retiree(miroir: Path) -> None:
    _ecrire(miroir, CONVIA, "# Conversation\nQX-7781")
    dirty.salir([CONVIA])
    frais.rafraichir()
    jeton, _ = dirty.reclamer()
    dirty.acquitter(jeton)
    assert frais.rafraichir()["retirees"] == 1
    assert frais.rechercher("QX-7781") == []


def test_arrivee_rclone_couverte_par_le_seuil(miroir: Path) -> None:
    dirty.poser_seuil_reconciliation(time.time_ns() - 5_000_000_000)
    _ecrire(miroir, CONVIA, "# Conversation\nArrivee par rclone, mot unique zorglubine.")
    frais.rafraichir()
    assert [r.chemin for r in frais.rechercher("zorglubine")] == [CONVIA]


def test_note_deja_indexee_hors_voie_fraiche(miroir: Path) -> None:
    deja = "notes/infra/etat.md"
    _ecrire(miroir, deja, "# Etat\nQX-7781 deja indexe.")
    _ecrire(miroir, CONVIA, "# Conversation\nQX-7781 nouvelle.")
    sauvegarder(Path(os.environ["VAULT_MCP_INDEX"]), vectoriser(["QX-7781 deja indexe."]),
                [MetaFragment(deja, 0, "Etat", "QX-7781 deja indexe.")], {})
    dirty.salir([deja, CONVIA])
    frais.rafraichir()
    assert [r.chemin for r in frais.rechercher("QX-7781")] == [CONVIA]


def test_corbeille_exclue(miroir: Path) -> None:
    corbeille = ".trash-wiki-publish/2026/x.md"
    _ecrire(miroir, corbeille, "QX-7781")
    dirty.salir([corbeille])
    frais.rafraichir()
    assert frais.rechercher("QX-7781") == []


def test_base_absente_rend_rien() -> None:
    assert frais.rechercher("n importe quoi") == []
    assert frais.statistiques() == {"frais_disponible": False}


def test_hybride_fusionne_la_voie_fraiche(miroir: Path, tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    ancienne = "notes/infra/etat.md"
    _ecrire(miroir, ancienne, "# Etat\nService de cuisine, rien a voir.")
    textes, metas = [], []
    for f in fragmenter(ancienne, (miroir / ancienne).read_text(encoding="utf-8")):
        textes.append(f.texte)
        metas.append(MetaFragment(f.chemin, f.rang, f.titre, f.texte[:APERCU_CARACTERES]))
    sauvegarder(tmp_path / "idx", vectoriser(textes), metas, {})
    index = Index(tmp_path / "idx")

    _ecrire(miroir, CONVIA, "# Conversation\nIncident QX-7781 resolu par redemarrage.")
    dirty.salir([CONVIA])
    frais.rafraichir()

    # Pas le rang 1 : dans un index d'une seule note, celle-ci a toujours le rang
    # vectoriel 0 et le prior d'autorite. On exige la presence, marquee fraiche.
    resultats = {r.chemin: r for r in index.recherche_hybride("QX-7781", limit=3)}
    assert resultats[CONVIA].origine == "frais"
    assert CONVIA in [r.chemin for r in index.recherche_lexicale("QX-7781", limit=3)]

    monkeypatch.setenv("VAULT_MCP_POIDS_FRAIS", "0")
    assert CONVIA not in [r.chemin for r in index.recherche_hybride("QX-7781", limit=3)]
