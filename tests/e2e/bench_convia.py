"""Mesure du workflow ConvIA actuel (1 list, puis 1 read + 1 write par conversation),
hors temps LLM, sur le serveur reel du banc.

Usage : python -m tests.e2e.bench_convia [tiny|realistic] [--json out.json]
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

from tests.e2e import canary
from tests.e2e.harness import Bench, RawClient


def run(profile: str, n: int) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="convia-bench-") as tmp:
        bench = Bench(root=Path(tmp))
        bench.start()
        try:
            convs = (canary.tiny(n) if profile == "tiny"
                     else canary.realistic(n, cap=3_500_000))
            c = RawClient(bench.url)
            c.initialize()
            for conv in convs:
                bench.add_conversation(canary.SOURCE, conv.name, conv.body)
            c.call("convia_scan")
            c.calls.clear()
            t0 = time.monotonic()
            listed = c.call("convia_list_pending_analysis", limit=50)
            reads = writes = 0
            bytes_read = 0
            for i, item in enumerate(listed["items"]):
                read = c.call("convia_read_for_analysis", path=item["path"])
                bytes_read += c.calls[-1][2]
                reads += 1
                res = c.call("convia_write_analysis", source_path=read["path"],
                             source_hash=read["source_sha256"],
                             analysis_version=read["analysis_version"],
                             markdown=canary.analysis_markdown(i))
                writes += 0 if res.get("duplicate") else 1
            total = time.monotonic() - t0
            remaining = c.call("convia_list_pending_analysis", limit=50)["pending_total"]
            c.close()
            dur = {k: [d for t, d, _ in c.calls if t == k] for k in
                   ("convia_list_pending_analysis", "convia_read_for_analysis",
                    "convia_write_analysis")}
            return {
                "profile": profile, "n": n, "mcp_calls": len(c.calls) - 1,
                "list_s": round(sum(dur["convia_list_pending_analysis"][:1]), 3),
                "reads": reads, "read_s": round(sum(dur["convia_read_for_analysis"]), 3),
                "writes_confirmed": writes,
                "write_s": round(sum(dur["convia_write_analysis"]), 3),
                "total_s_hors_llm": round(total, 3),
                "read_bytes_total": bytes_read,
                "read_bytes_max": max((b for t, _, b in c.calls
                                       if t == "convia_read_for_analysis"), default=0),
                "remaining": remaining,
            }
        finally:
            bench.stop()


def main() -> int:
    profile = sys.argv[1] if len(sys.argv) > 1 else "tiny"
    rows = [run(profile, n) for n in (1, 5, 10, 25, 50)]
    for r in rows:
        print(json.dumps(r))
    if "--json" in sys.argv:
        Path(sys.argv[sys.argv.index("--json") + 1]).write_text(json.dumps(rows, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
