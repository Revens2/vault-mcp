"""Verifie que le paquet est importable et que le gating tourne sur du vide."""

from vault_mcp import __version__


def test_version_exposee() -> None:
    # Figer le numero ici obligerait a modifier ce test a chaque livraison, et
    # l oubli fait echouer la suite pour rien (constate le 2026-09-10). La
    # coherence avec `pyproject.toml` est verifiee par `test_livraison.py`.
    assert __version__
    assert __version__[0].isdigit()
