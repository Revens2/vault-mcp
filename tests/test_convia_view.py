"""Le compacteur doit garder le signal et jeter le bruit — vérifié sur une fixture adverse.

Composition imposée par le cahier des charges : 30 appels d'outils réussis, 2 en erreur,
une stack trace, un retry, une résolution, 3 prompts utilisateur, 3 réponses finales,
un faux jeton d'API, de la réflexion interne, une énorme sortie de terminal.
"""

from __future__ import annotations

from vault_mcp import convia_view

FAKE_TOKEN = "sk-proj-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"  # faux, jamais valide
FAKE_PASSWORD = "password: hunter2-tres-secret"
HUGE_STDOUT = "\n".join(f"ligne de sortie terminal numero {i:05d}" for i in range(2000))

TRACE = """Traceback (most recent call last):
  File "/opt/app/main.py", line 42, in <module>
    run()
  File "/opt/app/core.py", line 17, in run
    connect(dsn)
ConnectionRefusedError: [Errno 111] Connection refused"""


def _tool(name: str, args: str, result: str, error: bool = False) -> str:
    label = "**Erreur**" if error else "**Résultat**"
    return (
        f"<details><summary>🔧 {name}</summary>\n\n"
        f"```json\n{args}\n```\n\n"
        f"{label} :\n```\n{result}\n```\n\n</details>"
    )


def build_fixture() -> str:
    parts = [
        "---",
        "source: claude-cli",
        "session_id: 11111111-2222-3333-4444-555555555555",
        "title: Deploiement casse",
        "convia_sanitized: 2",
        "content_hash: sha256:deadbeef",
        "---",
        "",
        "# Deploiement casse",
        "",
        "## 👤 User — 2026-09-01 10:00:00",
        "",
        "Le deploiement echoue depuis ce matin, tu peux regarder ?",
        "",
        "## 🤖 Assistant — 2026-09-01 10:00:05 · claude-opus-5",
        "",
        "<details><summary>💭 Réflexion — 812 mots</summary>\n\n"
        "Je me demande si le port est ouvert, peut-etre que le pare-feu... "
        "raisonnement interne qui ne doit jamais sortir.\n\n</details>",
        "",
        "Je regarde l'etat du service.",
        "",
    ]
    # 30 appels d'outils qui réussissent : le bruit à faire disparaître.
    for i in range(30):
        parts.append(_tool("Read", f'{{"file": "/opt/app/f{i}.py"}}', f"contenu du fichier {i}"))
        parts.append("")
    # Une sortie de terminal énorme, réussie : bruit également.
    parts.append(_tool("Bash", '{"command": "journalctl -u app"}', HUGE_STDOUT))
    parts.append("")

    parts += [
        "## 👤 User — 2026-09-01 10:05:00",
        "",
        f"Voici ma cle si besoin : {FAKE_TOKEN} et {FAKE_PASSWORD}",
        "",
        "## 🤖 Assistant — 2026-09-01 10:05:10 · claude-opus-5",
        "",
        _tool("Bash", '{"command": "systemctl start app"}', TRACE, error=True),
        "",
        _tool("Bash", '{"command": "systemctl start app"}', "Job for app.service failed", error=True),
        "",
        "Le service refuse la connexion a la base : le DSN pointe sur le mauvais hote.",
        "",
        "## 👤 User — 2026-09-01 10:10:00",
        "",
        "Corrige et verifie.",
        "",
        "## 🤖 Assistant — 2026-09-01 10:10:20 · claude-opus-5",
        "",
        _tool("Bash", '{"command": "systemctl start app"}', "app.service: active (running)"),
        "",
        "Corrige : le DSN pointait sur l'ancien hote. Service actif, deploiement repasse.",
        "",
    ]
    return "\n".join(parts) + "\n"


RAW = build_fixture()
VIEW, STATS = convia_view.project_markdown(RAW)


def test_the_three_user_prompts_survive() -> None:
    assert VIEW.count("## User") == 3
    assert "Le deploiement echoue depuis ce matin" in VIEW
    assert "Corrige et verifie." in VIEW


def test_the_three_final_answers_survive() -> None:
    assert VIEW.count("## Assistant") == 3
    assert "Je regarde l'etat du service." in VIEW
    assert "le DSN pointe sur le mauvais hote" in VIEW
    assert "Service actif, deploiement repasse." in VIEW


def test_internal_reasoning_is_gone() -> None:
    assert "raisonnement interne" not in VIEW
    assert "💭" not in VIEW


def test_secrets_are_redacted() -> None:
    assert FAKE_TOKEN not in VIEW
    assert "hunter2" not in VIEW
    assert "<REDACTED_API_KEY>" in VIEW
    assert "<REDACTED_PASSWORD>" in VIEW


def test_the_thirty_successful_tool_calls_are_gone() -> None:
    assert "contenu du fichier 17" not in VIEW
    assert VIEW.count("outil Read") == 0


def test_the_huge_terminal_output_is_gone() -> None:
    assert "ligne de sortie terminal numero 01500" not in VIEW


def test_the_errors_are_kept() -> None:
    assert "ConnectionRefusedError" in VIEW
    assert "Job for app.service failed" in VIEW
    assert VIEW.count("erreur :") == 2


def test_the_resolution_is_kept() -> None:
    assert "reprise réussie après l'échec précédent" in VIEW


def test_the_stack_trace_is_truncated_but_readable() -> None:
    assert "Traceback (most recent call last)" in VIEW
    assert len(VIEW) < len(RAW)


def test_reduction_is_measured_and_large() -> None:
    assert STATS["tool_calls_seen"] == 34  # 30 Read + 4 Bash
    assert STATS["tool_calls_kept"] == 3
    assert STATS["reduction_pct"] > 90
    assert STATS["bytes_projection"] < STATS["bytes_raw"]


def test_projection_is_deterministic() -> None:
    again, stats_again = convia_view.project_markdown(RAW)
    assert again == VIEW
    assert stats_again == STATS


def test_an_empty_document_does_not_explode() -> None:
    view, stats = convia_view.project_markdown("")
    assert stats["messages"] == 0
    assert view.strip() == "# Conversation"


RUNTIME_NOISE = r"""---
source: codex
title: Audit perf
convia_sanitized: 2
---

# Audit perf

## 👤 User — 2026-08-22 19:00:00

<recommended_plugins>
- Airtable (airtable@openai-curated-remote)
- Spotify (spotify@openai-curated-remote)
</recommended_plugins>

<INSTRUCTIONS>
claude.md
</INSTRUCTIONS>

<environment_context>
  <cwd>C:\Users\Juliann\Desktop\Watchy</cwd>
  <shell>powershell</shell>
</environment_context>

<command-message>caveman</command-message>
<command-name>/caveman</command-name>
<command-args>fait un audit complet des performances</command-args>

## 🤖 Assistant — 2026-08-22 19:00:10 · gpt-5

Message Type: NEW_TASK
Task name: /root/infra_obs_perf
Sender: /root
Payload:

Je prends le volet infrastructure en lecture seule.
"""


def test_runtime_scaffolding_is_stripped_but_the_request_survives() -> None:
    view, _ = convia_view.project_markdown(RUNTIME_NOISE)
    # Ce que le runtime a injecte : dehors.
    assert "openai-curated-remote" not in view
    assert "powershell" not in view
    assert "Message Type: NEW_TASK" not in view
    assert "Sender: /root" not in view
    assert "<command-args>" not in view
    # Ce que la personne a reellement demande : intact.
    assert "fait un audit complet des performances" in view
    assert "Je prends le volet infrastructure en lecture seule." in view
