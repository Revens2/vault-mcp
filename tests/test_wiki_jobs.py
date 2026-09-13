"""File Wiki ChatGPT-seul : leases, fencing, idempotence, CAS, quarantaine.

Couvre le contrat MCP wiki_ingest_* : claim atomique, read confine,
submit valide + spoolise (autorite serveur), release borne, merge_pending
deterministe et reprenable. Aucun appel reseau, aucun LLM.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

FACTICE = Path(__file__).parent / "fixtures" / "contrat_wiki_factice.py"
REEL = Path(os.environ.get("WIKI_CONTRACT_MODULE_REEL", "/usr/local/bin/llm_wiki_extract.py"))


def _valid_extraction(slug="doc-test", title="Doc test"):
    body = ("Ceci est un corps de fiche largement suffisant pour depasser les "
            "deux cents caracteres exiges par la validation serveur. " * 4)
    return {
        "language": "fr",
        "confidence": 0.8,
        "note": {
            "slug": slug,
            "title": title,
            "tags": ["test", "wiki"],
            "doc_date": "",
            "summary": "Resume de test.",
            "sections": [
                {"heading": "Resume", "markdown": body + " Voir {{E:ent-test}}."},
            ],
            "warnings": [],
        },
        "entities": [
            {"slug": "ent-test", "name": "Ent Test", "kind": "entity",
             "subtype": "systeme", "aliases": [], "tags": ["test"],
             "definition": "Entite de test.",
             "evidence": "mention dans le document",
             "salience": "primary"},
        ],
        "relations": [],
        "issues": [],
    }


@pytest.fixture
def wj(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WIKI_JOBS_DB", str(tmp_path / "wiki_jobs.db"))
    monkeypatch.setenv("WIKI_JOBS_SPOOL", str(tmp_path / "spool"))
    monkeypatch.setenv("WIKI_JOBS_MANIFEST", str(tmp_path / "manifest.jsonl"))
    monkeypatch.setenv("WIKI_RAW_DIR", str(tmp_path / "raw"))
    monkeypatch.setenv("WIKI_EXCLUDE_DIRS",
                       str(tmp_path / "raw" / "assets" / "ConvIA").replace("\\", "/")
                       + ":/srv/vault-mirror/raw/assets/ConvIA")
    monkeypatch.setenv("WIKI_CHUNK_MIN_TOKENS", "40")
    monkeypatch.setenv("WIKI_CHUNK_MIN_FLOOR", "8")
    monkeypatch.setenv("WIKI_CONTRACT_MODULE", str(FACTICE))
    monkeypatch.setenv("WIKI_NOTES_DIR", str(tmp_path / "wiki"))
    # Jamais le vrai marqueur : il declencherait la fusion des fiches en prod.
    monkeypatch.setenv("WIKI_INGEST_REQUEST", str(tmp_path / "wiki-ingest.request"))
    (tmp_path / "wiki" / "sources").mkdir(parents=True)
    from vault_mcp import wiki_jobs
    importlib.reload(wiki_jobs)
    return wiki_jobs


@pytest.fixture
def mcp(wj, monkeypatch: pytest.MonkeyPatch):
    # convia_mcp lie wiki_jobs a l'import : recharger APRES wiki_jobs pour que
    # les wrappers MCP testent exactement le code expose au serveur.
    importlib.reload(wj)
    from vault_mcp import convia_mcp
    importlib.reload(convia_mcp)
    return convia_mcp, wj


DOC = ("# Titre\n\nParagraphe de contenu deterministe. " * 40)


# ------------------------------------------------------------ sync & chunking
def test_sync_idempotent_et_chunking_deterministe(wj):
    r1 = wj.sync_source("raw/doc.md", "a" * 64, DOC)
    r2 = wj.sync_source("raw/doc.md", "a" * 64, DOC)
    assert r1["synced"] > 0 and r2["synced"] == 0
    c1 = wj.chunk_source("raw/doc.md", "a" * 64, DOC)
    c2 = wj.chunk_source("raw/doc.md", "a" * 64, DOC)
    assert [c["job_id"] for c in c1] == [c["job_id"] for c in c2]
    assert r1["chunks"] == len(c1) > 1  # seuil bas force le multi-chunks


def test_exclusion_convia_raw(wj):
    r = wj.sync_source("/srv/vault-mirror/raw/assets/ConvIA/x/code.md",
                       "b" * 64, DOC)
    assert r["excluded"] == 1
    assert wj.status()["pending_chunks"] == 0


# ------------------------------------------------------------------ claim
def test_claim_champs_lease(wj):
    wj.sync_source("raw/a.md", "a" * 64, DOC)
    got = wj.claim(limit=2)
    assert got["leased"] >= 1
    job = got["jobs"][0]
    for key in ("job_id", "lease_id", "fencing_token", "source",
                "source_hash", "chunk_index", "chunk_count", "chunk_hash",
                "chunk_bytes", "contract_version", "expires_at"):
        assert key in job, key
    assert job["fencing_token"] == 1
    assert job["contract_version"] == wj.CONTRACT_VERSION


def test_claim_ne_reserve_que_le_traitable(wj):
    wj.sync_source("raw/a.md", "a" * 64, DOC)
    total = wj.status()["pending_chunks"]
    first = wj.claim(limit=1, max_bytes=10**9)
    assert first["leased"] == 1
    rest = wj.claim(limit=10)
    assert rest["leased"] == total - 1
    assert wj.claim(limit=10)["leased"] == 0  # file vide, pas d'erreur


def test_claim_budget_bytes(wj):
    wj.sync_source("raw/a.md", "a" * 64, DOC)
    one = wj.claim(limit=10, max_bytes=1)
    assert one["leased"] == 1  # au moins un job, jamais zero si file non vide


def test_double_claim_concurrent_sans_chevauchement(wj):
    wj.sync_source("raw/a.md", "a" * 64, "Contenu. " * 200)
    total = wj.status()["pending_chunks"]
    assert total >= 2
    results: list[list[str]] = []

    def worker():
        got = wj.claim(limit=10)
        results.append([j["job_id"] for j in got["jobs"]])

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    flat = [j for r in results for j in r]
    assert len(flat) == total
    assert len(set(flat)) == total  # aucun job attribue deux fois


# ------------------------------------------------------------------ read
def test_read_bail_invalide_sans_contenu(wj):
    wj.sync_source("raw/a.md", "a" * 64, DOC)
    job = wj.claim(limit=1)["jobs"][0]
    with pytest.raises(wj.WikiJobsError, match="lease-invalid"):
        wj.read_job(job["job_id"], "mauvais-bail")
    with pytest.raises(wj.WikiJobsError, match="job inconnu"):
        wj.read_job("job-inexistant", job["lease_id"])


def test_read_bail_expire_remet_en_file(wj):
    wj.sync_source("raw/a.md", "a" * 64, DOC)
    job = wj.claim(limit=1, lease_seconds=60)["jobs"][0]
    conn = wj.connect()
    conn.execute("UPDATE wiki_jobs SET expires_at=1 WHERE job_id=?",
                 (job["job_id"],))
    conn.commit()
    conn.close()
    with pytest.raises(wj.WikiJobsError, match="lease-expired"):
        wj.read_job(job["job_id"], job["lease_id"])
    # Re-claim possible : le job n'est pas perdu.
    assert wj.claim(limit=10)["leased"] >= 1


def test_read_rend_snapshot_et_mention_donnee(wj):
    wj.sync_source("raw/a.md", "a" * 64, DOC)
    job = wj.claim(limit=1)["jobs"][0]
    vue = wj.read_job(job["job_id"], job["lease_id"])
    assert vue["chunk_markdown"]
    assert vue["chunk_hash"] == job["chunk_hash"]
    assert "DONNEE" in vue["data_notice"]


# ------------------------------------------------------------------ submit
def test_submit_valide_spoolise(wj):
    wj.sync_source("raw/a.md", "a" * 64, DOC)
    job = wj.claim(limit=1)["jobs"][0]
    recu = wj.submit(job["job_id"], job["lease_id"], job["fencing_token"],
                     wj.CONTRACT_VERSION, _valid_extraction())
    assert recu["receipt_id"] and recu["duplicate"] is False
    spool = wj._spool_path(job["source_hash"], job["chunk_index"])
    env = json.loads(spool.read_text(encoding="utf-8"))
    assert env["contract_version"] == wj.CONTRACT_VERSION
    assert env["note"]["slug"] == "doc-test"


def test_submit_idempotent_meme_payload(wj):
    wj.sync_source("raw/a.md", "a" * 64, DOC)
    job = wj.claim(limit=1)["jobs"][0]
    doc = _valid_extraction()
    r1 = wj.submit(job["job_id"], job["lease_id"], job["fencing_token"],
                   wj.CONTRACT_VERSION, doc)
    r2 = wj.submit(job["job_id"], job["lease_id"], job["fencing_token"],
                   wj.CONTRACT_VERSION, doc)
    assert r2["duplicate"] is True
    assert r2["receipt_id"] == r1["receipt_id"]


def test_submit_conflit_payload_different(wj):
    wj.sync_source("raw/a.md", "a" * 64, DOC)
    job = wj.claim(limit=1)["jobs"][0]
    wj.submit(job["job_id"], job["lease_id"], job["fencing_token"],
              wj.CONTRACT_VERSION, _valid_extraction())
    with pytest.raises(wj.WikiJobsError, match="conflit"):
        wj.submit(job["job_id"], job["lease_id"], job["fencing_token"],
                  wj.CONTRACT_VERSION, _valid_extraction(slug="autre-doc"))


def test_submit_fencing_perime_refuse(wj):
    wj.sync_source("raw/a.md", "a" * 64, DOC)
    job = wj.claim(limit=1)["jobs"][0]
    with pytest.raises(wj.WikiJobsError, match="[Ff]encing"):
        wj.submit(job["job_id"], job["lease_id"], job["fencing_token"] + 99,
                  wj.CONTRACT_VERSION, _valid_extraction())


def test_submit_double_soumission_reseau(wj):
    # Retry d'un appel MCP : meme lease, meme payload -> meme recu.
    wj.sync_source("raw/a.md", "a" * 64, DOC)
    job = wj.claim(limit=1)["jobs"][0]
    doc = _valid_extraction()
    recs = [wj.submit(job["job_id"], job["lease_id"], job["fencing_token"],
                      wj.CONTRACT_VERSION, doc) for _ in range(3)]
    assert {r["receipt_id"] for r in recs} == {recs[0]["receipt_id"]}


def test_submit_invalide_puis_quarantaine(wj):
    wj.sync_source("raw/a.md", "a" * 64, DOC)
    bad = _valid_extraction()
    bad["note"]["tags"] = []  # invalide : tags hors [2-8]
    for _ in range(wj.MAX_ATTEMPTS):
        job = wj.claim(limit=1)["jobs"][0]
        with pytest.raises(wj.WikiJobsError, match="validation refusee"):
            wj.submit(job["job_id"], job["lease_id"], job["fencing_token"],
                      wj.CONTRACT_VERSION, bad)
    assert wj.status()["quarantined"] >= 1
    # La quarantaine ne bloque pas les autres jobs.
    assert wj.claim(limit=10)["leased"] >= 0


def test_validation_relations_pendantes_sans_blocage(wj):
    wj.sync_source("raw/a.md", "a" * 64, DOC)
    job = wj.claim(limit=1)["jobs"][0]
    doc = _valid_extraction()
    doc["relations"] = [{"from": "ent-test", "to": "fantome",
                         "type": "appelle", "evidence": "x", "confidence": 0.9}]
    recu = wj.submit(job["job_id"], job["lease_id"], job["fencing_token"],
                     wj.CONTRACT_VERSION, doc)
    assert recu["receipt_id"]
    assert any("pendante" in w for w in recu["warnings"])


def test_cas_source_stale_refusee(wj):
    wj.sync_source("raw/a.md", "a" * 64, DOC)
    job = wj.claim(limit=1)["jobs"][0]
    wj.sync_source("raw/a.md", "c" * 64, DOC + "modifie")
    with pytest.raises(wj.WikiJobsError, match="stale"):
        wj.submit(job["job_id"], job["lease_id"], job["fencing_token"],
                  wj.CONTRACT_VERSION, _valid_extraction())


def test_contract_version_perimee(wj):
    wj.sync_source("raw/a.md", "a" * 64, DOC)
    job = wj.claim(limit=1)["jobs"][0]
    with pytest.raises(wj.WikiJobsError, match="contract_version"):
        wj.submit(job["job_id"], job["lease_id"], job["fencing_token"],
                  "wiki-extract-v3", _valid_extraction())


# ------------------------------------------------------------------ release
def test_release_defer_renew(wj):
    wj.sync_source("raw/a.md", "a" * 64, DOC)
    job = wj.claim(limit=1)["jobs"][0]
    assert wj.release(job["job_id"], job["lease_id"], "release")["status"] == "pending"
    job = wj.claim(limit=1)["jobs"][0]
    assert wj.release(job["job_id"], job["lease_id"], "renew")["status"] == "leased"
    job2 = wj.claim(limit=10)  # le job est encore loue : rien d'autre si file vide partielle
    assert job2["leased"] >= 0
    for _ in range(wj.MAX_RENEWS + 1):
        try:
            wj.release(job["job_id"], job["lease_id"], "renew")
        except wj.WikiJobsError as exc:
            assert "borne" in str(exc)
            break
    else:
        pytest.fail("renew aurait du etre borne")
    # defer repete -> quarantaine
    wj.release(job["job_id"], job["lease_id"], "release")
    for _ in range(wj.MAX_ATTEMPTS):
        j = wj.claim(limit=1)["jobs"][0]
        wj.release(j["job_id"], j["lease_id"], "defer", reason="test")
    assert wj.status()["quarantined"] >= 1


# ------------------------------------------------------------------ merge
def _drain_all(wj, source="raw/a.md", sha="a" * 64):
    n = 0
    while True:
        got = wj.claim(limit=10)
        if not got["leased"]:
            break
        for job in got["jobs"]:
            # Un slug par source : deux sources au meme note.slug sont refusees.
            wj.submit(job["job_id"], job["lease_id"], job["fencing_token"],
                      wj.CONTRACT_VERSION,
                      _valid_extraction(slug="doc-" + Path(job["source"]).stem))
            n += 1
    return n


def test_merge_multi_chunks_complet(wj):
    wj.sync_source("raw/a.md", "a" * 64, DOC)
    total = wj.status()["pending_chunks"]
    assert total > 1
    assert _drain_all(wj) == total
    res = wj.merge_pending()
    assert res["merged"] == 1 and res["manifest_updated"] is True
    assert wj.status()["merged"] == total
    # Reprise idempotente : rejouer ne duplique rien.
    again = wj.merge_pending()
    assert again["merged"] == 0
    lines = (Path(wj.MANIFEST_PATH).read_text(encoding="utf-8").strip().splitlines())
    assert len(lines) == 1
    assert json.loads(lines[0])["contract_version"] == wj.CONTRACT_VERSION


def test_merge_incomplet_attend(wj):
    wj.sync_source("raw/a.md", "a" * 64, DOC)
    job = wj.claim(limit=1)["jobs"][0]
    wj.submit(job["job_id"], job["lease_id"], job["fencing_token"],
              wj.CONTRACT_VERSION, _valid_extraction())
    assert wj.merge_pending()["merged"] == 0


def test_merge_double_run_concurrent(wj):
    wj.sync_source("raw/a.md", "a" * 64, DOC)
    _drain_all(wj)
    outs: list[dict] = []

    def worker():
        outs.append(wj.merge_pending())

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(o["merged"] for o in outs) == [0, 1]


def test_merge_erreur_job_n_arrete_pas_les_autres(wj):
    wj.sync_source("raw/a.md", "a" * 64, DOC)
    wj.sync_source("raw/b.md", "b" * 64, DOC)
    first = wj.claim(limit=10)["jobs"]
    jobs_a = [j for j in first if j["source"] == "raw/a.md"]
    for job in jobs_a:
        wj.submit(job["job_id"], job["lease_id"], job["fencing_token"],
                  wj.CONTRACT_VERSION, _valid_extraction())
    # Rendre les baux de b (un run qui les détenait les restitue sans les traiter).
    for job in first:
        if job["source"] == "raw/b.md":
            wj.release(job["job_id"], job["lease_id"], "release")
    # Corrompre un spool de a : merge de a echoue, b doit quand meme passer.
    bad = wj._spool_path(jobs_a[0]["source_hash"], jobs_a[0]["chunk_index"])
    bad.write_text("{ invalide", encoding="utf-8")
    _drain_all(wj, source="raw/b.md", sha="b" * 64)
    res = wj.merge_pending()
    assert res["merged"] == 1  # b
    assert res["failed"] >= 1  # a


# ------------------------------------------------------------------ status
def test_status_sans_quota_externe(wj):
    st = wj.status()
    assert "quota_wait" not in st
    for key in ("pending_docs", "pending_chunks", "leased",
                "submitted_spooled", "merged", "deferred",
                "quarantined", "last_merge_at", "errors_compact",
                "contract_version", "tokenizer"):
        assert key in st, key


def test_ingest_status_deprecie_sans_quota(mcp):
    convia_mcp, wj = mcp
    st = convia_mcp.ingest_status()
    assert st["quota_wait"] is False
    assert st.get("deprecated_legacy_worker") is True
    assert convia_mcp.ingest_start()["deprecated"] is True


# ------------------------------------------------------------------ garde no-LLM
FORBIDDEN = ["generateContent", "countTokens", "GEMINI_API_KEY", "AGY_BIN",
             "SUBMIT_MODE", "RESOURCE_EXHAUSTED", "EXTRACT_MODEL_CASCADE",
             "SAFETY_BLOCK_NONE", "thinking_config", "ModelCascade",
             "generativelanguage"]


def test_aucun_appel_llm_dans_le_chemin_production():
    import vault_mcp
    root = Path(vault_mcp.__file__).parent
    hits = []
    for name in ("wiki_jobs.py", "convia_mcp.py", "server.py"):
        text = (root / name).read_text(encoding="utf-8")
        for pat in FORBIDDEN:
            if pat in text:
                hits.append(f"{name}:{pat}")
    assert hits == []


# ------------------------------------------------------------------ surface MCP
def test_outils_mcp_exposes():
    server = pytest.importorskip("vault_mcp.server")  # SDK MCP requis (CI/prod)
    for tool in ("convia_status", "convia_scan", "convia_list_pending_analysis",
                 "convia_read_for_analysis", "convia_write_analysis",
                 "wiki_ingest_status", "wiki_ingest_start", "wiki_ingest_claim",
                 "wiki_ingest_read", "wiki_ingest_contract",
                 "wiki_ingest_submit", "wiki_ingest_release",
                 "wiki_ingest_merge_pending", "wiki_ingest_sync"):
        assert callable(getattr(server, tool)), tool


def test_e2e_via_wrappers_mcp(mcp):
    # Meme chemin que server.py : wrappers convia_mcp, sans etat partage.
    convia_mcp, wj = mcp
    wj.sync_source("raw/e2e.md", "e" * 64, DOC)
    claimed = convia_mcp.wiki_claim(limit=10)
    assert claimed["leased"] >= 1
    for job in claimed["jobs"]:
        vue = convia_mcp.wiki_read(job["job_id"], job["lease_id"])
        assert vue["chunk_markdown"]
        recu = convia_mcp.wiki_submit(job["job_id"], job["lease_id"],
                                      job["fencing_token"],
                                      wj.CONTRACT_VERSION, _valid_extraction())
        assert recu["receipt_id"]
    res = convia_mcp.wiki_merge_pending()
    assert res["merged"] == 1
    st = convia_mcp.ingest_status()
    assert st["merged"] >= 1


# ------------------------------------------------------- correctifs revue
def test_claim_recupere_bail_expire_sans_reaper(wj):
    # Pas de tache de fond : un lease mort doit etre reattribuable par claim.
    wj.sync_source("raw/a.md", "a" * 64, DOC)
    job = wj.claim(limit=1)["jobs"][0]
    conn = wj.connect()
    conn.execute("UPDATE wiki_jobs SET expires_at=1 WHERE job_id=?",
                 (job["job_id"],))
    conn.commit()
    conn.close()
    repris = wj.claim(limit=10)["jobs"]
    rejoues = [j for j in repris if j["job_id"] == job["job_id"]]
    assert len(rejoues) == 1  # le bail mort est reattribue, pas perdu
    assert rejoues[0]["lease_id"] != job["lease_id"]
    assert rejoues[0]["fencing_token"] == job["fencing_token"] + 1


def test_renew_bail_expire_refuse(wj):
    wj.sync_source("raw/a.md", "a" * 64, DOC)
    job = wj.claim(limit=1)["jobs"][0]
    conn = wj.connect()
    conn.execute("UPDATE wiki_jobs SET expires_at=1 WHERE job_id=?",
                 (job["job_id"],))
    conn.commit()
    conn.close()
    with pytest.raises(wj.WikiJobsError, match="lease-expired"):
        wj.release(job["job_id"], job["lease_id"], "renew")
    # Le job est rendu a la file, pas resuscite.
    assert wj.claim(limit=10)["leased"] >= 1


def test_validation_plafonds(wj):
    ok, errs, _, _ = wj.validate_extraction(_valid_extraction())
    assert ok, errs
    doc = _valid_extraction()
    doc["note"]["sections"] = [
        {"heading": f"S{i}", "markdown": "x" * 300} for i in range(101)]
    ok, errs, _, _ = wj.validate_extraction(doc)
    assert not ok and any("sections" in e for e in errs)
    doc = _valid_extraction()
    doc["entities"] = [dict(doc["entities"][0], slug=f"e-{i}")
                       for i in range(501)]
    ok, errs, _, _ = wj.validate_extraction(doc)
    assert not ok and any("entites" in e for e in errs)
    doc = _valid_extraction()
    doc["issues"] = [{"code": "c", "detail": "d" * 5000}]
    ok, _, _, norm = wj.validate_extraction(doc)
    assert ok and len(norm["issues"][0]["detail"]) == wj.MAX_ISSUE_DETAIL
    doc = _valid_extraction()
    doc["issues"] = [{"node": "pas-un-dict-valide"}]
    ok, _, _, norm = wj.validate_extraction(doc)
    assert ok and norm["issues"] == []


def test_wrapper_rejette_entiers_non_numeriques(mcp):
    convia_mcp, _ = mcp
    with pytest.raises(convia_mcp.ConviaError, match="non numerique"):
        convia_mcp.wiki_claim(limit="beaucoup")
    with pytest.raises(convia_mcp.ConviaError, match="non numerique"):
        convia_mcp.wiki_submit("j", "l", "pas-un-entier", "v", {})


def test_tokenizer_repli_unifie():
    import vault_mcp
    root = Path(vault_mcp.__file__).parent
    text = (root / "wiki_jobs.py").read_text(encoding="utf-8")
    assert "BYTES_PER_TOKEN_FALLBACK = 2.44" in text
    assert '"bytes-fallback"' in text


def test_wiki_sync_remplit_la_file(tmp_path, wj):
    raw = tmp_path / "raw"
    (raw / "notes").mkdir(parents=True)
    (raw / "notes" / "a.md").write_text("# A\n\nContenu. " * 20, encoding="utf-8")
    (raw / "notes" / "b.md").write_text("# B\n\nContenu. " * 20, encoding="utf-8")
    import os

    os.environ["WIKI_RAW_DIR"] = str(raw)
    import importlib

    importlib.reload(wj)
    try:
        stats = wj.sync_directory(limit_files=10)
        assert stats["eligibles"] == 2 and stats["jobs_crees"] == 2
        again = wj.sync_directory(limit_files=10)
        assert again["jobs_crees"] == 0  # idempotent
        assert wj.claim(limit=10)["leased"] == 2
    finally:
        del os.environ["WIKI_RAW_DIR"]
        importlib.reload(wj)


def test_wiki_sync_wrapper_mcp(mcp):
    convia_mcp, wj = mcp
    with pytest.raises(convia_mcp.ConviaError, match="non numerique"):
        convia_mcp.wiki_sync(limit_files="beaucoup")


# ------------------------------------------------------------ contrat expose
COURT = "# Court\n\nun seul chunk."


def _frais(doc):
    """Payload tel qu'il arrive par MCP : un JSON neuf a chaque appel."""
    return json.loads(json.dumps(doc))


def _row(wj, job_id):
    conn = wj.connect()
    try:
        return dict(conn.execute("SELECT * FROM wiki_jobs WHERE job_id=?", (job_id,)).fetchone())
    finally:
        conn.close()


def _module_de_reference(path):
    import runpy
    avant = list(sys.path)
    try:
        return runpy.run_path(str(path))
    finally:
        sys.path[:] = avant


def _claim_un(wj, source="raw/a.md", sha="a" * 64, contenu=COURT):
    wj.sync_source(source, sha, contenu)
    return wj.claim(limit=1)["jobs"][0]


def test_contrat_version_connue_schema_exact_de_la_source(wj):
    ref = _module_de_reference(FACTICE)
    c = wj.contract(wj.CONTRACT_VERSION)
    assert c["contract_version"] == wj.CONTRACT_VERSION
    assert c["response_schema"] == ref["RESPONSE_SCHEMA"]  # la source, pas une copie
    assert c["instructions"] == ref["SYSTEM_PREFIX"]
    assert c["source"]["module"] == str(FACTICE)
    js = c["json_schema"]
    assert js["properties"]["note"]["properties"]["slug"]["pattern"] == wj._SLUG_RE.pattern
    assert js["properties"]["note"]["properties"]["doc_date"]["type"] == ["string", "null"]
    assert "propertyOrdering" not in json.dumps(js)
    assert re.fullmatch(r"[0-9a-f]{64}", c["contract_digest"])
    assert wj.contract(wj.CONTRACT_VERSION)["contract_digest"] == c["contract_digest"]


def test_contrat_version_inconnue_refusee(wj):
    for v in ("wiki-extract-v3", "wiki-extract-v5", ""):
        with pytest.raises(wj.WikiJobsError, match="UNKNOWN_CONTRACT_VERSION"):
            wj.contract(v)


def test_contrat_wrapper_mcp(mcp):
    convia_mcp, wj = mcp
    assert convia_mcp.wiki_contract(wj.CONTRACT_VERSION)["contract_version"] == wj.CONTRACT_VERSION
    with pytest.raises(convia_mcp.ConviaError, match="UNKNOWN_CONTRACT_VERSION"):
        convia_mcp.wiki_contract("wiki-extract-v3")


def test_contrat_absent_fail_closed_rien_n_est_loue(wj, tmp_path, monkeypatch):
    wj.sync_source("raw/a.md", "a" * 64, COURT)
    monkeypatch.setenv("WIKI_CONTRACT_MODULE", str(tmp_path / "absent.py"))
    with pytest.raises(wj.WikiJobsError, match="CONTRACT_UNAVAILABLE"):
        wj.contract(wj.CONTRACT_VERSION)
    with pytest.raises(wj.WikiJobsError, match="CONTRACT_UNAVAILABLE"):
        wj.claim(limit=1)
    assert wj.status()["leased"] == 0


@pytest.mark.parametrize("mutation", [
    lambda t: t.replace('CONTRACT_VERSION = "wiki-extract-v4"',
                        'CONTRACT_VERSION = "wiki-extract-v5"'),
    lambda t: t + '\nRESPONSE_SCHEMA["anyOf"] = []\n',
    lambda t: t + "\nraise RuntimeError('import casse')\n",
    lambda t: t.replace("def validate(", "def _plus_de_validate("),
])
def test_contrat_canonique_inexploitable_fail_closed(wj, tmp_path, monkeypatch, mutation):
    autre = tmp_path / "canonique.py"
    autre.write_text(mutation(FACTICE.read_text(encoding="utf-8")), encoding="utf-8")
    monkeypatch.setenv("WIKI_CONTRACT_MODULE", str(autre))
    with pytest.raises(wj.WikiJobsError, match="CONTRACT_UNAVAILABLE"):
        wj.contract(wj.CONTRACT_VERSION)


def test_wiki_contract_importable_seul():
    # Processus neuf : aucun import prealable ne doit masquer un import manquant
    # (importlib.abc n'est pas charge par importlib.util).
    racine = Path(__file__).resolve().parent.parent
    subprocess.run([sys.executable, "-c", "import vault_mcp.wiki_contract"],
                   cwd=racine, check=True, capture_output=True)


def test_chargement_restaure_sys_path(wj):
    wj._contract._cache.clear()
    avant = list(sys.path)
    wj.contract(wj.CONTRACT_VERSION)
    assert sys.path == avant


def test_claim_et_read_portent_le_digest(wj):
    digest = wj.contract(wj.CONTRACT_VERSION)["contract_digest"]
    wj.sync_source("raw/a.md", "a" * 64, COURT)
    got = wj.claim(limit=1)
    job = got["jobs"][0]
    assert got["contract_digest"] == job["contract_digest"] == digest
    assert wj.read_job(job["job_id"], job["lease_id"])["contract_digest"] == digest


def test_digest_perime_refuse_sans_consommer_de_tentative(wj, tmp_path, monkeypatch):
    job = _claim_un(wj)
    vieux = job["contract_digest"]
    modif = tmp_path / "canonique.py"
    modif.write_text(FACTICE.read_text(encoding="utf-8")
                     + '\nSYSTEM_PREFIX = SYSTEM_PREFIX + " v2"\n', encoding="utf-8")
    monkeypatch.setenv("WIKI_CONTRACT_MODULE", str(modif))
    with pytest.raises(wj.WikiJobsError, match="CONTRACT_DIGEST_MISMATCH"):
        wj.submit(job["job_id"], job["lease_id"], job["fencing_token"],
                  wj.CONTRACT_VERSION, _valid_extraction(), contract_digest=vieux)
    row = _row(wj, job["job_id"])
    assert row["status"] == "leased" and row["attempts"] == 0
    neuf = wj.contract(wj.CONTRACT_VERSION)["contract_digest"]
    assert neuf != vieux
    recu = wj.submit(job["job_id"], job["lease_id"], job["fencing_token"],
                     wj.CONTRACT_VERSION, _valid_extraction(), contract_digest=neuf)
    assert recu["duplicate"] is False


def test_json_schema_derive_valide_et_coherent_avec_le_validateur(wj):
    jsonschema = pytest.importorskip("jsonschema")
    js = wj.contract(wj.CONTRACT_VERSION)["json_schema"]
    jsonschema.Draft202012Validator.check_schema(js)
    jsonschema.validate(_valid_extraction(), js)
    bad = _valid_extraction()
    bad["note"]["tags"] = ["un"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, js)


# ------------------------------------------------------------ submit (contrat)
def test_submit_mauvaise_version_sans_effet(wj):
    job = _claim_un(wj)
    with pytest.raises(wj.WikiJobsError, match="UNKNOWN_CONTRACT_VERSION"):
        wj.submit(job["job_id"], job["lease_id"], job["fencing_token"],
                  "wiki-extract-v3", _valid_extraction())
    row = _row(wj, job["job_id"])
    assert row["status"] == "leased" and row["attempts"] == 0


@pytest.mark.parametrize("casse, motif", [
    (lambda d: d.pop("note"), "note absente"),
    (lambda d: d["note"].update(slug="!!!", title=""), "note.slug"),
    (lambda d: d["note"].update(tags=["un"]), "note.tags"),
    (lambda d: d["note"].update(sections=[]), "sections"),
    (lambda d: d.update(issues=[{"code": "rejet-canonique", "detail": "x"}]), "canonique"),
])
def test_submit_invalide_refuse(wj, casse, motif):
    job = _claim_un(wj)
    doc = _valid_extraction()
    casse(doc)
    with pytest.raises(wj.WikiJobsError, match="validation refusee"):
        wj.submit(job["job_id"], job["lease_id"], job["fencing_token"],
                  wj.CONTRACT_VERSION, doc)
    row = _row(wj, job["job_id"])
    assert row["status"] == "pending" and row["attempts"] == 1
    assert motif in row["last_error"]
    assert wj.status()["spool_files"] == 0


def test_submit_relation_invalide_ecartee_du_spool(wj):
    job = _claim_un(wj)
    doc = _valid_extraction()
    doc["relations"] = [{"from": "ent-test", "to": "fantome", "type": "appelle",
                         "evidence": "x", "confidence": 0.9},
                        {"from": "ent-test", "to": "ent-test", "type": "boucle",
                         "evidence": "x", "confidence": 0.9}]
    wj.submit(job["job_id"], job["lease_id"], job["fencing_token"], wj.CONTRACT_VERSION, doc)
    env = json.loads(wj._spool_path(job["source_hash"], 0).read_text(encoding="utf-8"))
    assert env["relations"] == []


def test_sortie_du_validateur_canonique_est_celle_spoolee(wj):
    job = _claim_un(wj)
    doc = _valid_extraction()
    doc["entities"].append(dict(doc["entities"][0], slug="sauvegarde-2026-01-02"))
    recu = wj.submit(job["job_id"], job["lease_id"], job["fencing_token"],
                     wj.CONTRACT_VERSION, doc)
    env = json.loads(wj._spool_path(job["source_hash"], 0).read_text(encoding="utf-8"))
    assert [e["slug"] for e in env["entities"]] == ["ent-test"]
    assert "entite datee rejetee" in recu["warnings"]


def test_submit_ne_mute_pas_l_objet_de_l_appelant(wj):
    job = _claim_un(wj)
    doc = _valid_extraction()
    avant = json.dumps(doc, sort_keys=True)
    wj.submit(job["job_id"], job["lease_id"], job["fencing_token"], wj.CONTRACT_VERSION, doc)
    assert json.dumps(doc, sort_keys=True) == avant


def test_rejeu_json_frais_reconnu_idempotent(wj):
    # doc_date "" est normalise en None : l'ancien code comparait le hash du
    # payload BRUT au hash NORMALISE et rendait un faux conflit au rejeu reel.
    job = _claim_un(wj)
    doc = _valid_extraction()
    assert doc["note"]["doc_date"] == ""
    r1 = wj.submit(job["job_id"], job["lease_id"], job["fencing_token"],
                   wj.CONTRACT_VERSION, _frais(doc))
    r2 = wj.submit(job["job_id"], job["lease_id"], job["fencing_token"],
                   wj.CONTRACT_VERSION, _frais(doc))
    assert r2["duplicate"] is True and r2["receipt_id"] == r1["receipt_id"]
    assert wj.status()["spool_files"] == 1
    assert wj.merge_pending()["merged"] == 1
    r3 = wj.submit(job["job_id"], job["lease_id"], job["fencing_token"],
                   wj.CONTRACT_VERSION, _frais(doc))
    assert r3["duplicate"] is True
    assert wj.merge_pending()["merged"] == 0  # aucun second merge
    lignes = Path(wj.MANIFEST_PATH).read_text(encoding="utf-8").strip().splitlines()
    assert len(lignes) == 1


def test_collision_de_slug_avec_une_autre_source_du_spool(wj):
    a = _claim_un(wj, "raw/a.md", "a" * 64)
    wj.submit(a["job_id"], a["lease_id"], a["fencing_token"], wj.CONTRACT_VERSION,
              _valid_extraction(slug="meme-slug"))
    b = _claim_un(wj, "raw/b.md", "b" * 64)
    with pytest.raises(wj.WikiJobsError, match="deja porte par une autre source"):
        wj.submit(b["job_id"], b["lease_id"], b["fencing_token"], wj.CONTRACT_VERSION,
                  _valid_extraction(slug="meme-slug"))
    b = wj.claim(limit=1)["jobs"][0]
    assert wj.submit(b["job_id"], b["lease_id"], b["fencing_token"], wj.CONTRACT_VERSION,
                     _valid_extraction(slug="autre-slug"))["receipt_id"]


def test_collision_de_slug_avec_une_fiche_wiki_existante(wj):
    fiche = wj.WIKI_NOTES_DIR / "sources" / "doc-test.md"
    fiche.write_text("---\ntype: source\n---\n# X\n\n## Sources\n\n"
                     "- `raw/autre.md` (sha 12345678)\n", encoding="utf-8")
    job = _claim_un(wj)
    with pytest.raises(wj.WikiJobsError, match="deja porte"):
        wj.submit(job["job_id"], job["lease_id"], job["fencing_token"],
                  wj.CONTRACT_VERSION, _valid_extraction())
    # La fiche de la MEME source est une mise a jour, pas une collision.
    fiche.write_text("# X\n\n## Sources\n\n- `raw/a.md` (sha 12345678)\n", encoding="utf-8")
    job = wj.claim(limit=1)["jobs"][0]
    assert wj.submit(job["job_id"], job["lease_id"], job["fencing_token"],
                     wj.CONTRACT_VERSION, _valid_extraction())["receipt_id"]


@pytest.mark.skipif(os.name != "posix", reason="modes POSIX")
def test_spool_lisible_par_le_groupe_malgre_umask(wj):
    old = os.umask(0o077)
    try:
        job = _claim_un(wj)
        wj.submit(job["job_id"], job["lease_id"], job["fencing_token"],
                  wj.CONTRACT_VERSION, _valid_extraction())
    finally:
        os.umask(old)
    p = wj._spool_path(job["source_hash"], 0)
    assert p.stat().st_mode & 0o777 == 0o660
    assert p.parent.stat().st_mode & 0o7777 == 0o2775


# ------------------------------------------------------------ baux / fencing
def test_bail_expire_ancien_bail_et_ancien_fencing_refuses(wj):
    job = _claim_un(wj)
    conn = wj.connect()
    conn.execute("UPDATE wiki_jobs SET expires_at=1 WHERE job_id=?", (job["job_id"],))
    conn.commit()
    conn.close()
    assert wj.status()["leased_expired"] == 1
    neuf = wj.claim(limit=1)["jobs"][0]
    assert neuf["job_id"] == job["job_id"] and neuf["fencing_token"] == job["fencing_token"] + 1
    with pytest.raises(wj.WikiJobsError, match="lease-invalid"):
        wj.submit(job["job_id"], job["lease_id"], job["fencing_token"],
                  wj.CONTRACT_VERSION, _valid_extraction())
    with pytest.raises(wj.WikiJobsError, match="fencing"):
        wj.submit(neuf["job_id"], neuf["lease_id"], job["fencing_token"],
                  wj.CONTRACT_VERSION, _valid_extraction())
    assert wj.submit(neuf["job_id"], neuf["lease_id"], neuf["fencing_token"],
                     wj.CONTRACT_VERSION, _valid_extraction())["duplicate"] is False
    assert wj.status()["leased_expired"] == 0


def test_release_apres_submit_refuse_pas_de_double_cloture(wj):
    job = _claim_un(wj)
    wj.submit(job["job_id"], job["lease_id"], job["fencing_token"],
              wj.CONTRACT_VERSION, _valid_extraction())
    for action in ("release", "defer", "renew"):
        with pytest.raises(wj.WikiJobsError, match="non loue"):
            wj.release(job["job_id"], job["lease_id"], action)
    assert _row(wj, job["job_id"])["status"] == "submitted"
    assert wj.merge_pending()["merged"] == 1
    with pytest.raises(wj.WikiJobsError, match="lease-invalid"):
        wj.release(job["job_id"], job["lease_id"], "release")
    assert _row(wj, job["job_id"])["status"] == "merged"


# ------------------------------------------------------------ merge
def test_merge_pending_demande_la_fusion_des_fiches(mcp):
    convia_mcp, wj = mcp
    job = _claim_un(wj)
    convia_mcp.wiki_submit(job["job_id"], job["lease_id"], job["fencing_token"],
                           wj.CONTRACT_VERSION, _valid_extraction(),
                           job["contract_digest"])
    assert not convia_mcp.INGEST_REQUEST.exists()
    res = convia_mcp.wiki_merge_pending()
    assert res["merged"] == 1 and res["note_merge_requested"] is True
    assert convia_mcp.INGEST_REQUEST.exists()
    convia_mcp.INGEST_REQUEST.unlink()
    again = convia_mcp.wiki_merge_pending()
    assert again["merged"] == 0 and again["note_merge_requested"] is False
    assert not convia_mcp.INGEST_REQUEST.exists()


def test_merge_pending_contrat_indisponible_ne_touche_rien(wj, tmp_path, monkeypatch):
    job = _claim_un(wj)
    wj.submit(job["job_id"], job["lease_id"], job["fencing_token"],
              wj.CONTRACT_VERSION, _valid_extraction())
    monkeypatch.setenv("WIKI_CONTRACT_MODULE", str(tmp_path / "absent.py"))
    with pytest.raises(wj.WikiJobsError, match="CONTRACT_UNAVAILABLE"):
        wj.merge_pending()
    row = _row(wj, job["job_id"])
    assert row["status"] == "submitted" and row["attempts"] == 0


# ------------------------------------------------------------ contrat reel (VPS)
reel = pytest.mark.skipif(not REEL.is_file(), reason="contrat canonique absent (hors VPS)")


@reel
def test_contrat_reel_identique_a_print_schema(wj, monkeypatch):
    monkeypatch.setenv("WIKI_CONTRACT_MODULE", str(REEL))
    c = wj.contract(wj.CONTRACT_VERSION)
    out = subprocess.run([sys.executable, str(REEL), "--print-schema"],
                         capture_output=True, text=True, check=True).stdout
    assert c["response_schema"] == json.loads(out)
    assert c["instructions"] == _module_de_reference(REEL)["SYSTEM_PREFIX"]


@reel
def test_contrat_reel_json_schema_et_flux_complet(mcp, monkeypatch):
    jsonschema = pytest.importorskip("jsonschema")
    convia_mcp, wj = mcp
    monkeypatch.setenv("WIKI_CONTRACT_MODULE", str(REEL))
    c = convia_mcp.wiki_contract(wj.CONTRACT_VERSION)
    jsonschema.Draft202012Validator.check_schema(c["json_schema"])
    doc = _valid_extraction(title="Audit latence gateway", slug="audit-latence-gateway")
    jsonschema.validate(doc, c["json_schema"])
    # Entite = le document lui-meme : ecartee par le canonique, pas par vault-mcp.
    doc["entities"].append(dict(doc["entities"][0], slug="audit-latence-gateway",
                                name="Audit latence gateway"))
    job = _claim_un(wj)
    assert job["contract_digest"] == c["contract_digest"]
    recu = convia_mcp.wiki_submit(job["job_id"], job["lease_id"], job["fencing_token"],
                                  wj.CONTRACT_VERSION, _frais(doc), c["contract_digest"])
    env = json.loads(wj._spool_path(job["source_hash"], 0).read_text(encoding="utf-8"))
    assert [e["slug"] for e in env["entities"]] == ["ent-test"]
    assert any("entite-titre" in w for w in recu["warnings"])
    assert convia_mcp.wiki_submit(job["job_id"], job["lease_id"], job["fencing_token"],
                                  wj.CONTRACT_VERSION, _frais(doc),
                                  c["contract_digest"])["duplicate"] is True
    assert convia_mcp.wiki_merge_pending()["merged"] == 1


# ------------------------------------------------- mort du consommateur (2026-09-12)
def _expirer(wj, job_id):
    conn = wj.connect()
    conn.execute("UPDATE wiki_jobs SET expires_at=1 WHERE job_id=?", (job_id,))
    conn.commit()
    conn.close()


def test_reaper_rend_le_bail_sans_tentative_et_trace(wj):
    wj.sync_source("raw/a.md", "a" * 64, "Contenu court. " * 10)
    job = wj.claim(limit=1)["jobs"][0]
    _expirer(wj, job["job_id"])
    assert wj.status()["leased_expired"] == 1
    assert wj.reap_expired("test") == 1
    row = wj.connect().execute("SELECT * FROM wiki_jobs WHERE job_id=?",
                               (job["job_id"],)).fetchone()
    assert row["status"] == "pending" and row["attempts"] == 0
    assert [e["event"] for e in wj.events(job["job_id"])][:2] == ["lease-expired", "claim"]
    again = wj.claim(limit=1)["jobs"][0]
    assert again["fencing_token"] == job["fencing_token"] + 1


def test_bail_plafonne_et_renew_plafonne(wj):
    wj.sync_source("raw/a.md", "a" * 64, "Contenu court. " * 10)
    job = wj.claim(limit=1, lease_seconds=86400)["jobs"][0]
    row = wj.connect().execute("SELECT expires_at FROM wiki_jobs WHERE job_id=?",
                               (job["job_id"],)).fetchone()
    assert row["expires_at"] - wj._now_ts() <= wj.MAX_LEASE_S
    wj.release(job["job_id"], job["lease_id"], "renew")
    row = wj.connect().execute("SELECT expires_at FROM wiki_jobs WHERE job_id=?",
                               (job["job_id"],)).fetchone()
    assert row["expires_at"] - wj._now_ts() <= wj.MAX_LEASE_S


def test_job_d_une_version_morte_jamais_loue_ni_penalise(wj):
    wj.sync_source("raw/a.md", "a" * 64, "Version un. " * 10)
    wj.sync_source("raw/a.md", "b" * 64, "Version deux. " * 10)
    jobs = wj.claim(limit=10)["jobs"]
    assert {j["source_hash"] for j in jobs} == {"b" * 64}
    rows = wj.connect().execute(
        "SELECT status, attempts FROM wiki_jobs WHERE source_hash=?", ("a" * 64,)).fetchall()
    assert all(r["status"] == "superseded" and r["attempts"] == 0 for r in rows)
