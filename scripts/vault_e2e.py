"""Probe E2E Vault (Phase 6) — distingue EDGE vs Rust vs Python.

Etats : EDGE_DOWN / EDGE_AUTH_ALIVE / RUST_ALIVE / PYTHON_DOWN /
MCP_INITIALIZE_FAILED / TOOLS_LIST_FAILED / E2E_HEALTHY.

Token jamais en argv/logs : lu depuis fichier env (0600) en memoire.
Usage : VAULT_E2E_BASE=http://127.0.0.1:18987 VAULT_E2E_TOKEN_FILE=/opt/vault-mcp/mcp.env python3 vault_e2e.py
Ne fait que des lectures : initialize + tools/list. Aucun restart/reload.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request


def tcp_ok(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def load_token(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("VAULT_MCP_TOKEN="):
                return line.strip().split("=", 1)[1]
    raise SystemExit("secret introuvable (fichier env sans VAULT_MCP_TOKEN)")


def rpc(base: str, token: str | None, method: str, timeout: float = 10.0):
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "watchdog", "version": "1"},
        },
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(
        base + "/mcp", data=json.dumps(body).encode(), headers=headers
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            # tools/list fait ~38 outils (dizaines de Ko en SSE) : lire assez
            # large, sinon le JSON tronque et le comptage tombe a -1.
            return r.status, time.monotonic() - t0, r.read(512 * 1024)
    except urllib.error.HTTPError as e:
        # 401 sans Bearer = vivant (EDGE_AUTH_ALIVE), pas une panne.
        try:
            body = e.read(4096)
        except Exception:
            body = b""
        return e.code, time.monotonic() - t0, body
    except Exception as e:  # noqa: BLE001 - verdict, pas stack
        return f"EXC:{type(e).__name__}", time.monotonic() - t0, b""


def main() -> int:
    base = os.environ.get("VAULT_E2E_BASE", "http://127.0.0.1:18987")
    token_file = os.environ.get("VAULT_E2E_TOKEN_FILE", "/opt/vault-mcp/mcp.env")
    # 1. Transport
    if not tcp_ok("127.0.0.1", 18987) and not tcp_ok("127.0.0.1", 8788):
        print("EDGE_DOWN")
        return 2
    # 2. Edge auth (401 rapide attendu, jamais E2E_HEALTHY)
    code, dt, _ = rpc(base, None, "initialize", timeout=10)
    if code == 401:
        print(f"EDGE_AUTH_ALIVE ({dt:.3f}s)")
    # 3. Rust liveness
    try:
        with urllib.request.urlopen(base.replace("/mcp", "") + "/health", timeout=5) as r:
            if r.status == 200:
                print("RUST_ALIVE")
    except Exception:
        pass
    # 4. Python local (401 sans Bearer = vivant, pas bloque)
    code_py, dt_py, _ = rpc("http://127.0.0.1:8787", None, "initialize", timeout=10)
    if isinstance(code_py, str) or (isinstance(code_py, int) and code_py >= 500):
        print(f"PYTHON_DOWN ({code_py} {dt_py:.1f}s)")
        return 3
    print(f"PYTHON_ALIVE ({code_py} {dt_py:.3f}s)")
    # 5. E2E authentifie
    try:
        token = load_token(token_file)
    except SystemExit as e:
        print(f"MCP_INITIALIZE_FAILED ({e})")
        return 4
    code_i, dt_i, _ = rpc(base, token, "initialize", timeout=15)
    if code_i != 200:
        print(f"MCP_INITIALIZE_FAILED ({code_i} {dt_i:.1f}s)")
        return 4
    code_t, dt_t, body = rpc(base, token, "tools/list", timeout=15)
    if code_t != 200:
        print(f"TOOLS_LIST_FAILED ({code_t} {dt_t:.1f}s)")
        return 5
    try:
        text = body.decode("utf-8", "ignore")
        # Reponse streamable-http (SSE `data: {...}`) ou JSON direct.
        if "data:" in text:
            payloads = [l[5:].strip() for l in text.splitlines() if l.strip().startswith("data:")]
            text = payloads[-1] if payloads else text
        n = len(json.loads(text).get("result", {}).get("tools", []))
        if n == 0 and '"tools"' in text:
            n = text.count('"name"')
    except Exception:
        n = -1
    print(f"E2E_HEALTHY (initialize {dt_i:.2f}s, tools/list {dt_t:.2f}s, tools={n})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
