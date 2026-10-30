"""Qualité des analyses ConvIA : résumé des erreurs d'outils, vue compactée."""

from __future__ import annotations

from vault_mcp import convia_view


def _tool(name: str, result: str) -> str:
    return (
        f"<details><summary>🔧 {name}</summary>\n\n```json\n{{}}\n```\n\n"
        f"**Résultat** :\n```\n{result}\n```\n\n</details>\n"
    )


POWERSHELL = (
    "---\nsource: opencode\ntitle: Photometrie\nconvia_sanitized: 2\n---\n\n# Photometrie\n\n"
    "## 👤 User — 2026-10-04 10:02:41\n\nliste les sessions\n\n"
    "## 🤖 Assistant — 2026-10-04 10:02:42\n\n"
    + _tool("read", "contenu ok")
    + _tool(
        "bash",
        "[31;1mhead: [31;1mLe terme « head » n’est pas reconnu comme nom d’applet de commande.",
    )
    + _tool("bash", "rtk: Failed to resolve 'ls'\nBinary 'ls' not found on PATH")
    + _tool("bash", "Exit code 2\nsomething")
    + _tool("bash", "Name  Length\nfoo   12")
    + "\nC'est fait.\n"
)


def test_french_powershell_and_exit_code_failures_are_detected() -> None:
    proj = convia_view.project(POWERSHELL)
    assert proj.tool_calls == {"read": 1, "bash": 4}
    assert proj.tool_errors == {"bash": 3}
    assert proj.last_tool == ("bash", False)


def test_tool_summary_heads_the_projection() -> None:
    view, _ = convia_view.project_markdown(POWERSHELL)
    summary_at = view.index("# Résumé des appels d'outils")
    assert summary_at < view.index("## User")
    assert "- appels : 5 ; en échec : 3 ; reprises réussies après échec : 1" in view
    assert "- outils en échec : bash ×3" in view
    assert "- échec bash — head: Le terme « head » n’est pas reconnu" in view
    assert "- dernier appel d'outil : bash — succès" in view
    # La couleur ANSI orpheline ne pollue pas le résumé.
    assert "[31;1m" not in view.split("## User")[0]


def test_no_tools_no_summary() -> None:
    view, _ = convia_view.project_markdown("## 👤 User — 2026-10-04 10:00:00\n\nallo\n")
    assert "Résumé des appels" not in view


def test_successful_grep_mentioning_error_word_mid_line_is_not_a_failure() -> None:
    raw = "## 🤖 Assistant — 2026-10-04 10:00:00\n\n" + _tool(
        "bash", "src/a.py: handle the error case"
    )
    assert convia_view.project(raw).tool_errors == {}


def _big_raw(n: int = 120) -> str:
    parts = ["---\nsource: opencode\ntitle: Gros\nconvia_sanitized: 2\n---\n\n# Gros\n"]
    for i in range(n):
        parts.append(
            f"## 👤 User — 2026-10-04 10:{i % 60:02d}:00\n\nDemande numero {i:03d} : "
            + "corrige le service " * 30
            + "\n"
        )
        parts.append(
            f"## 🤖 Assistant — 2026-10-04 10:{i % 60:02d}:30\n\nReponse numero {i:03d} "
            + "j'ai verifie la configuration " * 60
            + "\n"
            + _tool("bash", f"Exit code 1\nechec etape {i:03d}")
        )
    parts.append(
        "## 👤 User — 2026-10-04 11:00:00\n\nDERNIERE DEMANDE : valide le resultat final\n"
    )
    parts.append(
        "## 🤖 Assistant — 2026-10-04 11:00:30\n\nRESULTAT FINAL : service actif et teste.\n"
    )
    return "\n".join(parts)


def test_compact_view_fits_and_keeps_intent_frictions_outcome() -> None:
    view, _ = convia_view.project_markdown(_big_raw())
    assert len(view) > 60000
    out = convia_view.compact_view(view, 30000)
    assert len(out) <= 30000
    assert out.startswith("# Conversation — Gros")
    assert "# Résumé des appels d'outils" in out  # faits calculés conservés
    assert "Demande numero 000" in out and "Reponse numero 000" in out  # intention
    assert "RESULTAT FINAL : service actif et teste." in out  # issue
    assert "DERNIERE DEMANDE" in out
    assert (
        sum(f"Demande numero {i:03d}" in out for i in range(120)) > 20
    )  # frictions échantillonnées
    assert "message(s) omis" in out and "Vue compactée" in out
    assert "# Technical events relevant to analysis" in out


def test_compacted_excerpts_are_literal_in_the_full_projection() -> None:
    view, _ = convia_view.project_markdown(_big_raw())
    out = convia_view.compact_view(view, 30000)
    markers = ("[Vue compactée", "[… ", "- [… événements")
    for line in out.splitlines():
        if line.strip() and not line.startswith(markers):
            assert line in view, line[:80]


def test_small_view_is_untouched() -> None:
    view, _ = convia_view.project_markdown(POWERSHELL)
    assert convia_view.compact_view(view, 60000) == view


def test_compact_view_degenerate_single_huge_message() -> None:
    raw = "## 👤 User — 2026-10-04 10:00:00\n\n" + "x" * 5000 + "\n"
    view, _ = convia_view.project_markdown(raw)
    out = convia_view.compact_view(view, 3000)
    assert len(out) <= 3000 and "xxxx" in out
