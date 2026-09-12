"""Journal des appels MCP, pour reconstituer un run du consommateur.

Pourquoi : le 2026-09-12, impossible de dire ce qu'un run ChatGPT « a 0 traite »
avait fait. nginx ne loggue ni duree ni session, vault-mcp ne tracait pas les
`tools/call`. On raisonnait sur « le scheduler a tourne + le compteur n'a pas bouge ».

Ce middleware ASGI note, pour chaque requete HTTP vers `/mcp` : debut, duree,
session MCP (`Mcp-Session-Id`), methode JSON-RPC, nom d'outil, identifiant metier
MINIMAL (empreinte courte du chemin, job_id tronque), statut HTTP, octets, et
l'issue : `ok`, `tool_error` (refus metier), `http_error`, `client_disconnect`
(le client a coupe avant la fin de la reponse), `exception`.

Jamais stocke : contenu de conversation, analyse, extraction, prompt, jeton,
en-tete d'autorisation. Le corps de requete n'est lu que pour en extraire
`method`, `params.name` et deux identifiants ; il n'est jamais ecrit.

Une panne du journal ne casse jamais une requete : toute erreur d'ecriture est
avalee (le service passe avant son observabilite).

CLI : `python -m vault_mcp.telemetry runs [--last 10]` et
`python -m vault_mcp.telemetry calls <session_prefix>`.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
from collections.abc import MutableMapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]

DB_DEFAUT = "/var/lib/vault-mcp/telemetry.db"
RETENTION_S = 30 * 86400
# Deux requetes d'une meme session separees de plus que ca = deux runs distincts.
RUN_GAP_S = 15 * 60
MAX_BODY_PARSE = 2_000_000
# Plafond du tampon de corps (requete pas encore authentifiee a ce stade).
BODY_BUFFER_MAX = 256_000
SNIFF_BYTES = 8192
SMALL_RESPONSE = 6000
_WRITE_TOOLS = ("convia_write_analysis", "wiki_ingest_submit")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mcp_calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  REAL    NOT NULL,
    duration_ms INTEGER NOT NULL,
    session     TEXT,
    client      TEXT,
    http_method TEXT,
    rpc_method  TEXT,
    tool        TEXT,
    ref         TEXT,
    http_status INTEGER,
    bytes_in    INTEGER,
    bytes_out   INTEGER,
    outcome     TEXT    NOT NULL,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_mcp_calls_started ON mcp_calls (started_at);
CREATE INDEX IF NOT EXISTS idx_mcp_calls_session ON mcp_calls (session, started_at);
"""

_lock = threading.Lock()


def db_path() -> Path:
    return Path(os.environ.get("VAULT_MCP_TELEMETRY_DB", DB_DEFAUT))


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    nouveau = not path.exists()
    conn = sqlite3.connect(path, timeout=5)
    if nouveau:
        # Pas de contenu ici, mais des horaires d'usage : lisible par le service seul.
        with contextlib.suppress(OSError):
            path.chmod(0o600)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


def _ref(args: dict[str, Any]) -> str:
    """Identifiant metier minimal, non reversible pour un chemin."""
    job = args.get("job_id")
    if isinstance(job, str) and job:
        return "job:" + job[:12]
    for key in ("path", "source_path"):
        val = args.get(key)
        if isinstance(val, str) and val:
            return "p:" + hashlib.sha256(val.encode("utf-8")).hexdigest()[:10]
    return ""


def parse_request(body: bytes) -> tuple[str, str, str]:
    """(rpc_method, tool, ref). Batch JSON-RPC : premier element seulement."""
    if not body or len(body) > MAX_BODY_PARSE:
        return "", "", ""
    try:
        doc = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return "?", "", ""
    if isinstance(doc, list):
        doc = doc[0] if doc else {}
    if not isinstance(doc, dict):
        return "?", "", ""
    method = str(doc.get("method") or ("response" if "result" in doc else ""))[:60]
    params = doc.get("params") if isinstance(doc.get("params"), dict) else {}
    tool = ""
    ref = ""
    if method == "tools/call":
        tool = str(params.get("name") or "")[:60]
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        ref = _ref(args)
    return method, tool, ref


_HEAD_METHOD = re.compile(rb'"method"\s*:\s*"([^"]{1,60})"')
_HEAD_NAME = re.compile(rb'"name"\s*:\s*"([A-Za-z0-9_]{1,60})"')


def _parse_head(head: bytes) -> tuple[str, str, str]:
    """Corps trop gros pour etre tamponne : methode et outil depuis le debut seulement."""
    method = _HEAD_METHOD.search(head)
    name = _HEAD_NAME.search(head)
    m = method.group(1).decode("ascii", "replace") if method else "?"
    return m, (name.group(1).decode("ascii") if name and m == "tools/call" else ""), ""


_REFUS = re.compile(rb'(\\?"etat\\?"\s*:\s*\\?"refuse\\?"|\\?"isError\\?"\s*:\s*true)')
_DUPLICATE = re.compile(rb'\\?"duplicate\\?"\s*:\s*true')
_MESSAGE = re.compile(rb'\\?"message\\?"\s*:\s*\\?"((?:[^"\\]|\\.){0,200})')


def sniff_error(head: bytes) -> str | None:
    """Message court d'un refus metier dans le debut de la reponse, ou None."""
    if not _REFUS.search(head):
        return None
    match = _MESSAGE.search(head)
    brut = match.group(1).decode("utf-8", "replace") if match else "refus"
    brut = brut.replace("\\\\", "\\").replace('\\"', '"')
    # Le message d'erreur peut citer un chemin, jamais du contenu. Par securite,
    # on le passe quand meme au masqueur de secrets.
    try:
        from vault_mcp.secrets import masquer

        brut = masquer(brut)
    except ImportError:  # pragma: no cover
        pass
    return brut[:160]


def record(row: dict[str, Any]) -> None:
    with _lock, contextlib.suppress(sqlite3.Error, OSError):
        conn = connect()
        try:
            conn.execute(
                "INSERT INTO mcp_calls (started_at, duration_ms, session, client,"
                " http_method, rpc_method, tool, ref, http_status, bytes_in, bytes_out,"
                " outcome, error) VALUES (:started_at, :duration_ms, :session, :client,"
                " :http_method, :rpc_method, :tool, :ref, :http_status, :bytes_in,"
                " :bytes_out, :outcome, :error)", row)
            if int(row["started_at"]) % 97 == 0:
                conn.execute("DELETE FROM mcp_calls WHERE started_at < ?",
                             (time.time() - RETENTION_S,))
            conn.commit()
        finally:
            conn.close()


def _header(scope: Scope, name: bytes) -> str:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return str(value.decode("latin-1"))
    return ""


class Telemetrie:
    """Middleware ASGI. Place a l'exterieur de l'authentification."""

    def __init__(self, app: Any, *, chemin: str = "/mcp") -> None:
        self._app = app
        self._chemin = chemin

    async def __call__(self, scope: Scope, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not str(scope.get("path", "")).startswith(self._chemin):
            await self._app(scope, receive, send)
            return

        t0 = time.time()
        m0 = time.monotonic()
        http_method = str(scope.get("method", ""))
        # Ce middleware est AVANT l'authentification : il ne doit jamais retenir en
        # memoire plus que BODY_BUFFER_MAX octets d'une requete non authentifiee.
        # On tamponne au plus ce plafond pour identifier l'outil, on le rejoue a
        # l'app, puis le reste du corps passe en flux sans copie.
        chunks: list[bytes] = []
        buffered = 0
        disconnected_early = False
        body_complete = http_method != "POST"
        if http_method == "POST":
            while buffered < BODY_BUFFER_MAX:
                msg = await receive()
                if msg["type"] == "http.disconnect":
                    disconnected_early = True
                    break
                data = msg.get("body", b"") or b""
                chunks.append(data)
                buffered += len(data)
                if not msg.get("more_body"):
                    body_complete = True
                    break
        body = b"".join(chunks)
        bytes_in = {"n": len(body)}
        if body_complete:
            rpc_method, tool, ref = parse_request(body)
        else:
            rpc_method, tool, ref = _parse_head(body)
        state: dict[str, Any] = {
            "status": 0, "bytes_out": 0, "head": b"", "done": False,
            "disconnect": disconnected_early, "session": _header(scope, b"mcp-session-id"),
        }
        replayed = False

        async def receive_wrapper() -> Message:
            nonlocal replayed
            if http_method == "POST" and not replayed:
                replayed = True
                if disconnected_early:
                    return {"type": "http.disconnect"}
                return {"type": "http.request", "body": body,
                        "more_body": not body_complete}
            msg = await receive()
            if msg["type"] == "http.request":
                bytes_in["n"] += len(msg.get("body", b"") or b"")
            if msg["type"] == "http.disconnect" and not state["done"]:
                state["disconnect"] = True
            return msg  # type: ignore[no-any-return]

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                state["status"] = int(message.get("status", 0))
                for key, value in message.get("headers", []):
                    if key.lower() == b"mcp-session-id" and not state["session"]:
                        state["session"] = value.decode("latin-1")
            elif message["type"] == "http.response.body":
                data = message.get("body", b"") or b""
                state["bytes_out"] += len(data)
                if len(state["head"]) < SNIFF_BYTES:
                    state["head"] += data[: SNIFF_BYTES - len(state["head"])]
                if not message.get("more_body"):
                    state["done"] = True
            try:
                await send(message)
            except OSError:
                state["disconnect"] = True
                raise

        outcome = "ok"
        error: str | None = None
        try:
            await self._app(scope, receive_wrapper, send_wrapper)
        except BaseException as exc:
            if state["disconnect"] and not state["done"]:
                outcome = "client_disconnect"
            else:
                outcome = "exception"
                error = type(exc).__name__
            raise
        finally:
            if outcome not in ("exception", "client_disconnect"):
                if state["disconnect"] and not state["done"]:
                    outcome = "client_disconnect"
                elif state["status"] >= 400:
                    outcome = "http_error"
                else:
                    # Un refus est une petite reponse {etat, message}. Au-dela, c'est du
                    # contenu (projection, chunk) qui peut CITER ces mots : ne pas sniffer.
                    small = state["bytes_out"] <= SMALL_RESPONSE
                    sniffed = sniff_error(state["head"]) if tool and small else None
                    if sniffed is not None:
                        outcome, error = "tool_error", sniffed
                    elif (small and tool in _WRITE_TOOLS
                          and _DUPLICATE.search(state["head"])):
                        outcome = "duplicate"
            if http_method == "DELETE":
                rpc_method = rpc_method or "session/close"
            row = {
                "started_at": t0,
                "duration_ms": int((time.monotonic() - m0) * 1000),
                "session": (state["session"] or "")[:64] or None,
                "client": _header(scope, b"user-agent")[:40] or None,
                "http_method": http_method,
                "rpc_method": rpc_method or None,
                "tool": tool or None,
                "ref": ref or None,
                "http_status": state["status"],
                "bytes_in": bytes_in["n"],
                "bytes_out": state["bytes_out"],
                "outcome": outcome,
                "error": error,
            }
            # Ecriture SQLite hors de la boucle asyncio : un verrou de base ne doit
            # jamais retarder les autres requetes MCP.
            try:
                await asyncio.to_thread(record, row)
            except BaseException:  # noqa: BLE001 -- annulation : tracer quand meme
                record(row)


# ---------------------------------------------------------------- lecture / CLI
_CONVIA_READ = "convia_read_for_analysis"
_CONVIA_WRITE = "convia_write_analysis"


def runs(last: int = 10, since: float = 0.0) -> list[dict[str, Any]]:
    """Regroupe les appels en runs : meme session (ou meme client sans session)
    et pas plus de RUN_GAP_S entre deux appels."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM mcp_calls WHERE started_at >= ? ORDER BY started_at",
            (since or time.time() - 7 * 86400,)).fetchall()
    finally:
        conn.close()
    open_runs: dict[str, dict[str, Any]] = {}
    done: list[dict[str, Any]] = []
    for r in rows:
        key = r["session"] or f"nosession:{r['client'] or '?'}"
        cur = open_runs.get(key)
        if cur is not None and r["started_at"] - cur["last_at"] > RUN_GAP_S:
            done.append(cur)
            cur = None
        if cur is None:
            cur = {"run": key[:12], "session": r["session"], "client": r["client"],
                   "start": r["started_at"], "last_at": r["started_at"], "calls": 0,
                   "convia_read": 0, "convia_write_ok": 0, "convia_write_dup": 0,
                   "convia_write_err": 0, "wiki_claim": 0, "wiki_submit_ok": 0,
                   "wiki_release": 0, "errors": 0, "max_ms": 0, "last_tool": None,
                   "end": "open", "bytes_out": 0}
            open_runs[key] = cur
        cur["calls"] += 1
        cur["last_at"] = r["started_at"] + r["duration_ms"] / 1000
        cur["max_ms"] = max(cur["max_ms"], r["duration_ms"])
        cur["bytes_out"] += r["bytes_out"] or 0
        tool, ok = r["tool"], r["outcome"] in ("ok", "duplicate")
        if tool:
            cur["last_tool"] = f"{tool}:{r['outcome']}"
        if not ok:
            cur["errors"] += 1
        if tool == _CONVIA_READ and ok:
            cur["convia_read"] += 1
        elif tool == _CONVIA_WRITE:
            if r["outcome"] == "ok":
                cur["convia_write_ok"] += 1
            elif r["outcome"] == "duplicate":
                cur["convia_write_dup"] += 1
            elif r["outcome"] == "tool_error":
                cur["convia_write_err"] += 1
        elif tool == "wiki_ingest_claim" and ok:
            cur["wiki_claim"] += 1
        elif tool == "wiki_ingest_submit" and ok:
            cur["wiki_submit_ok"] += 1
        elif tool == "wiki_ingest_release" and ok:
            cur["wiki_release"] += 1
        if r["outcome"] == "client_disconnect":
            cur["end"] = "client_disconnect"
        elif r["rpc_method"] == "session/close":
            cur["end"] = "clean_close"
    done.extend(open_runs.values())
    now = time.time()
    for run in done:
        if run["end"] == "open" and now - run["last_at"] > RUN_GAP_S:
            run["end"] = "silent_stop"
        run["duration_s"] = round(run["last_at"] - run["start"], 1)
    done.sort(key=lambda x: x["start"])
    return done[-last:]


def calls(session_prefix: str, limit: int = 500) -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT started_at, duration_ms, rpc_method, tool, ref, http_status,"
            " bytes_out, outcome, error FROM mcp_calls WHERE session LIKE ?"
            " ORDER BY started_at LIMIT ?", (session_prefix + "%", limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _hm(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%m-%d %H:%M:%S")


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("runs", "calls"):
        print("usage: python -m vault_mcp.telemetry runs [--last N] | calls <session>")
        return 2
    if argv[0] == "runs":
        last = int(argv[argv.index("--last") + 1]) if "--last" in argv else 10
        print(f"{'RUN':<13}{'START(UTC)':<16}{'DUR':>7}{'CALLS':>6}{'READ':>5}"
              f"{'W_OK':>5}{'W_DUP':>6}{'W_ERR':>6}{'CLAIM':>6}{'SUBMIT':>7}"
              f"{'ERR':>4}{'MAXMS':>7}  END / LAST")
        for r in runs(last):
            print(f"{r['run']:<13}{_hm(r['start']):<16}{r['duration_s']:>6}s{r['calls']:>6}"
                  f"{r['convia_read']:>5}{r['convia_write_ok']:>5}{r['convia_write_dup']:>6}"
                  f"{r['convia_write_err']:>6}{r['wiki_claim']:>6}{r['wiki_submit_ok']:>7}"
                  f"{r['errors']:>4}{r['max_ms']:>7}  {r['end']} / {r['last_tool']}")
        return 0
    for c in calls(argv[1] if len(argv) > 1 else ""):
        print(f"{_hm(c['started_at'])} {c['duration_ms']:>6}ms {c['rpc_method'] or '':<22}"
              f"{c['tool'] or '':<30}{c['ref'] or '':<16}{c['http_status']:>4}"
              f"{c['bytes_out']:>8} {c['outcome']} {c['error'] or ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
