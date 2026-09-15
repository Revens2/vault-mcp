#!/usr/bin/env python3
"""Rafraichit la voie fraiche (vault_mcp.frais). Zero LLM, zero embedding, quelques secondes."""

from __future__ import annotations

import sys
import time

from vault_mcp import frais


def main() -> int:
    debut = time.time()
    resultat = frais.rafraichir()
    print(
        f"frais : {resultat['notes']} note(s), +{resultat['ajoutees']} "
        f"-{resultat['retirees']}, {time.time() - debut:.1f}s",
        file=sys.stderr,
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
