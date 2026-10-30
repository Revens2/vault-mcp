"""Dossier d'analyse ConvIA : même casse que le dossier source."""

from __future__ import annotations

from vault_mcp import convia_mcp


def test_opencode_analysis_lands_in_the_source_folder_casing() -> None:
    src = "raw/assets/ConvIA/OpenCode/2026-10-04_x_04de9d404ffe.md"
    assert convia_mcp.analysis_path_for(src, "opencode", "x") == (
        "raw/assets/ConvIA-Analysis/OpenCode/2026-10-04_x_04de9d404ffe__analyse.md"
    )


def test_unknown_agent_follows_the_source_folder() -> None:
    src = "raw/assets/ConvIA/NouvelAgent/2026-10-04_x_abcd1234.md"
    assert convia_mcp.analysis_path_for(src, "nouvel-agent", "x").startswith(
        "raw/assets/ConvIA-Analysis/NouvelAgent/"
    )


def test_known_agents_unchanged() -> None:
    src = "raw/assets/ConvIA/Claude-CLI/2026-10-04_x_abcd1234.md"
    assert convia_mcp.analysis_path_for(src, "claude-cli", "x") == (
        "raw/assets/ConvIA-Analysis/Claude-CLI/2026-10-04_x_abcd1234__analyse.md"
    )
