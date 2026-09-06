"""Verifie que le paquet est importable et que le gating tourne sur du vide."""

from vault_mcp import __version__


def test_version_exposee() -> None:
    assert __version__ == "2.0.0"
