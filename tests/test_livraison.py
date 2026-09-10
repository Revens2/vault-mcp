"""Invariants de livraison du serveur vault-mcp (lot 2 MCP v2).

Deux regressions constatees le 2026-09-10 sur le live sont verrouillees ici :

- V2 : `MCPServer` (SDK 2.x) a `version: str = ""` par defaut. La migration
  FastMCP -> MCPServer a donc fait annoncer `serverInfo.version = ""` a tous les
  clients. On exige une version de livraison vault non vide, egale a
  `vault_mcp.__version__`, et DISTINCTE de la version du SDK `mcp` (confondre les
  deux est l'erreur symetrique, commise puis corrigee sur tasks-mcp).
- V1 : `uvicorn.run` sans `timeout_graceful_shutdown` attend la fin des flux SSE
  GET /mcp ; systemd tue alors le service a TimeoutStopSec (90 s). Chaque
  restart/rollback finissait en SIGKILL (7 fois en 14 jours). L'arret doit rester
  borne et court.
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path
from typing import Any

import pytest

SECRET_DE_TEST = "x" * 32

# Un rollback doit tenir dans le RTO de la mission ; un arret plus long que cela
# redevient un SIGKILL systemd deguise.
ARRET_MAX_S = 10.0


@pytest.fixture
def serveur(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    monkeypatch.setenv("MCP_SECRET", SECRET_DE_TEST)
    monkeypatch.setenv("MCP_PORT", "8799")
    monkeypatch.setenv("COUCH_URL", "http://u:p@127.0.0.1:5984")
    monkeypatch.setenv("VAULT_MCP_ISSUER", "https://exemple.test")
    monkeypatch.setenv("VAULT_MCP_OAUTH_DIR", str(tmp_path / "oauth"))
    module = importlib.import_module("vault_mcp.server")
    return importlib.reload(module)


def test_version_de_livraison_annoncee_non_vide(serveur: Any) -> None:
    from vault_mcp import __version__

    assert __version__, "vault_mcp.__version__ ne doit jamais etre vide"
    # `MCPServer.version` alimente serverInfo.version cote protocole.
    assert serveur.mcp.version == __version__
    assert serveur.mcp._lowlevel_server.version == __version__


def test_version_annoncee_nest_pas_celle_du_sdk(serveur: Any) -> None:
    from importlib.metadata import version as version_paquet

    from vault_mcp import __version__

    # Regression symetrique : annoncer la version du SDK `mcp` renseigne le client
    # sur la bibliotheque, pas sur le service, et casse tout suivi de livraison.
    assert __version__ != version_paquet("mcp")


def test_version_du_paquet_suit_pyproject() -> None:
    from vault_mcp import __version__

    racine = Path(__file__).resolve().parent.parent
    pyproject = tomllib.loads((racine / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == __version__


def test_arret_borne_passe_a_uvicorn(serveur: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    appels: list[dict[str, Any]] = []

    def _run(app: Any, **kwargs: Any) -> None:
        appels.append(kwargs)

    # L'application complete ouvre l'index et le miroir : hors sujet ici.
    monkeypatch.setattr(serveur, "construire_application", lambda: object())
    monkeypatch.setattr(serveur.uvicorn, "run", _run)

    serveur.main()

    assert len(appels) == 1
    borne = appels[0].get("timeout_graceful_shutdown")
    assert borne is not None, "sans borne, systemd finit par SIGKILL a TimeoutStopSec"
    assert 0 < borne <= ARRET_MAX_S
