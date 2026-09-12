"""Consommateur jetable, lance en SOUS-PROCESSUS pour pouvoir etre tue (SIGKILL).

Imite le deroule de la tache ChatGPT : il annonce chaque frontiere par une ligne
`CHECKPOINT <nom>` sur stdout puis, s'il atteint `--stop-at`, s'endort. Le test
tue alors le processus a cet instant precis : aucune reponse, aucun release,
aucune fermeture de session — exactement un consommateur qui disparait.

Scenarios :
  convia : status -> list -> read(first) -> write(first)
  wiki   : sync -> claim(1) -> read -> submit -> merge
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from tests.e2e import canary
from tests.e2e.harness import RawClient


def checkpoint(name: str, **data: object) -> None:
    print(f"CHECKPOINT {name} {json.dumps(data)}", flush=True)


def maybe_stop(name: str, stop_at: str) -> None:
    if name == stop_at:
        print(f"PARKED {name}", flush=True)
        time.sleep(3600)


def run_convia(c: RawClient, stop_at: str) -> None:
    c.call("convia_status")
    maybe_stop("status", stop_at)
    listed = c.call("convia_list_pending_analysis", limit=50)
    first = listed["items"][0]
    checkpoint("list", path=first["path"], count=listed["returned"])
    maybe_stop("list", stop_at)
    read = c.call("convia_read_for_analysis", path=first["path"])
    checkpoint("read", path=read["path"], hash=read["source_sha256"])
    maybe_stop("read", stop_at)
    res = c.call("convia_write_analysis", source_path=read["path"],
                 source_hash=read["source_sha256"],
                 analysis_version=read["analysis_version"],
                 markdown=canary.analysis_markdown(0))
    checkpoint("write", path=res.get("path"))
    maybe_stop("write", stop_at)


def run_wiki(c: RawClient, stop_at: str, lease: int) -> None:
    c.call("wiki_ingest_sync", limit_files=200)
    got = c.call("wiki_ingest_claim", limit=1, lease_seconds=lease)
    job = got["jobs"][0]
    checkpoint("claim", job_id=job["job_id"], lease_id=job["lease_id"],
               fencing=job["fencing_token"], digest=job["contract_digest"])
    maybe_stop("claim", stop_at)
    c.call("wiki_ingest_read", job_id=job["job_id"], lease_id=job["lease_id"])
    checkpoint("read", job_id=job["job_id"])
    maybe_stop("read", stop_at)
    extraction = canary.wiki_extraction("canary-" + job["job_id"][:10])
    rec = c.call("wiki_ingest_submit", job_id=job["job_id"], lease_id=job["lease_id"],
                 fencing_token=job["fencing_token"],
                 contract_version=job["contract_version"], extraction=extraction,
                 contract_digest=job["contract_digest"])
    checkpoint("submit", receipt=rec["receipt_id"], job_id=job["job_id"])
    maybe_stop("submit", stop_at)
    c.call("wiki_ingest_merge_pending")
    checkpoint("merge")
    maybe_stop("merge", stop_at)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--scenario", choices=("convia", "wiki"), required=True)
    ap.add_argument("--stop-at", default="")
    ap.add_argument("--lease", type=int, default=2)
    a = ap.parse_args()
    c = RawClient(a.url)
    c.initialize()
    checkpoint("init", session=c.session)
    if a.scenario == "convia":
        run_convia(c, a.stop_at)
    else:
        run_wiki(c, a.stop_at, a.lease)
    c.close()
    checkpoint("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
