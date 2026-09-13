"""Transport MCP reel + journal d'appels : distinguer serveur lent, client mort,
coupure, outil en erreur, et « le LLM n'a jamais envoye l'ecriture »."""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import time

import httpx
import pytest

from tests.e2e import canary
from tests.e2e.conftest import kill_consumer_at
from tests.e2e.harness import REPO, TOKEN, Bench, RawClient, ToolRefusedError, sdk_call

pytestmark = pytest.mark.e2e


def _calls(bench: Bench, at_least: int = 1, timeout: float = 5.0) -> list[dict]:
    """La ligne est ecrite APRES l'envoi de la reponse : attendre qu'elle arrive."""
    deadline = time.monotonic() + timeout
    rows: list[dict] = []
    while time.monotonic() < deadline:
        try:
            rows = bench.sql("telemetry.db", "SELECT * FROM mcp_calls ORDER BY id")
        except Exception:  # noqa: BLE001 -- base pas encore creee
            rows = []
        if len(rows) >= at_least:
            return rows
        time.sleep(0.05)
    return rows


def _telemetry_cli(bench: Bench, *args: str) -> str:
    env = bench.env()
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "vault_mcp.telemetry", *args], cwd=REPO, env=env,
        capture_output=True, text=True, check=True).stdout


def test_client_sdk_officiel(bench: Bench) -> None:
    data = sdk_call(bench.url, "convia_status", {})
    assert "analysis_pending" in data
    calls = _calls(bench)
    assert any(c["tool"] == "convia_status" and c["outcome"] == "ok" for c in calls)


def test_jeton_invalide_http_error(bench: Bench) -> None:
    c = RawClient(bench.url, token="mauvais-" + "x" * 40)
    with pytest.raises(httpx.HTTPStatusError):
        c.initialize()
    assert _calls(bench)[-1]["outcome"] == "http_error"
    assert _calls(bench)[-1]["http_status"] == 401


def test_outil_en_erreur_trace_sans_contenu(bench: Bench, client: RawClient) -> None:
    with pytest.raises(ToolRefusedError):
        client.call("convia_read_for_analysis", path="raw/assets/ConvIA/x/absent.md")
    time.sleep(0.3)
    last = [c for c in _calls(bench) if c["tool"]][-1]
    assert last["outcome"] == "tool_error"
    assert "introuvable" in (last["error"] or "")
    assert last["ref"].startswith("p:") and "absent" not in last["ref"]


def test_contenu_jamais_dans_la_telemetrie(bench: Bench, client: RawClient) -> None:
    conv = canary.conversation(0, 3000, seed=77)
    secret_phrase = "PHRASE-PRIVEE-QUI-NE-DOIT-JAMAIS-FUIR"
    path = bench.add_conversation(canary.SOURCE, conv.name, conv.body + secret_phrase)
    client.call("convia_scan")
    read = client.call("convia_read_for_analysis", path=path)
    client.call("convia_write_analysis", source_path=path, source_hash=read["source_sha256"],
                analysis_version=read["analysis_version"],
                markdown="ANALYSE-PRIVEE-QUI-NE-DOIT-JAMAIS-FUIR")
    dump = (bench.root / "telemetry.db").read_bytes()
    for wal in bench.root.glob("telemetry.db-wal"):
        dump += wal.read_bytes()
    assert secret_phrase.encode() not in dump
    assert b"ANALYSE-PRIVEE" not in dump
    assert conv.name.encode() not in dump
    assert TOKEN.encode() not in dump


def test_consommateur_tue_visible_dans_le_journal(bench: Bench) -> None:
    bench.add_wiki_doc("doc-t.md", canary.wiki_doc(99))
    seen = kill_consumer_at(bench, "wiki", "claim", lease=60)
    session = seen["init"]["session"]
    out = _telemetry_cli(bench, "calls", session[:12])
    tools = [line.split()[4] for line in out.splitlines() if "tools/call" in line]
    assert tools == ["wiki_ingest_sync", "wiki_ingest_claim"], out
    assert "wiki_ingest_submit" not in out, "on voit qu'aucun submit n'a ete envoye"
    runs = _telemetry_cli(bench, "runs", "--last", "5")
    assert session[:12] in runs and "wiki_ingest_claim:ok" in runs


def test_coupure_client_pendant_la_reponse(bench: Bench, client: RawClient) -> None:
    convs = canary.realistic(30, cap=300_000)
    for conv in convs:
        bench.add_conversation(canary.SOURCE, conv.name, conv.body)
    client.call("convia_scan")
    fast = httpx.Client(timeout=httpx.Timeout(0.005))
    body = {"jsonrpc": "2.0", "id": 99, "method": "tools/call",
            "params": {"name": "convia_list_pending_analysis", "arguments": {"limit": 50}}}
    headers = {"authorization": f"Bearer {TOKEN}", "content-type": "application/json",
               "accept": "application/json, text/event-stream",
               "mcp-session-id": client.session or "", "mcp-protocol-version": "2025-06-18"}
    with pytest.raises(httpx.TimeoutException):
        fast.post(bench.url, content=json.dumps(body), headers=headers)
    fast.close()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        rows = [c for c in _calls(bench) if c["tool"] == "convia_list_pending_analysis"]
        if rows:
            break
        time.sleep(0.2)
    assert rows[-1]["outcome"] == "client_disconnect", rows[-1]


def test_redemarrage_serveur_en_cours_de_run(bench: Bench) -> None:
    convs = canary.tiny(4)
    c = RawClient(bench.url)
    c.initialize()
    paths = [bench.add_conversation(canary.SOURCE, x.name, x.body) for x in convs]
    c.call("convia_scan")
    read = c.call("convia_read_for_analysis", path=paths[0])
    c.call("convia_write_analysis", source_path=paths[0], source_hash=read["source_sha256"],
           analysis_version=read["analysis_version"], markdown=canary.analysis_markdown(0))
    bench.stop(signal.SIGKILL)
    bench.start()
    with pytest.raises(httpx.HTTPStatusError):
        c.call("convia_status")  # ancienne session inconnue du nouveau processus
    c2 = RawClient(bench.url)
    c2.initialize()
    listed = c2.call("convia_list_pending_analysis", limit=50)
    assert listed["pending_total"] == 3, "l'ecriture faite avant le crash est conservee"
    c2.close()


def test_gros_corps_passe_en_flux_et_reste_trace(bench: Bench, client: RawClient) -> None:
    """Revue 2026-09-12 : le middleware (avant auth) ne tamponne pas un corps entier."""
    conv = canary.conversation(0, 2000, seed=5)
    path = bench.add_conversation(canary.SOURCE, conv.name, conv.body)
    client.call("convia_scan")
    read = client.call("convia_read_for_analysis", path=path)
    enorme = "x" * 400_000  # > BODY_BUFFER_MAX et > MAX_ANALYSIS_CHARS
    with pytest.raises(ToolRefusedError, match="trop longue"):
        client.call("convia_write_analysis", source_path=path,
                    source_hash=read["source_sha256"],
                    analysis_version=read["analysis_version"], markdown=enorme)
    time.sleep(0.3)
    last = [c for c in _calls(bench) if c["tool"]][-1]
    assert last["tool"] == "convia_write_analysis"
    assert last["bytes_in"] > 400_000
    assert last["outcome"] == "tool_error"


def test_gros_corps_sans_jeton_refuse_sans_tamponner(bench: Bench) -> None:
    corps = b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"x","arguments":{"a":"'
    corps += b"y" * 3_000_000 + b'"}}}'
    resp = httpx.post(bench.url, content=corps, timeout=30,
                      headers={"content-type": "application/json",
                               "accept": "application/json, text/event-stream"})
    assert resp.status_code == 401
    last = _calls(bench)[-1]
    assert last["outcome"] in ("http_error", "client_disconnect")
    assert bench.proc is not None and bench.proc.poll() is None, "serveur toujours vivant"
