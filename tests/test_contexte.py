"""Second MCP `vault-context` : surface strictement lecture seule, jeton dedie obligatoire."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from vault_mcp import contexte

JETON = "c" * 40


def test_surface_strictement_lecture() -> None:
    noms = {outil.name for outil in asyncio.run(contexte.mcp.list_tools())}
    assert noms == contexte.OUTILS_LECTURE
    interdits = ("create", "update", "append", "patch", "delete", "move", "rename", "fix",
                 "reindex", "sync", "write", "set_", "convia", "wiki", "mark", "requeue",
                 "confirm", "prepare", "claim", "submit", "release", "ingest")
    assert not [nom for nom in noms if any(mot in nom for mot in interdits)]


def test_jeton_du_principal_refuse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAULT_CONTEXT_TOKEN", JETON)
    monkeypatch.setenv("VAULT_MCP_TOKEN", JETON)
    with pytest.raises(RuntimeError):
        contexte.construire_application()


def test_jeton_court_refuse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAULT_CONTEXT_TOKEN", "court")
    with pytest.raises(RuntimeError):
        contexte.construire_application()


@pytest.mark.parametrize("entete", [None, "Bearer mauvais-jeton", f"Basic {JETON}"])
def test_requete_sans_bon_jeton_rejetee(monkeypatch: pytest.MonkeyPatch, entete: str | None) -> None:
    monkeypatch.setenv("VAULT_CONTEXT_TOKEN", JETON)
    monkeypatch.delenv("VAULT_MCP_TOKEN", raising=False)
    application = contexte.construire_application()

    async def appeler() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test"
        ) as client:
            entetes = {"authorization": entete} if entete else {}
            return await client.post("/mcp", json={}, headers=entetes)

    assert asyncio.run(appeler()).status_code == 401
