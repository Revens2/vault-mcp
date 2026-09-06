"""Surface MCP ConvIA : confinement, idempotence, et le raw qui ne sort jamais."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

RAW = """---
source: claude-cli
session_id: 9f8e7d6c-0000-1111-2222-333344445555
title: Deploiement casse
convia_sanitized: 2
---

# Deploiement casse

## 👤 User — 2026-09-01 10:00:00

Le deploiement echoue.

## 🤖 Assistant — 2026-09-01 10:00:05 · claude-opus-5

<details><summary>💭 Réflexion — 40 mots</summary>

secret de raisonnement interne

</details>

<details><summary>🔧 Read</summary>

```json
{"file": "/opt/app/x.py"}
```

**Résultat** :
```
contenu banal qui ne doit pas ressortir
```

</details>

Le DSN est faux.
"""


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    raw_root = tmp_path / "ConvIA"
    (raw_root / "Claude-CLI").mkdir(parents=True)
    conv = raw_root / "Claude-CLI" / "2026-09-01_deploiement-casse_9f8e7d6c.md"
    conv.write_text(RAW, encoding="utf-8")

    monkeypatch.setenv("CONVIA_QUEUE_DB", str(tmp_path / "q.db"))
    monkeypatch.setenv("CONVIA_RAW_ROOT", str(raw_root))
    import importlib

    from vault_mcp import convia_mcp, convia_queue
    importlib.reload(convia_queue)
    importlib.reload(convia_mcp)
    convia_queue.scan()
    return convia_mcp, convia_queue, conv


def test_pending_lists_the_conversation_with_projection_size(env):
    mcp, _queue, _conv = env
    out = mcp.list_pending(limit=5)
    assert out["pending_total"] == 1
    item = out["items"][0]
    assert item["source"] == "claude-cli"
    assert item["session_id"].startswith("9f8e7d6c")
    # Le dimensionnement annoncé est celui de la projection, pas du raw.
    assert 0 < item["projection_bytes"] < len(RAW)


def test_read_for_analysis_returns_the_projection_not_the_raw(env):
    mcp, _queue, _conv = env
    path = "raw/assets/ConvIA/Claude-CLI/2026-09-01_deploiement-casse_9f8e7d6c.md"
    out = mcp.read_for_analysis(path)
    body = out["content"]
    assert "Le deploiement echoue." in body
    assert "Le DSN est faux." in body
    assert "secret de raisonnement interne" not in body
    assert "contenu banal qui ne doit pas ressortir" not in body
    assert "convia_sanitized" not in body  # le frontmatter brut ne sort pas non plus
    assert out["metrics"]["tool_calls_kept"] == 0


@pytest.mark.parametrize(
    "bad",
    [
        "raw/assets/ConvIA/../../../etc/passwd",
        "../etc/passwd",
        "raw/assets/Autre/note.md",
        "wiki/sources/quelque-chose.md",
        "/etc/passwd",
        "",
        "raw/assets/ConvIA-Analysis/Claude-CLI/x.md",
    ],
)
def test_paths_outside_the_namespace_are_refused(env, bad):
    mcp, _queue, _conv = env
    with pytest.raises(mcp.ConviaError):
        mcp.read_for_analysis(bad)


def test_write_refuses_a_stale_hash(env):
    mcp, queue, conv = env
    path = "raw/assets/ConvIA/Claude-CLI/2026-09-01_deploiement-casse_9f8e7d6c.md"
    with pytest.raises(mcp.ConviaError, match="hash"):
        mcp.prepare_analysis(path, "0" * 64, queue.ANALYSIS_VERSION, "# Analyse\n\nok")


def test_write_refuses_an_unknown_version(env):
    mcp, queue, conv = env
    path = "raw/assets/ConvIA/Claude-CLI/2026-09-01_deploiement-casse_9f8e7d6c.md"
    digest = queue.sha256_of(conv)
    with pytest.raises(mcp.ConviaError, match="version"):
        mcp.prepare_analysis(path, digest, "convia-analysis-v99", "# Analyse")


def test_write_path_is_derived_not_chosen(env):
    mcp, queue, conv = env
    path = "raw/assets/ConvIA/Claude-CLI/2026-09-01_deploiement-casse_9f8e7d6c.md"
    digest = queue.sha256_of(conv)
    prepared = mcp.prepare_analysis(path, digest, queue.ANALYSIS_VERSION, "# Analyse\n\nRAS")
    assert prepared["path"] == (
        "raw/assets/ConvIA-Analysis/Claude-CLI/"
        "2026-09-01_deploiement-casse_9f8e7d6c__analyse.md"
    )
    assert "analysis_of: raw/assets/ConvIA/Claude-CLI/" in prepared["content"]
    assert f"source_sha256: {digest}" in prepared["content"]
    assert f"analysis_version: {queue.ANALYSIS_VERSION}" in prepared["content"]


def test_same_identity_is_never_processed_twice(env):
    mcp, queue, conv = env
    path = "raw/assets/ConvIA/Claude-CLI/2026-09-01_deploiement-casse_9f8e7d6c.md"
    digest = queue.sha256_of(conv)
    prepared = mcp.prepare_analysis(path, digest, queue.ANALYSIS_VERSION, "# Analyse")
    assert mcp.confirm_analysis(path, digest, str(prepared["path"])) is True

    with pytest.raises(mcp.ConviaError, match="déjà produite"):
        mcp.prepare_analysis(path, digest, queue.ANALYSIS_VERSION, "# Analyse bis")
    assert queue.status()["analysis_pending"] == 0


def test_a_modified_source_becomes_pending_again(env):
    mcp, queue, conv = env
    path = "raw/assets/ConvIA/Claude-CLI/2026-09-01_deploiement-casse_9f8e7d6c.md"
    digest = queue.sha256_of(conv)
    prepared = mcp.prepare_analysis(path, digest, queue.ANALYSIS_VERSION, "# Analyse")
    mcp.confirm_analysis(path, digest, str(prepared["path"]))

    conv.write_text(RAW + "\n## 👤 User — 2026-09-02 09:00:00\n\nEt la suite ?\n", encoding="utf-8")
    queue.scan()
    assert queue.status()["analysis_pending"] == 1
    # Le nouveau hash n'a pas d'analyse : il est légitimement analysable.
    mcp.prepare_analysis(path, queue.sha256_of(conv), queue.ANALYSIS_VERSION, "# Analyse 2")


def test_secrets_are_redacted_in_the_written_analysis(env):
    mcp, queue, conv = env
    path = "raw/assets/ConvIA/Claude-CLI/2026-09-01_deploiement-casse_9f8e7d6c.md"
    digest = queue.sha256_of(conv)
    prepared = mcp.prepare_analysis(
        path, digest, queue.ANALYSIS_VERSION,
        "# Analyse\n\nLa cle sk-proj-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA apparait.",
    )
    assert "sk-proj-AbCdEf" not in prepared["content"]
    assert "<REDACTED_API_KEY>" in prepared["content"]


def test_analysis_is_size_bounded(env):
    mcp, queue, conv = env
    path = "raw/assets/ConvIA/Claude-CLI/2026-09-01_deploiement-casse_9f8e7d6c.md"
    digest = queue.sha256_of(conv)
    with pytest.raises(mcp.ConviaError, match="trop longue"):
        mcp.prepare_analysis(path, digest, queue.ANALYSIS_VERSION, "x" * 200_000)


def test_status_flags_a_frozen_corpus(env):
    mcp, _queue, conv = env
    os.utime(conv, (0, 0))
    etat = mcp.status()
    assert etat["capture_fraiche"] is False
    assert any("figé" in e for e in etat["active_errors"])
