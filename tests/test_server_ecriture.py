"""Tests des gardes d ecriture et de la convention `limit=0` (adr/0020).

Le point le plus important de ce fichier est la triple garde : un outil
d ecriture doit refuser SANS jeton, avec un jeton portant la seule portee de
lecture, et quand le drapeau `VAULT_MCP_ECRITURE` est a 0. Les trois sont
independants et chacun doit suffire.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest

SECRET_DE_TEST = "x" * 32

OUTILS_D_ECRITURE = [
    ("create_note", {"path": "wiki/n.md", "content": "x"}),
    ("update_note", {"path": "wiki/n.md", "content": "x"}),
    ("append_note", {"path": "wiki/n.md", "content": "x"}),
    ("patch_note", {"path": "wiki/n.md", "old_string": "a", "new_string": "b"}),
    ("set_frontmatter", {"path": "wiki/n.md", "fields": {"k": "v"}}),
    ("delete_note", {"path": "wiki/n.md"}),
    ("move_note", {"path": "wiki/a.md", "new_path": "wiki/b.md"}),
    ("rename_note", {"path": "wiki/a.md", "new_name": "b"}),
    ("create_folder", {"path": "wiki/nouveau-dossier"}),
    ("fix_links", {"path": "wiki/n.md"}),
    ("reindex_note", {"path": "wiki/n.md"}),
    ("reindex_vault", {}),
    ("sync_now", {}),
]


@pytest.fixture
def serveur(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    monkeypatch.setenv("MCP_SECRET", SECRET_DE_TEST)
    monkeypatch.setenv("MCP_PORT", "8799")
    monkeypatch.setenv("COUCH_URL", "http://u:p@127.0.0.1:5984")
    monkeypatch.setenv("VAULT_MCP_ISSUER", "https://exemple.test")
    monkeypatch.setenv("VAULT_MCP_OAUTH_DIR", str(tmp_path / "oauth"))
    monkeypatch.setenv("VAULT_MCP_SPOOL", str(tmp_path / "spool"))
    module = importlib.import_module("vault_mcp.server")
    return importlib.reload(module)


class _JetonDouble:
    def __init__(self, scopes: list[str]) -> None:
        self.scopes = scopes
        self.client_id = "client-de-test"


def _poser_jeton(serveur: Any, monkeypatch: pytest.MonkeyPatch, scopes: list[str] | None) -> None:
    jeton = _JetonDouble(scopes) if scopes is not None else None
    monkeypatch.setattr(serveur, "get_access_token", lambda: jeton)


# --- Garde 1 : le drapeau d arret ------------------------------------------
@pytest.mark.parametrize(("nom", "arguments"), OUTILS_D_ECRITURE)
def test_ecriture_eteinte_refuse_tout(
    serveur: Any, monkeypatch: pytest.MonkeyPatch, nom: str, arguments: dict[str, Any]
) -> None:
    monkeypatch.setenv("VAULT_MCP_ECRITURE", "0")
    _poser_jeton(serveur, monkeypatch, ["mcp:lecture", "mcp:ecriture"])
    resultat = getattr(serveur, nom)(**arguments)
    assert resultat["etat"] == "refuse"
    assert "desactivee" in resultat["message"]


# --- Garde 2 : la portee ---------------------------------------------------
@pytest.mark.parametrize(("nom", "arguments"), OUTILS_D_ECRITURE)
def test_sans_jeton_refuse(
    serveur: Any, monkeypatch: pytest.MonkeyPatch, nom: str, arguments: dict[str, Any]
) -> None:
    monkeypatch.setenv("VAULT_MCP_ECRITURE", "1")
    _poser_jeton(serveur, monkeypatch, None)
    resultat = getattr(serveur, nom)(**arguments)
    assert resultat["etat"] == "refuse"
    assert "mcp:ecriture" in resultat["message"]


@pytest.mark.parametrize(("nom", "arguments"), OUTILS_D_ECRITURE)
def test_portee_lecture_seule_refuse(
    serveur: Any, monkeypatch: pytest.MonkeyPatch, nom: str, arguments: dict[str, Any]
) -> None:
    # Le cas qui justifie toute la portee separee : un jeton de LECTURE deja
    # emis ne doit jamais valoir droit d ecriture.
    monkeypatch.setenv("VAULT_MCP_ECRITURE", "1")
    _poser_jeton(serveur, monkeypatch, ["mcp:lecture"])
    resultat = getattr(serveur, nom)(**arguments)
    assert resultat["etat"] == "refuse"
    assert "mcp:ecriture" in resultat["message"]


# --- La garde precede la validation de chemin ------------------------------
def test_la_garde_precede_toute_validation_de_chemin(
    serveur: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Un appelant non autorise ne doit meme pas apprendre si un chemin est valide.
    monkeypatch.setenv("VAULT_MCP_ECRITURE", "1")
    _poser_jeton(serveur, monkeypatch, ["mcp:lecture"])
    resultat = serveur.create_note(path="../../etc/passwd", content="x")
    assert "mcp:ecriture" in resultat["message"]
    assert "chemin" not in resultat["message"]


# --- vault_status est en lecture -------------------------------------------
def test_vault_status_ne_demande_pas_la_portee_d_ecriture(
    serveur: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VAULT_MCP_ECRITURE", "0")
    _poser_jeton(serveur, monkeypatch, ["mcp:lecture"])
    etat = serveur.vault_status()
    assert etat["ecriture_activee"] is False
    assert "spool" in etat


def test_write_status_ne_demande_pas_la_portee_d_ecriture(
    serveur: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _poser_jeton(serveur, monkeypatch, ["mcp:lecture"])
    assert serveur.write_status(id="aaaaaaaaaaaa")["etat"] == "inconnu"


# --- Le schema des outils survit au decorateur -----------------------------
@pytest.mark.parametrize(
    ("nom", "attendus"),
    [
        ("create_note", {"path", "content"}),
        ("update_note", {"path", "content", "expected_sha256"}),
        ("patch_note", {"path", "old_string", "new_string"}),
        ("move_note", {"path", "new_path", "rewrite_backlinks"}),
        ("create_folder", {"path"}),
        ("fix_links", {"path"}),
        ("reindex_note", {"path"}),
        ("write_status", {"id"}),
        ("list_notes", {"prefix", "limit"}),
    ],
)
def test_schema_des_outils_preserve(serveur: Any, nom: str, attendus: set[str]) -> None:
    # Un decorateur maison sans functools.wraps NI conservation des annotations
    # transformerait le schema de l outil en `**kwargs` : le client ne verrait
    # plus aucun parametre nomme, en silence.
    schema = serveur.mcp._tool_manager.get_tool(nom).parameters
    assert attendus <= set(schema["properties"])


# --- Convention limit=0 ----------------------------------------------------
class _StoreDouble:
    def __init__(self, chemins: list[str]) -> None:
        self._chemins = chemins

    def lister_chemins(self, prefix: str = "", limit: int = 200) -> list[str]:
        return self._chemins if limit <= 0 else self._chemins[:limit]

    def lire_note(self, path: str) -> str:
        return "abcdefghij"


def test_limit_zero_rend_tout(serveur: Any) -> None:
    serveur._store = _StoreDouble([f"n{i}.md" for i in range(10)])
    assert len(serveur.list_notes(limit=0)) == 10


def test_limit_positif_est_respecte(serveur: Any) -> None:
    serveur._store = _StoreDouble([f"n{i}.md" for i in range(10)])
    assert len(serveur.list_notes(limit=3)) == 3


def test_read_note_limit_zero_lit_jusqu_a_la_fin(serveur: Any) -> None:
    serveur._store = _StoreDouble([])
    assert serveur.read_note(path="wiki/n.md", offset=2, limit=0) == "cdefghij"


def test_read_note_limit_positif_pagine(serveur: Any) -> None:
    serveur._store = _StoreDouble([])
    assert serveur.read_note(path="wiki/n.md", offset=2, limit=3) == "cde"
