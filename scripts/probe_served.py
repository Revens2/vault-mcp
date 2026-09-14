"""Sonde de la surface SERVIE : golden via search_vault + statuts, sur 127.0.0.1:8787/mcp.

Jeton lu dans mcp.env en memoire, jamais affiche. Lecture seule (aucun outil d'ecriture).
Usage : probe_served.py golden.jsonl out.json
"""

# ruff: noqa: E501, E741, SIM115, PTH123

import asyncio
import json
import math
import statistics
import sys
import time

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client


def jeton() -> str:
    for ligne in open("/opt/vault-mcp/mcp.env", encoding="utf-8"):
        if ligne.startswith("VAULT_MCP_TOKEN="):
            return ligne.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("jeton absent")


def charge(res):
    sc = getattr(res, "structured_content", None)
    if sc:
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    txt = "".join(getattr(c, "text", "") for c in res.content)
    try:
        return json.loads(txt)
    except ValueError:
        return txt


async def main() -> None:
    golden = [json.loads(l) for l in open(sys.argv[1], encoding="utf-8") if l.strip()]
    entetes = {"Authorization": f"Bearer {jeton()}"}
    http = create_mcp_http_client(headers=entetes, timeout=httpx2.Timeout(120.0))
    async with http, streamable_http_client("http://127.0.0.1:8787/mcp", http_client=http) as flux:
        r, w = flux[0], flux[1]
        async with ClientSession(r, w) as s:
            await s.initialize()
            statuts = {}
            for outil in ("vault_status", "convia_status", "wiki_ingest_status"):
                t = time.perf_counter()
                res = await s.call_tool(outil, {})
                statuts[outil] = {"erreur": getattr(res, "is_error", None), "ms": round((time.perf_counter() - t) * 1000),
                                  "extrait": str(charge(res))[:400]}
            lat, rr, hits, fam, details, stale = [], [], {1: 0, 5: 0, 10: 0}, {}, [], 0
            for g in golden:
                t = time.perf_counter()
                res = await s.call_tool("search_vault", {"query": g["q"], "limit": 10})
                lat.append((time.perf_counter() - t) * 1000)
                top = [x["chemin"] if "chemin" in x else x.get("path") for x in (charge(res) or [])]
                att = tuple(g["attendus"])
                rang = next((i for i, c in enumerate(top) if c and c.startswith(att)), None)
                for k in hits:
                    hits[k] += rang is not None and rang < k
                rr.append(0.0 if rang is None else 1 / (rang + 1))
                per = tuple(g.get("perimes") or ())
                if per:
                    rp = next((i for i, c in enumerate(top[:5]) if c and c.startswith(per)), None)
                    stale += rp is not None and (rang is None or rp < rang)
                f = fam.setdefault(g["famille"], [0, 0])
                f[0] += rang is not None and rang < 5
                f[1] += 1
                details.append({"id": g["id"], "famille": g["famille"], "rang": rang, "top3": top[:3]})
    n = len(golden)
    lat.sort()
    rapport = {
        **{f"hit@{k}": round(v / n, 3) for k, v in hits.items()},
        "mrr": round(statistics.mean(rr), 3), "stale@5": stale,
        "p50_ms": round(lat[n // 2]), "p95_ms": round(lat[math.ceil(0.95 * n) - 1]),
        "familles_hit@5": {k: f"{a}/{b}" for k, (a, b) in sorted(fam.items())},
    }
    json.dump({"rapport": rapport, "statuts": statuts, "details": details},
              open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(rapport, ensure_ascii=False))
    print(json.dumps({k: {"erreur": v["erreur"], "ms": v["ms"]} for k, v in statuts.items()}))


asyncio.run(main())
