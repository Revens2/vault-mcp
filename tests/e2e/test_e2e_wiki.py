"""Wiki de bout en bout : consommateur tue apres claim / read / submit, reponse
perdue, redemarrage serveur, merge rejoue. Bail court (2 s) pour tester
l'expiration reelle sans attendre 15 min."""

from __future__ import annotations

import signal
import time

import pytest

from tests.e2e import canary
from tests.e2e.conftest import kill_consumer_at
from tests.e2e.harness import Bench, RawClient, ToolRefusedError

pytestmark = pytest.mark.e2e


def _job(bench: Bench, job_id: str) -> dict:
    return bench.sql("wiki_jobs.db", "SELECT * FROM wiki_jobs WHERE job_id=?", (job_id,))[0]


def _events(bench: Bench, job_id: str) -> list[str]:
    return [r["event"] for r in bench.sql(
        "wiki_jobs.db", "SELECT event FROM wiki_job_events WHERE job_id=? ORDER BY id", (job_id,))]


def _submit(client: RawClient, job: dict, fencing: int | None = None, slug: str = "") -> dict:
    return client.call("wiki_ingest_submit", job_id=job["job_id"], lease_id=job["lease_id"],
                       fencing_token=job["fencing_token"] if fencing is None else fencing,
                       contract_version=job["contract_version"],
                       extraction=canary.wiki_extraction(slug or "canary-" + job["job_id"][:10]),
                       contract_digest=job["contract_digest"])


def _recover_after_death(bench: Bench, client: RawClient, dead: dict) -> dict:
    job_id = dead["job_id"]
    assert _job(bench, job_id)["status"] == "leased"
    time.sleep(3.5)  # bail de 2 s expire (horodatage a la seconde)
    sync = client.call("wiki_ingest_sync", limit_files=200)
    assert sync["baux_expires_rendus"] == 1, "le run suivant rend le bail des sa phase sync"
    row = _job(bench, job_id)
    assert row["status"] == "pending" and row["attempts"] == 0
    assert "lease-expired" in _events(bench, job_id)
    got = client.call("wiki_ingest_claim", limit=1, lease_seconds=60)
    new = got["jobs"][0]
    assert new["job_id"] == job_id
    assert new["fencing_token"] == dead["fencing"] + 1
    # L'ancien detenteur ressuscite : refuse.
    with pytest.raises(ToolRefusedError, match="lease-invalid|fencing"):
        client.call("wiki_ingest_submit", job_id=job_id, lease_id=dead["lease_id"],
                    fencing_token=dead["fencing"], contract_version=new["contract_version"],
                    extraction=canary.wiki_extraction("canary-zombie"),
                    contract_digest=new["contract_digest"])
    return new


# ------------------------------------------------------------------ Crash 1
def test_crash_juste_apres_claim(bench: Bench, client: RawClient) -> None:
    bench.add_wiki_doc("doc-1.md", canary.wiki_doc(1))
    seen = kill_consumer_at(bench, "wiki", "claim", lease=2)
    new = _recover_after_death(bench, client, seen["claim"])
    rec = _submit(client, new)
    assert rec["duplicate"] is False
    assert _job(bench, new["job_id"])["attempts"] == 0, "mort du consommateur : 0 tentative"
    merged = client.call("wiki_ingest_merge_pending")
    assert merged["merged"] == 1


# ------------------------------------------------------------------ Crash 2
def test_crash_apres_read(bench: Bench, client: RawClient) -> None:
    bench.add_wiki_doc("doc-2.md", canary.wiki_doc(2))
    seen = kill_consumer_at(bench, "wiki", "read", lease=2)
    new = _recover_after_death(bench, client, seen["claim"])
    client.call("wiki_ingest_read", job_id=new["job_id"], lease_id=new["lease_id"])
    _submit(client, new)
    assert client.call("wiki_ingest_merge_pending")["merged"] == 1
    assert _job(bench, new["job_id"])["attempts"] == 0


# ------------------------------------------------------------------ Crash 3
def test_reponse_submit_perdue_puis_rejeu(bench: Bench, client: RawClient) -> None:
    bench.add_wiki_doc("doc-3.md", canary.wiki_doc(3))
    client.call("wiki_ingest_sync", limit_files=200)
    job = client.call("wiki_ingest_claim", limit=1, lease_seconds=60)["jobs"][0]
    extraction = canary.wiki_extraction("canary-lost")
    args = {"job_id": job["job_id"], "lease_id": job["lease_id"],
            "fencing_token": job["fencing_token"], "contract_version": job["contract_version"],
            "extraction": extraction, "contract_digest": job["contract_digest"]}
    client.call_and_lose_response("wiki_ingest_submit", **args)
    deadline = time.monotonic() + 10
    while _job(bench, job["job_id"])["status"] != "submitted" and time.monotonic() < deadline:
        time.sleep(0.1)
    row = _job(bench, job["job_id"])
    assert row["status"] == "submitted"
    spool = sorted((bench.root / "wiki-spool").rglob("*.json"))
    assert len(spool) == 1
    replay = client.call("wiki_ingest_submit", **args)
    assert replay["duplicate"] is True and replay["receipt_id"] == row["receipt_id"]
    assert sorted((bench.root / "wiki-spool").rglob("*.json")) == spool, "aucune seconde fiche"
    with pytest.raises(ToolRefusedError, match="conflit"):
        client.call("wiki_ingest_submit", **{**args,
                    "extraction": canary.wiki_extraction("canary-autre")})
    assert _job(bench, job["job_id"])["attempts"] == 0


# ------------------------------------------------------------------ Crash 4
def test_crash_apres_submit_avant_merge(bench: Bench, client: RawClient) -> None:
    bench.add_wiki_doc("doc-4.md", canary.wiki_doc(4))
    seen = kill_consumer_at(bench, "wiki", "submit", lease=60)
    job_id = seen["submit"]["job_id"]
    assert _job(bench, job_id)["status"] == "submitted"
    # Run suivant : aucune nouvelle extraction, le merge suffit.
    res = client.call("wiki_ingest_merge_pending")
    assert res["merged"] == 1
    row = _job(bench, job_id)
    assert row["status"] == "merged" and row["attempts"] == 0
    assert client.call("wiki_ingest_claim", limit=10)["leased"] == 0


# ------------------------------------------------------------------ Crash 5
def test_serveur_tue_apres_submit_puis_merge_rejoue_sans_doublon(bench: Bench) -> None:
    bench.add_wiki_doc("doc-5.md", canary.wiki_doc(5))
    c = RawClient(bench.url)
    c.initialize()
    c.call("wiki_ingest_sync", limit_files=200)
    job = c.call("wiki_ingest_claim", limit=1, lease_seconds=60)["jobs"][0]
    _submit(c, job)
    bench.stop(signal.SIGKILL)  # crash brutal du SERVEUR
    bench.start()
    c2 = RawClient(bench.url)
    c2.initialize()
    first = c2.call("wiki_ingest_merge_pending")
    second = c2.call("wiki_ingest_merge_pending")
    assert first["merged"] == 1 and second["merged"] == 0
    lines = (bench.root / "manifest.jsonl").read_text().splitlines()
    assert len(lines) == 1, "merge rejoue : une seule entree de manifeste"
    c2.close()


def test_merges_concurrents_convergent(bench: Bench, client: RawClient) -> None:
    import threading

    for i in range(4):
        bench.add_wiki_doc(f"doc-c{i}.md", canary.wiki_doc(10 + i))
    client.call("wiki_ingest_sync", limit_files=200)
    for job in client.call("wiki_ingest_claim", limit=10, lease_seconds=60)["jobs"]:
        _submit(client, job)
    results: list[int] = []

    def merge() -> None:
        c = RawClient(bench.url)
        c.initialize()
        results.append(int(c.call("wiki_ingest_merge_pending")["merged"]))
        c.close()

    threads = [threading.Thread(target=merge) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(results) == 4
    rows = bench.sql("wiki_jobs.db", "SELECT status, COUNT(*) n FROM wiki_jobs GROUP BY status")
    assert rows == [{"status": "merged", "n": 4}]


def test_source_modifiee_job_superseded_sans_tentative(bench: Bench, client: RawClient) -> None:
    doc = bench.add_wiki_doc("doc-6.md", canary.wiki_doc(6))
    client.call("wiki_ingest_sync", limit_files=200)
    old = client.call("wiki_ingest_claim", limit=1, lease_seconds=60)["jobs"][0]
    doc.write_text(canary.wiki_doc(6) + "\nversion 2 CANARY-E2E\n", encoding="utf-8")
    client.call("wiki_ingest_sync", limit_files=200)
    with pytest.raises(ToolRefusedError, match="stale"):
        _submit(client, old)
    row = _job(bench, old["job_id"])
    assert row["status"] == "superseded" and row["attempts"] == 0
    got = client.call("wiki_ingest_claim", limit=10, lease_seconds=60)
    assert [j["job_id"] for j in got["jobs"]] != [old["job_id"]]
    assert got["leased"] == 1


def test_bail_plafonne(bench: Bench, client: RawClient) -> None:
    bench.add_wiki_doc("doc-7.md", canary.wiki_doc(7))
    client.call("wiki_ingest_sync", limit_files=200)
    job = client.call("wiki_ingest_claim", limit=1, lease_seconds=86400)["jobs"][0]
    row = _job(bench, job["job_id"])
    assert row["expires_at"] - time.time() <= 900 + 2
