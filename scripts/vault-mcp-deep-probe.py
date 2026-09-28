#!/usr/bin/env python3
"""Sonde applicative profonde de vault-mcp : une VRAIE recherche de bout en bout.

Le healthcheck historique ne voyait que le 401/200 de la porte d'entree : un
processus fige dans ses threads d'outils (reclaim memoire, 27-28/09/2026)
continuait de repondre a `initialize` pendant des minutes. Ici on fait
initialize -> initialized -> tools/call search_vault, borne par un delai.

Sortie : 0 = recherche OK, 1 = echec/delai. Une ligne sur stdout.
Le jeton est lu dans mcp.env (root uniquement), jamais affiche.
"""
import json
import sys
import http.client
import time

# Hote et port fixes (http.client) : aucun schema dynamique possible (file://...).
HOTE, PORT, CHEMIN = "127.0.0.1", 8787, "/mcp"
ENV = "/opt/vault-mcp/mcp.env"
DELAI = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0


def jeton() -> str:
    with open(ENV, encoding="utf-8") as f:
        for ligne in f:
            if ligne.startswith("VAULT_MCP_TOKEN="):
                return ligne.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("jeton absent")


def poster(corps: dict, entetes: dict, reste: float) -> tuple[dict, str | None]:
    connexion = http.client.HTTPConnection(HOTE, PORT, timeout=max(reste, 1.0))
    try:
        connexion.request("POST", CHEMIN, body=json.dumps(corps).encode(), headers=entetes)
        rep = connexion.getresponse()
        if rep.status >= 400:
            raise RuntimeError(f"HTTP {rep.status}")
        session = rep.getheader("mcp-session-id")
        brut = rep.read().decode("utf-8", "replace")
    finally:
        connexion.close()
    if not brut.strip():
        return {}, session
    if brut.lstrip().startswith("{"):
        return json.loads(brut), session
    # SSE : garder le dernier evenement data: JSON-RPC
    dernier = {}
    for ligne in brut.splitlines():
        if ligne.startswith("data:"):
            try:
                dernier = json.loads(ligne[5:].strip())
            except ValueError:
                pass
    return dernier, session


def main() -> int:
    t0 = time.monotonic()
    entetes = {
        "Authorization": f"Bearer {jeton()}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    try:
        rep, session = poster({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "deep-probe", "version": "1"}}}, entetes, DELAI)
        if "result" not in rep:
            print(f"DEEP_FAIL initialize {str(rep)[:120]}")
            return 1
        if session:
            entetes["mcp-session-id"] = session
        poster({"jsonrpc": "2.0", "method": "notifications/initialized"}, entetes, DELAI - (time.monotonic() - t0))
        rep, _ = poster({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "search_vault", "arguments": {"query": "vault-mcp healthcheck", "limit": 1}}},
            entetes, DELAI - (time.monotonic() - t0))
    except Exception as exc:  # noqa: BLE001
        print(f"DEEP_FAIL {type(exc).__name__} apres {time.monotonic() - t0:.1f}s")
        return 1
    duree = time.monotonic() - t0
    res = rep.get("result") or {}
    if "error" in rep or res.get("isError"):
        print(f"DEEP_FAIL outil {str(rep)[:160]} {duree:.1f}s")
        return 1
    print(f"DEEP_OK {duree:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
