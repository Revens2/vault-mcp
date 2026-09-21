#!/usr/bin/env python3
"""Smoke/E2E du MCP `vault-context` par JSON-RPC brut (sans dependre de l'API client du SDK).

Usage : VAULT_CONTEXT_TOKEN=... smoke_contexte.py URL REQUETE [CHEMIN_ATTENDU]
Sort en 0 si : 401 sans jeton, outils == surface lecture, et (si fourni) CHEMIN_ATTENDU
dans le top 10 de search_vault(REQUETE). Le jeton n'est jamais affiche.
"""

from __future__ import annotations

import json
import os
import sys

import httpx

ACCEPT = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def _json(reponse: httpx.Response) -> dict:
    if reponse.headers.get("content-type", "").startswith("text/event-stream"):
        for ligne in reponse.text.splitlines():
            if ligne.startswith("data:"):
                return json.loads(ligne[5:])
        raise RuntimeError("flux SSE sans donnees")
    return reponse.json()


def main() -> int:
    url, requete = sys.argv[1], sys.argv[2]
    attendu = sys.argv[3] if len(sys.argv) > 3 else ""
    jeton = os.environ["VAULT_CONTEXT_TOKEN"]
    with httpx.Client(timeout=60) as client:
        anonyme = client.post(url, headers=ACCEPT, json={"jsonrpc": "2.0", "id": 0, "method": "ping"})
        print("sans jeton ->", anonyme.status_code)
        assert anonyme.status_code == 401

        entetes = {**ACCEPT, "authorization": f"Bearer {jeton}"}
        init = client.post(url, headers=entetes, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "smoke-contexte", "version": "1"}},
        })
        init.raise_for_status()
        serveur = _json(init)["result"]["serverInfo"]
        print("serveur ->", serveur)
        session = init.headers.get("mcp-session-id")
        if session:
            entetes["mcp-session-id"] = session
        if "mcp-protocol-version" in init.headers:
            entetes["mcp-protocol-version"] = init.headers["mcp-protocol-version"]
        client.post(url, headers=entetes, json={"jsonrpc": "2.0", "method": "notifications/initialized"})

        outils = _json(client.post(url, headers=entetes,
                                   json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
        noms = sorted(o["name"] for o in outils["result"]["tools"])
        print("outils ->", noms)
        assert set(noms) == {"list_notes", "read_note", "search_notes", "search_vault",
                             "get_graph_context", "context_status"}

        def appeler(nom: str, arguments: dict) -> object:
            brut = _json(client.post(url, headers=entetes, json={
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": nom, "arguments": arguments}}))["result"]
            if "structuredContent" in brut:
                contenu = brut["structuredContent"]
                return contenu.get("result", contenu)
            return json.loads(brut["content"][0]["text"])

        print("context_status ->", appeler("context_status", {}))
        resultats = appeler("search_vault", {"query": requete, "limit": 10})
        for r in resultats[:5]:
            print(f"  {r['origine']:8} {r['score']:.4f} {r['chemin']}")
        if attendu:
            assert attendu in [r["chemin"] for r in resultats], "chemin attendu absent du top 10"
            lu = appeler("read_note", {"path": attendu, "limit": 200})
            assert isinstance(lu, str) and not lu.startswith(("NOT FOUND", "ERREUR"))
            print("read_note -> ok")
    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
