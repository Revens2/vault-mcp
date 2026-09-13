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

    from vault_mcp import convia_mcp, convia_queue, wiki_jobs
    importlib.reload(convia_queue)
    # wiki_jobs fige WIKI_JOBS_DB a l import : le recharger sous l environnement isole,
    # sinon `status()` lit la file Wiki de production.
    importlib.reload(wiki_jobs)
    importlib.reload(convia_mcp)
    convia_queue.scan()
    return convia_mcp, convia_queue, conv


# ---------------------------------------------------------------------------
# Fichiers mal places a la racine ConvIA (mission 5, canary E2E du 2026-09-07)
# ---------------------------------------------------------------------------
def _scan_with(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
               files: dict[str, str]):
    raw_root = tmp_path / "ConvIA"
    for rel, content in files.items():
        p = raw_root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    monkeypatch.setenv("CONVIA_QUEUE_DB", str(tmp_path / "q.db"))
    monkeypatch.setenv("CONVIA_RAW_ROOT", str(raw_root))
    import importlib
    from vault_mcp import convia_queue
    importlib.reload(convia_queue)
    stats = convia_queue.scan()
    return convia_queue, raw_root, stats


def test_md_a_la_racine_convia_ignore_pas_source(monkeypatch, tmp_path):
    """`raw/assets/ConvIA/foo.md` (canary, artefact) : ignore, compte comme mal
    place, JAMAIS une source ni un pending (source fantome « ConvIA »)."""
    queue, _root, stats = _scan_with(tmp_path, monkeypatch, {
        "CanaryE2E-m4-20260907T153010Z.md": "# canary inoffensif\n",
        "claude-cli/2026-09-01_conversation.md": "contenu sans frontmatter\n",
    })
    assert stats["mal_places"] == 1
    assert stats["vus"] == 1, "seule la conversation imbriquee est vue"
    pending = queue.list_pending(limit=10)
    agents = {item.source_agent for item in pending}
    assert "ConvIA" not in agents
    assert "ConvIA" not in queue.status()["pending_by_source"]
    assert "claude-cli" in agents
    assert queue._corpus_count() == 1, "le .md racine ne compte pas dans le corpus"


def test_md_dans_un_dossier_source_accepte(monkeypatch, tmp_path):
    """`ConvIA/<source>/<fichier>.md` reste accepte (source = dossier parent si
    pas de frontmatter), sans liste fermee de providers."""
    queue, _root, stats = _scan_with(tmp_path, monkeypatch, {
        "claude-cli/2026-09-01_conversation.md": "contenu\n",
        "une-nouvelle-source/2026-09-02_futur.md": "contenu\n",
    })
    assert stats["mal_places"] == 0
    pending = queue.list_pending(limit=10)
    agents = sorted({item.source_agent for item in pending})
    assert "claude-cli" in agents
    assert "une-nouvelle-source" in agents


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

    # Rejeu (reponse MCP perdue) : meme identite -> meme resultat, aucun contenu a deposer.
    replay = mcp.prepare_analysis(path, digest, queue.ANALYSIS_VERSION, "# Analyse bis")
    assert replay["duplicate"] is True
    assert replay["path"] == prepared["path"]
    assert "content" not in replay
    assert mcp.confirm_analysis(path, digest, str(prepared["path"])) is False
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
    assert "sk-proj-" not in prepared["content"]
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


def test_an_analysis_that_never_reached_the_mirror_returns_to_the_queue(env, tmp_path, monkeypatch):
    """Un depot accepte par le spool n'est pas encore une note sur le Drive."""
    mcp, queue, conv = env
    path = "raw/assets/ConvIA/Claude-CLI/2026-09-01_deploiement-casse_9f8e7d6c.md"
    digest = queue.sha256_of(conv)
    prepared = mcp.prepare_analysis(path, digest, queue.ANALYSIS_VERSION, "# Analyse")
    mcp.confirm_analysis(path, digest, str(prepared["path"]))
    assert queue.status()["analysis_pending"] == 0

    mirror = tmp_path / "mirror"
    monkeypatch.setattr(queue, "MIRROR_ROOT", mirror)

    # Encore frais : on laisse le pousseur travailler, aucune reprise.
    assert queue.reconcile_lost_analyses() == 0
    assert queue.status()["analysis_pending"] == 0

    # Passe le delai, toujours rien dans le miroir : l'analyse est perdue.
    monkeypatch.setattr(queue, "RECONCILE_AFTER_S", -1)
    assert queue.reconcile_lost_analyses() == 1
    assert queue.status()["analysis_pending"] == 1


def test_an_analysis_present_in_the_mirror_stays_done(env, tmp_path, monkeypatch):
    mcp, queue, conv = env
    path = "raw/assets/ConvIA/Claude-CLI/2026-09-01_deploiement-casse_9f8e7d6c.md"
    digest = queue.sha256_of(conv)
    prepared = mcp.prepare_analysis(path, digest, queue.ANALYSIS_VERSION, "# Analyse")
    mcp.confirm_analysis(path, digest, str(prepared["path"]))

    mirror = tmp_path / "mirror"
    cible = mirror / str(prepared["path"])
    cible.parent.mkdir(parents=True)
    cible.write_text("# Analyse", encoding="utf-8")
    monkeypatch.setattr(queue, "MIRROR_ROOT", mirror)
    monkeypatch.setattr(queue, "RECONCILE_AFTER_S", -1)

    assert queue.reconcile_lost_analyses() == 0
    assert queue.status()["analysis_pending"] == 0


# ------------------------------------------------------- wiki_ingest_* (lot 2026-09-06)
def test_un_oneshot_en_activating_compte_comme_en_cours(env, monkeypatch):
    """`llm-wiki-ingest.service` est un Type=oneshot : il ne passe JAMAIS par
    `active`, il reste `activating (start)` pendant tout le run. Garde conservee
    pour le flag best-effort `running` du statut (le worker LLM lui-meme est
    retire : `ingest_start` rend toujours `deprecated`)."""
    mcp, _, _ = env
    monkeypatch.setattr(mcp, "_systemctl", lambda *a, **k: (0, "ActiveState=activating"))
    assert mcp._is_running() is True
    assert mcp.ingest_start()["state"] == "deprecated"


def test_un_oneshot_inactive_ne_compte_pas_comme_en_cours(env, monkeypatch):
    mcp, _, _ = env
    monkeypatch.setattr(mcp, "_systemctl", lambda *a, **k: (0, "ActiveState=inactive"))
    assert mcp._is_running() is False


def test_ingest_start_depose_une_demande_et_n_escalade_jamais(env, tmp_path, monkeypatch):
    """Bascule ChatGPT-seul : l'ancien worker LLM ne demarre plus. `ingest_start`
    rend `deprecated` SANS toucher au disque ni lancer le moindre sous-processus
    (ni sudo, ni marqueur, ni systemctl start). Ce test echoue si quelqu un
    rebranche un demarrage ou un ecriture."""
    mcp, _, _ = env
    marqueur = tmp_path / "wiki-ingest.request"
    monkeypatch.setattr(mcp, "INGEST_REQUEST", marqueur)

    def interdit(*a, **k):  # pragma: no cover - doit ne jamais etre appele
        raise AssertionError("ingest_start ne doit lancer aucun sous-processus")

    monkeypatch.setattr(mcp.subprocess, "run", interdit)

    resultat = mcp.ingest_start()
    assert resultat["state"] == "deprecated"
    assert resultat["deprecated"] is True
    assert not marqueur.exists()


def test_ingest_start_signale_un_depot_impossible(env, tmp_path, monkeypatch):
    """Meme sur un filesystem hostile, le stub deprecated ne fait rien et rend
    `deprecated` (aucun depot tente, donc aucun echec de depot possible)."""
    mcp, _, _ = env
    monkeypatch.setattr(mcp, "INGEST_REQUEST", tmp_path / "fichier" / "x" / "req")
    (tmp_path / "fichier").write_text("pas un dossier", encoding="utf-8")
    assert mcp.ingest_start()["state"] == "deprecated"


# ---------------------------------------------------------------------------
# File polluee par les versions mortes (constat production 2026-09-12)
# ---------------------------------------------------------------------------
def test_versions_anterieures_retirees_de_la_file(env):
    mcp, queue, conv = env
    for k in range(3):
        conv.write_text(RAW + f"\nsuite {k}\n", encoding="utf-8")
        queue.scan()
    items = queue.list_pending(limit=50)
    assert len(items) == 1
    assert items[0].source_hash == queue.sha256_of(conv)
    st = queue.status()
    assert st["analysis_pending"] == 1 and st["analysis_superseded"] == 3
    assert queue.pending_count() == 1


def test_rattrapage_d_une_file_existante_deja_polluee(env):
    mcp, queue, conv = env
    rel = "raw/assets/ConvIA/Claude-CLI/2026-09-01_deploiement-casse_9f8e7d6c.md"
    conn = queue.connect()
    for h in ("1" * 64, "2" * 64):  # lignes heritees de l ancien scan, jamais retirees
        conn.execute("INSERT INTO pending_analysis (created_at, source_path, source_hash,"
                     " source_agent, version) VALUES ('2026-09-01T00:00:00Z', ?, ?, 'x', ?)",
                     (rel, h, queue.ANALYSIS_VERSION))
    conn.commit()
    conn.close()
    queue.scan()
    items = queue.list_pending(limit=50)
    assert [i.source_hash for i in items] == [queue.sha256_of(conv)]


def test_analyse_sur_hash_non_encore_scanne_fait_bouger_la_file(env):
    mcp, queue, conv = env
    path = "raw/assets/ConvIA/Claude-CLI/2026-09-01_deploiement-casse_9f8e7d6c.md"
    conv.write_text(RAW + "\nnouveau\n", encoding="utf-8")  # pas de scan
    digest = queue.sha256_of(conv)
    prepared = mcp.prepare_analysis(path, digest, queue.ANALYSIS_VERSION, "# A")
    assert mcp.confirm_analysis(path, digest, str(prepared["path"]),
                                str(prepared["source_agent"])) is True
    assert queue.list_pending(limit=50) == []
    assert queue.pending_count() == 0


def test_retour_a_une_version_deja_connue_reste_servi(env):
    mcp, queue, conv = env
    a = conv.read_text(encoding="utf-8")
    conv.write_text(a + "\nversion B\n", encoding="utf-8")
    queue.scan()
    conv.write_text(a, encoding="utf-8")  # A -> B -> A
    queue.scan()
    items = queue.list_pending(limit=50)
    assert [i.source_hash for i in items] == [queue.sha256_of(conv)]
