"""Administration locale de la file Wiki (jamais exposee en MCP).

Remise en file des quarantaines dont la cause est reparee, sortie definitive des
sources reellement vides, lecture du journal d'audit. Dry-run par defaut : rien ne
change sans `--apply`, et `--apply` exige une `--cause` (tracee dans wiki_job_events).

    sudo -u juliann-app /opt/vault-mcp/venv/bin/python -m vault_mcp.wiki_admin status
    ... requeue --reason "non expos" --limit 5                      # dry-run
    ... requeue --reason "non expos" --limit 5 --apply --cause "contrat expose le 2026-09-10"
    ... requeue --job 64b6bd8b --target alternate --apply --cause "SKIPPED_SAFETY"
    ... skip --job afaf1fe2 --apply --cause "source vide"
    ... events --job afaf1fe2
"""

from __future__ import annotations

import argparse
import json
import sys

from vault_mcp import wiki_jobs


def _print(obj: object) -> None:
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=1, default=str)
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="wiki_admin", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    rq = sub.add_parser("requeue", help="quarantaines -> file (dry-run par defaut)")
    rq.add_argument("--job", action="append", default=[], help="job_id ou prefixe >= 8 hex")
    rq.add_argument("--reason", default=None, help="sous-chaine de last_error")
    rq.add_argument("--limit", type=int, default=10)
    rq.add_argument("--target", choices=("pending", "alternate"), default="pending")
    rq.add_argument("--apply", action="store_true")
    rq.add_argument("--cause", default="")
    of = sub.add_parser("offload", help="file ChatGPT -> route alternative"
                        " (dry-run par defaut)")
    of.add_argument("--limit", type=int, default=50)
    of.add_argument("--apply", action="store_true")
    of.add_argument("--cause", default="")
    sk = sub.add_parser("skip", help="quarantaine -> terminal_skip (source vide/inutile)")
    sk.add_argument("--job", action="append", required=True)
    sk.add_argument("--apply", action="store_true")
    sk.add_argument("--cause", default="")
    ev = sub.add_parser("events", help="journal d'audit d'un job")
    ev.add_argument("--job", required=True)
    ev.add_argument("--limit", type=int, default=50)
    a = p.parse_args(argv)
    try:
        if a.cmd == "status":
            _print(wiki_jobs.status())
        elif a.cmd == "requeue":
            _print(wiki_jobs.requeue(job_ids=a.job or None, reason_like=a.reason,
                                     limit=a.limit, dry_run=not a.apply, cause=a.cause,
                                     target=a.target, actor="wiki_admin"))
        elif a.cmd == "offload":
            _print(wiki_jobs.offload(limit=a.limit, dry_run=not a.apply,
                                     cause=a.cause, actor="wiki_admin"))
        elif a.cmd == "skip":
            _print(wiki_jobs.terminal_skip(a.job, a.cause, dry_run=not a.apply,
                                           actor="wiki_admin"))
        else:
            _print(wiki_jobs.events(a.job, a.limit))
    except wiki_jobs.WikiJobsError as exc:
        print(f"refus : {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
