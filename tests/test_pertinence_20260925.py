"""Reglages de pertinence/cout du 2026-09-25 (audit qualite RAG V1).

A. journaux dates au rang d'autorite 2 ; B. quasi-doublons raw ecartes ;
C. search_vault a 5 resultats par defaut ; D. transcripts d'evaluation RAG non indexes.
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from typing import Any

import pytest

from vault_mcp.autorite import historique, rang_autorite
from vault_mcp.index import POOL_MIN, cle_quasi_doublon, sans_quasi_doublons
from vault_mcp.selection import indexable


# --- A ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "chemin",
    [
        "notes/activity/daily/2026-09-24.md",
        "notes/planning/reconciliation/2026-09-20-reconciliation.md",
    ],
)
def test_journaux_au_rang_2(chemin: str) -> None:
    assert rang_autorite(chemin) == 2


@pytest.mark.parametrize(
    ("chemin", "rang"),
    [
        ("notes/planning/CURRENT_STATE.md", 0),
        ("notes/activity/aw/resume.md", 0),
        ("notes/system/hardware/inventaire.md", 0),
        ("wiki/entities/NetBird.md", 1),
        ("wiki/sources/x.md", 2),
        ("raw/assets/ConvIA-Analysis/a.md", 3),
        ("raw/assets/ConvIA/Claude-CLI/b.md", 4),
    ],
)
def test_autres_rangs_inchanges(chemin: str, rang: int) -> None:
    assert rang_autorite(chemin) == rang


def test_requete_historique_desactive_le_prior() -> None:
    # Le prior entier (donc la retrogradation des journaux) saute sur ces requetes.
    assert historique("qu'ai-je fait le 24 septembre ?")
    assert historique("la conversation sur NetBird")
    assert not historique("quelle IP NetBird pour vps-etude ?")


# --- B ---------------------------------------------------------------------------


def test_cle_quasi_doublon_ignore_le_suffixe_hash() -> None:
    a = "raw/assets/ConvIA/Claude-Desktop/2026-09-20_question-sata_8fbb0de7.md"
    b = "raw/assets/ConvIA/Claude-Desktop/2026-09-20_question-sata_145b7709.md"
    assert cle_quasi_doublon(a) == cle_quasi_doublon(b)
    assert cle_quasi_doublon("notes/a.md") != cle_quasi_doublon("notes/b.md")


def test_quasi_doublons_raw_ecartes_et_top_complete() -> None:
    classes = [
        ("raw/c/2026-09-20_q_8fbb0de7.md", 0.9),
        ("raw/c/2026-09-20_q_145b7709.md", 0.8),
        ("notes/fiche.md", 0.7),
        ("raw/c/2026-09-20_q_f4ecc38a.md", 0.6),
        ("wiki/entities/NAS.md", 0.5),
        ("raw/c/autre_0cca3063.md", 0.4),
    ]
    sortie = [c for c, _ in sans_quasi_doublons(classes, 3)]
    assert sortie == ["raw/c/2026-09-20_q_8fbb0de7.md", "notes/fiche.md", "wiki/entities/NAS.md"]


def test_hors_raw_jamais_ecarte() -> None:
    classes = [("notes/a/NAS.md", 1.0), ("wiki/entities/NAS.md", 0.9)]
    assert len(sans_quasi_doublons(classes, 10)) == 2


def test_pool_plancher_couvre_le_dedoublonnage() -> None:
    assert POOL_MIN >= 150


# --- C ---------------------------------------------------------------------------


@pytest.fixture
def serveur(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    monkeypatch.setenv("MCP_SECRET", "x" * 32)
    monkeypatch.setenv("MCP_PORT", "8799")
    monkeypatch.setenv("COUCH_URL", "http://u:p@127.0.0.1:5984")
    monkeypatch.setenv("VAULT_MCP_ISSUER", "https://exemple.test")
    monkeypatch.setenv("VAULT_MCP_OAUTH_DIR", str(tmp_path / "oauth"))
    return importlib.reload(importlib.import_module("vault_mcp.server"))


def test_search_vault_5_par_defaut_limit_explicite_respecte(
    serveur: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert inspect.signature(serveur.search_vault).parameters["limit"].default == 5
    vus: list[int] = []

    class _Idx:
        disponible = True
        metas = [object()] * 40

        def recherche_hybride(self, q: str, n: int) -> list[Any]:
            vus.append(n)
            return []

        recherche_vectorielle = recherche_lexicale = recherche_hybride

    monkeypatch.setattr(serveur, "_index", _Idx())
    serveur.search_vault("x")
    serveur.search_vault("x", limit=12)
    serveur.search_vault("x", limit=0)
    assert vus == [5, 12, 40]


# --- D ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "chemin",
    [
        "raw/assets/ConvIA/Claude-Desktop/2026-09-20_rag-v2-answer-eval-question-rendez-vous_fa6eac9c.md",
        "raw/assets/ConvIA/Claude-Desktop/2026-09-20_question-quel-ancien-controleur-sata_8fbb0de7.md",
    ],
)
def test_transcripts_eval_rag_exclus(chemin: str) -> None:
    assert not indexable(chemin)


@pytest.mark.parametrize(
    "chemin",
    [
        "raw/assets/ConvIA/Claude-CLI/2026-09-14_rag-retrieval-production-deploy_78867741.md",
        "raw/assets/ConvIA/Claude-Desktop/2026-09-20_dis-juste-ok_217fe9a8.md",
        "raw/assets/ConvIA-Analysis/2026-09-20_question-sata.md",
        "notes/question-ouverte.md",
    ],
)
def test_autres_transcripts_indexables(chemin: str) -> None:
    assert indexable(chemin)
