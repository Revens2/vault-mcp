"""Invariants de livraison du serveur vault-mcp (lot 2 MCP v2).

Deux regressions constatees le 2026-09-10 sur le live sont verrouillees ici :

- V2 : `MCPServer` (SDK 2.x) a `version: str = ""` par defaut. La migration
  FastMCP -> MCPServer a donc fait annoncer `serverInfo.version = ""` a tous les
  clients. On exige une version de livraison vault non vide, egale a
  `vault_mcp.__version__`, et DISTINCTE de la version du SDK `mcp` (confondre les
  deux est l'erreur symetrique, commise puis corrigee sur tasks-mcp).
- V1 : l arret doit rester borne et court, EN DEUX ETAGES.
  `timeout_graceful_shutdown` borne l attente des CONNEXIONS (flux SSE GET /mcp).
  Mais la mesure sur candidat du 2026-09-10 a montre qu il ne borne PAS l arret du
  gestionnaire de sessions MCP : avec un appel d outil SYNCHRONE en vol, l arret
  dure ~54 s dont ~53 s dans le lifespan. Les outils du vault sont synchrones et
  executes dans un thread, que Python ne sait pas interrompre : il faut donc un
  chien de garde qui sorte en dur passe le delai. Sans lui, systemd tue le service
  a TimeoutStopSec (90 s) et chaque restart finit en SIGKILL (7 fois en 14 jours).
"""

from __future__ import annotations

import importlib
import threading
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
    """Etage 1 : l attente des connexions (flux SSE) est bornee."""
    configs: list[Any] = []
    demarres: list[Any] = []

    class _ServeurFactice:
        def __init__(self, config: Any) -> None:
            configs.append(config)
            self.should_exit = False

        def run(self) -> None:
            demarres.append(self)

    # L'application complete ouvre l'index et le miroir : hors sujet ici.
    monkeypatch.setattr(serveur, "construire_application", lambda: object())
    monkeypatch.setattr(serveur.uvicorn, "Server", _ServeurFactice)
    monkeypatch.setattr(serveur.threading, "Thread", _ThreadFactice)

    serveur.main()

    assert len(configs) == 1
    borne = configs[0].timeout_graceful_shutdown
    assert borne is not None, "sans borne, uvicorn attend la fin des flux SSE"
    assert 0 < borne <= ARRET_MAX_S
    assert demarres, "le serveur doit etre demarre"


class _ThreadFactice:
    """Enregistre le chien de garde sans le lancer (il ne finirait jamais)."""

    lances: list[tuple[Any, tuple[Any, ...]]] = []

    def __init__(self, target: Any = None, args: tuple[Any, ...] = (), **kwargs: Any) -> None:
        self._cible, self._args, self.daemon = target, args, kwargs.get("daemon", False)

    def start(self) -> None:
        _ThreadFactice.lances.append((self._cible, self._args))


def test_le_chien_de_garde_est_arme_en_demon(serveur: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _ThreadFactice.lances.clear()

    class _ServeurFactice:
        def __init__(self, config: Any) -> None:
            self.should_exit = False

        def run(self) -> None:
            return None

    monkeypatch.setattr(serveur, "construire_application", lambda: object())
    monkeypatch.setattr(serveur.uvicorn, "Server", _ServeurFactice)
    monkeypatch.setattr(serveur.threading, "Thread", _ThreadFactice)

    serveur.main()

    assert len(_ThreadFactice.lances) == 1
    cible, _ = _ThreadFactice.lances[0]
    assert cible is serveur._sortie_bornee


def test_le_chien_de_garde_sort_en_dur_si_l_arret_traine(
    serveur: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Etage 2 : un appel d outil synchrone en vol ne peut PAS etre interrompu.

    Sans cette sortie forcee, l arret dure ~54 s (mesure sur candidat) et
    systemd finit par SIGKILL. Le code de sortie doit rester 0 pour ne pas
    declencher la notification `OnFailure` de l unite.
    """
    codes: list[int] = []
    monkeypatch.setattr(serveur.os, "_exit", codes.append)

    class _ServeurQuiTraine:
        should_exit = True  # arret demande...

    serveur._sortie_bornee(_ServeurQuiTraine(), delai=0.05, pas=0.01)  # ...mais jamais abouti

    assert codes == [0]


def test_le_chien_de_garde_ne_sort_pas_avant_le_signal(
    serveur: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    codes: list[int] = []
    monkeypatch.setattr(serveur.os, "_exit", codes.append)

    class _ServeurQuiTourne:
        should_exit = False

    fil = threading.Thread(
        target=serveur._sortie_bornee,
        args=(_ServeurQuiTourne(),),
        kwargs={"delai": 0.01, "pas": 0.01},
        daemon=True,
    )
    fil.start()
    fil.join(timeout=0.3)

    assert codes == [], "aucune sortie tant que l arret n a pas ete demande"
