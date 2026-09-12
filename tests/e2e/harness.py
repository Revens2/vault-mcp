"""Banc E2E : le VRAI serveur `vault_mcp.server`, lance en sous-processus uvicorn
sur un port local ephemere, avec des repertoires et bases jetables.

Deux clients :
- `RawClient` parle JSON-RPC sur HTTP comme le connecteur ChatGPT (`openai-mcp`) :
  initialize -> notifications/initialized -> tools/call, session `Mcp-Session-Id`.
  Il sait aussi PERDRE une reponse (fermer la socket juste apres l'envoi).
- `sdk_call` utilise le client officiel `mcp` (streamable HTTP) pour un test de
  conformite transport.

Rien ici ne touche au VPS, a Google Drive, au Vault reel ou a un LLM. Toute
variable de chemin est posee explicitement : un chemin de production par defaut
(`/srv/...`, `/var/lib/...`, `/opt/...`) dans l'environnement du serveur fait
echouer le demarrage du banc (`assert_isolated`).
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parents[2]
FACTICE = REPO / "tests" / "fixtures" / "contrat_wiki_factice.py"
TOKEN = "e2e-" + "t" * 40
PROD_PREFIXES = ("/srv/", "/var/lib/", "/opt/", "/usr/local/")
PROTOCOL = "2025-06-18"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class Bench:
    root: Path
    port: int = field(default_factory=free_port)
    proc: subprocess.Popen[bytes] | None = None
    extra_env: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------- chemins
    @property
    def mirror(self) -> Path:
        return self.root / "mirror"

    @property
    def convia_root(self) -> Path:
        return self.mirror / "raw" / "assets" / "ConvIA"

    @property
    def wiki_raw(self) -> Path:
        return self.mirror / "raw"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def env(self) -> dict[str, str]:
        r = self.root
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(r / "home"),
            "PYTHONPATH": str(REPO),
            "PYTHONDONTWRITEBYTECODE": "1",
            "MCP_PORT": str(self.port),
            "VAULT_MCP_ISSUER": "https://e2e.invalid",
            "VAULT_MCP_OAUTH_DIR": str(r / "oauth"),
            "VAULT_MCP_OAUTH_CLIENTS": str(r / "oauth" / "clients.json"),
            "VAULT_MCP_TOKEN": TOKEN,
            "VAULT_MCP_TOKEN_SCOPES": "mcp:lecture mcp:ecriture",
            "VAULT_MCP_ECRITURE": "1",
            "VAULT_MCP_VAULT": str(self.mirror),
            "VAULT_MCP_INDEX": str(r / "index"),
            "VAULT_MCP_SPOOL": str(r / "spool"),
            "VAULT_MCP_DIRTY": str(r / "dirty"),
            "VAULT_MCP_TELEMETRY_DB": str(r / "telemetry.db"),
            "CONVIA_QUEUE_DB": str(r / "convia.db"),
            "CONVIA_RAW_ROOT": str(self.convia_root),
            "CONVIA_MIRROR_ROOT": str(self.mirror),
            "WIKI_JOBS_DB": str(r / "wiki_jobs.db"),
            "WIKI_JOBS_SPOOL": str(r / "wiki-spool"),
            "WIKI_JOBS_MANIFEST": str(r / "manifest.jsonl"),
            "WIKI_RAW_DIR": str(self.wiki_raw),
            "WIKI_EXCLUDE_DIRS": str(self.convia_root),
            "WIKI_NOTES_DIR": str(r / "wiki"),
            "WIKI_CONTRACT_MODULE": str(FACTICE),
            "WIKI_INGEST_REQUEST": str(r / "wiki-ingest.request"),
            "WIKI_ALTERNATE_REQUEST": str(r / "wiki-alternate.request"),
            "WIKI_INGEST_STATE": str(r / "llm-wiki"),
            "WIKI_INGEST_BIN": str(r / "absent"),
            "WIKI_INGEST_UNIT": "e2e-absent.service",
            "WIKI_MIN_LEASE_S": "1",
        }
        env.update(self.extra_env)
        return env

    def assert_isolated(self) -> None:
        for key, value in self.env().items():
            if key in ("PATH", "PYTHONPATH"):
                continue
            assert not value.startswith(PROD_PREFIXES), f"{key} pointe vers la prod : {value}"

    # ------------------------------------------------------------- cycle
    def prepare(self) -> None:
        for d in (self.convia_root, self.root / "oauth", self.root / "index",
                  self.root / "spool" / "tmp", self.root / "spool" / "queue",
                  self.root / "dirty", self.root / "wiki" / "sources",
                  self.root / "home"):
            d.mkdir(parents=True, exist_ok=True)

    def start(self, timeout: float = 30.0) -> None:
        self.assert_isolated()
        self.prepare()
        log = (self.root / "server.log").open("ab")
        self.proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-m", "vault_mcp.server"], cwd=REPO, env=self.env(),
            stdout=log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("serveur mort au demarrage :\n" + self.log_tail())
            with contextlib.suppress(OSError), socket.create_connection(
                    ("127.0.0.1", self.port), timeout=0.2):
                return
            time.sleep(0.1)
        raise RuntimeError("serveur non joignable :\n" + self.log_tail())

    def stop(self, sig: int = signal.SIGTERM) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(sig)
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc = None

    def log_tail(self, n: int = 40) -> str:
        p = self.root / "server.log"
        return "\n".join(p.read_text(errors="replace").splitlines()[-n:]) if p.exists() else ""

    # ------------------------------------------------------------- donnees
    def add_conversation(self, source: str, name: str, body: str) -> str:
        path = self.convia_root / source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return f"raw/assets/ConvIA/{source}/{name}"

    def add_wiki_doc(self, name: str, body: str) -> Path:
        path = self.wiki_raw / "canary-e2e" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def sql(self, db: str, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        import sqlite3

        conn = sqlite3.connect(self.root / db, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(query, params).fetchall()]
        finally:
            conn.close()


# ------------------------------------------------------------------ client brut
class ToolRefusedError(RuntimeError):
    pass


def _parse_body(resp: httpx.Response) -> dict[str, Any]:
    ctype = resp.headers.get("content-type", "")
    text = resp.text
    if "text/event-stream" in ctype:
        for line in text.splitlines():
            if line.startswith("data:"):
                doc = json.loads(line[5:].strip())
                if "result" in doc or "error" in doc:
                    return doc  # type: ignore[no-any-return]
        raise RuntimeError(f"SSE sans reponse JSON-RPC : {text[:200]}")
    return resp.json()  # type: ignore[no-any-return]


class RawClient:
    """Client JSON-RPC minimal, au plus pres de ce qu'envoie le connecteur ChatGPT."""

    def __init__(self, url: str, token: str = TOKEN, timeout: float = 60.0) -> None:
        self.url = url
        self.token = token
        self.session: str | None = None
        self._id = 0
        self.http = httpx.Client(timeout=timeout)
        self.calls: list[tuple[str, float, int]] = []

    def _headers(self) -> dict[str, str]:
        h = {"authorization": f"Bearer {self.token}", "content-type": "application/json",
             "accept": "application/json, text/event-stream",
             "user-agent": "e2e-consumer/1.0", "mcp-protocol-version": PROTOCOL}
        if self.session:
            h["mcp-session-id"] = self.session
        return h

    def _next(self) -> int:
        self._id += 1
        return self._id

    def initialize(self) -> None:
        body = {"jsonrpc": "2.0", "id": self._next(), "method": "initialize",
                "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                           "clientInfo": {"name": "e2e-consumer", "version": "1.0"}}}
        resp = self.http.post(self.url, json=body, headers=self._headers())
        resp.raise_for_status()
        self.session = resp.headers.get("mcp-session-id")
        _parse_body(resp)
        self.http.post(self.url, json={"jsonrpc": "2.0",
                                       "method": "notifications/initialized"},
                       headers=self._headers())

    def call(self, tool: str, **args: Any) -> dict[str, Any]:
        body = {"jsonrpc": "2.0", "id": self._next(), "method": "tools/call",
                "params": {"name": tool, "arguments": args}}
        t0 = time.monotonic()
        resp = self.http.post(self.url, json=body, headers=self._headers())
        self.calls.append((tool, time.monotonic() - t0, len(resp.content)))
        resp.raise_for_status()
        doc = _parse_body(resp)
        if "error" in doc:
            raise ToolRefusedError(str(doc["error"]))
        result = doc["result"]
        data = result.get("structuredContent")
        if data is None:
            texts = [c.get("text", "") for c in result.get("content", [])]
            data = json.loads(texts[0]) if texts else {}
        if isinstance(data, dict) and set(data) == {"result"}:
            data = data["result"]
        if result.get("isError") or (isinstance(data, dict) and data.get("etat") == "refuse"):
            raise ToolRefusedError(str(data.get("message") if isinstance(data, dict) else data))
        return data  # type: ignore[no-any-return]

    def call_and_lose_response(self, tool: str, **args: Any) -> None:
        """Envoie un tools/call complet puis coupe la socket sans lire la reponse.

        Le serveur recoit la requete entiere et execute l'outil ; le client, lui,
        ne saura jamais s'il a reussi. C'est la « reponse perdue ».
        """
        payload = json.dumps({"jsonrpc": "2.0", "id": self._next(), "method": "tools/call",
                              "params": {"name": tool, "arguments": args}}).encode()
        host, port = "127.0.0.1", int(self.url.split(":")[2].split("/")[0])
        head = ["POST /mcp HTTP/1.1", f"host: {host}:{port}",
                f"content-length: {len(payload)}"]
        head += [f"{k}: {v}" for k, v in self._headers().items()]
        raw = ("\r\n".join(head) + "\r\n\r\n").encode() + payload
        with socket.create_connection((host, port), timeout=5) as sock:
            sock.sendall(raw)
            # Laisser le serveur lire le corps et lancer l'outil, puis couper.
            time.sleep(0.3)
            # SO_LINGER (1, 0) : fermeture par RST, sans attendre la reponse.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01" + b"\x00" * 7)

    def close(self) -> None:
        if self.session:
            with contextlib.suppress(httpx.HTTPError):
                self.http.delete(self.url, headers=self._headers())
        self.http.close()


# ------------------------------------------------------------------ client SDK
def sdk_call(url: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    import anyio
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    async def run() -> dict[str, Any]:
        client = create_mcp_http_client(headers={"authorization": f"Bearer {TOKEN}"})
        async with client, streamable_http_client(url, http_client=client) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.call_tool(tool, args)
                data = res.structured_content
                if data is None:
                    data = json.loads(res.content[0].text)  # type: ignore[union-attr]
                return data  # type: ignore[no-any-return]

    return anyio.run(run)
