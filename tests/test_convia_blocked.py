"""Statut `blocked` ConvIA : sortie durable de file, sans perte ni faux compteur.

Une conversation que le consommateur ChatGPT ne peut définitivement pas lire
ne doit pas revenir éternellement en tête de backlog. `blocked` la gare :
plus servie par `list_pending`, source intacte, ligne conservée, auditée,
requeue admin possible, et toute NOUVELLE version de la source redevient
`pending` d'elle-même. Les compteurs `done` ne bougent jamais.
"""

from __future__ import annotations

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
"""

PATH = "raw/assets/ConvIA/Claude-CLI/2026-09-01_deploiement-casse_9f8e7d6c.md"


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


def test_blocked_leaves_the_pending_queue(env):
    mcp, queue, _conv = env
    out = mcp.mark_blocked(PATH, "refus plateforme répété à la lecture")
    assert out["blocked"] is True
    assert out["path"] == PATH
    assert queue.list_pending(limit=50) == []
    assert queue.pending_count() == 0
    assert queue.status()["analysis_pending"] == 0
    assert queue.status()["analysis_blocked"] == 1


def test_source_file_untouched_and_row_kept(env):
    mcp, queue, conv = env
    before = conv.read_text(encoding="utf-8")
    digest = queue.sha256_of(conv)
    mcp.mark_blocked(PATH, "illisible")
    # Source intacte, ligne conservée avec motif et horodatage.
    assert conv.read_text(encoding="utf-8") == before
    row = queue.find_entry(PATH, digest)
    assert row is not None and row["status"] == "blocked"
    assert row["blocked_reason"] == "illisible"
    assert row["blocked_at"]
    # Audit : l'action est tracée.
    events = queue.blocked_events(PATH)
    assert len(events) == 1 and events[0]["action"] == "blocked"


def test_requeue_restores_pending_with_same_identity(env):
    mcp, queue, conv = env
    digest = queue.sha256_of(conv)
    mcp.mark_blocked(PATH, "essai local")
    out = mcp.requeue_blocked(PATH)
    assert out["requeued"] is True
    items = queue.list_pending(limit=50)
    assert [i.source_hash for i in items] == [digest]
    # L'unité est à nouveau analysable normalement.
    prepared = mcp.prepare_analysis(PATH, digest, queue.ANALYSIS_VERSION, "# Analyse")
    assert prepared["duplicate"] is False
    # La cause du blocage reste lisible (historique), l'audit a deux entrées.
    assert queue.find_entry(PATH, digest)["blocked_reason"] == "essai local"
    assert [e["action"] for e in queue.blocked_events(PATH)] == ["requeued", "blocked"]


def test_new_source_version_becomes_pending_again(env):
    mcp, queue, conv = env
    old_digest = queue.sha256_of(conv)
    mcp.mark_blocked(PATH, "version A illisible")
    conv.write_text(RAW + "\n## 👤 User — 2026-09-02 09:00:00\n\nEt la suite ?\n",
                    encoding="utf-8")
    queue.scan()
    items = queue.list_pending(limit=50)
    assert len(items) == 1
    assert items[0].source_hash == queue.sha256_of(conv) != old_digest
    # L'ancienne version reste bloquée, jamais ressuscitée ni supprimée.
    assert queue.find_entry(PATH, old_digest)["status"] == "blocked"
    assert queue.status()["analysis_blocked"] == 1


def test_scan_never_resurrects_a_blocked_unit(env):
    mcp, queue, _conv = env
    mcp.mark_blocked(PATH, "durable")
    queue.scan()
    queue.scan()
    assert queue.list_pending(limit=50) == []
    assert queue.status()["analysis_blocked"] == 1


def test_done_counter_never_falsified(env):
    mcp, queue, conv = env
    assert queue.status()["analysis_done"] == 0
    mcp.mark_blocked(PATH, "pas un done")
    assert queue.status()["analysis_done"] == 0
    mcp.requeue_blocked(PATH)
    assert queue.status()["analysis_done"] == 0
    # Le seul chemin vers `done` reste une vraie analyse déposée.
    digest = queue.sha256_of(conv)
    prepared = mcp.prepare_analysis(PATH, digest, queue.ANALYSIS_VERSION, "# Analyse")
    assert mcp.confirm_analysis(PATH, digest, str(prepared["path"])) is True
    assert queue.status()["analysis_done"] == 1
    assert queue.status()["analysis_blocked"] == 0


def test_mark_blocked_requires_a_reason(env):
    mcp, _queue, _conv = env
    with pytest.raises(mcp.ConviaError):
        mcp.mark_blocked(PATH, "   ")


def test_mark_blocked_unknown_path_is_refused(env):
    mcp, _queue, _conv = env
    with pytest.raises(mcp.ConviaError, match="aucune analyse en attente"):
        mcp.mark_blocked("raw/assets/ConvIA/Claude-CLI/absent.md", "rien")


def test_requeue_with_nothing_blocked_is_refused(env):
    mcp, _queue, _conv = env
    with pytest.raises(mcp.ConviaError, match="aucune unité bloquée"):
        mcp.requeue_blocked(PATH)


def test_blocked_hides_legacy_older_pendings(env):
    """File héritée : deux `pending` pour le même chemin. Bloquer la plus
    récente retire aussi l'ancienne (superseded), sinon elle reviendrait en tête."""
    mcp, queue, conv = env
    rel = PATH
    conn = queue.connect()
    conn.execute("INSERT INTO pending_analysis (created_at, source_path, source_hash,"
                 " source_agent, version) VALUES ('2026-09-01T00:00:00Z', ?, ?, 'x', ?)",
                 (rel, "0" * 64, queue.ANALYSIS_VERSION))
    conn.commit()
    conn.close()
    mcp.mark_blocked(PATH, "ménage")
    assert queue.list_pending(limit=50) == []
    assert queue.pending_count() == 0


# ---------------------------------------------------------------------------
# Course list -> source modifiée -> mark (finding review post-merge PR #2) :
# le hash épingle la version visée, un hash périmé refuse sans rien bloquer.
# ---------------------------------------------------------------------------
def test_race_old_hash_refuses_and_current_version_stays_pending(env):
    """A(list, hash A) -> B(source modifiée + scan) -> mark_blocked(hash A) :
    refus explicite, B reste pending, rien n'est bloqué, done intact."""
    mcp, queue, conv = env
    hash_a = queue.sha256_of(conv)
    conv.write_text(RAW + "\n## 👤 User — 2026-09-02 09:00:00\n\nEt la suite ?\n",
                    encoding="utf-8")
    queue.scan()
    hash_b = queue.sha256_of(conv)
    assert hash_b != hash_a
    with pytest.raises(mcp.ConviaError, match="périmé"):
        mcp.mark_blocked(PATH, "refus qui concernait A", source_hash=hash_a)
    items = queue.list_pending(limit=50)
    assert [i.source_hash for i in items] == [hash_b]
    st = queue.status()
    assert st["analysis_blocked"] == 0 and st["analysis_done"] == 0
    assert queue.blocked_events(PATH) == []


def test_mark_with_matching_hash_blocks_the_intended_version(env):
    mcp, queue, conv = env
    digest = queue.sha256_of(conv)
    out = mcp.mark_blocked(PATH, "épinglé", source_hash=digest)
    assert out["blocked"] is True and out["source_hash"] == digest
    assert queue.status()["analysis_blocked"] == 1


def test_requeue_with_stale_hash_refuses_and_changes_nothing(env):
    """A bloqué, puis B bloqué : requeue(hash A) vise l'unité parquée la plus
    récente (B) -> refus, les deux restent bloquées, rien ne revient en file."""
    mcp, queue, conv = env
    hash_a = queue.sha256_of(conv)
    mcp.mark_blocked(PATH, "A illisible", source_hash=hash_a)
    conv.write_text(RAW + "\nsuite B\n", encoding="utf-8")
    queue.scan()
    hash_b = queue.sha256_of(conv)
    mcp.mark_blocked(PATH, "B illisible", source_hash=hash_b)
    with pytest.raises(mcp.ConviaError, match="périmé"):
        mcp.requeue_blocked(PATH, source_hash=hash_a)
    assert queue.list_pending(limit=50) == []
    assert queue.status()["analysis_blocked"] == 2
    assert queue.find_entry(PATH, hash_a)["status"] == "blocked"
    assert queue.find_entry(PATH, hash_b)["status"] == "blocked"


def test_requeue_with_matching_hash_requeues_only_it(env):
    mcp, queue, conv = env
    digest = queue.sha256_of(conv)
    mcp.mark_blocked(PATH, "pause", source_hash=digest)
    out = mcp.requeue_blocked(PATH, source_hash=digest)
    assert out["requeued"] is True and out["source_hash"] == digest
    assert [i.source_hash for i in queue.list_pending(limit=50)] == [digest]
