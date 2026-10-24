#!/usr/bin/env python3
"""Revocation administrative des jetons OAuth du vault (issue #11).

Usage (sur l'hote, compte de service, VAULT_MCP_OAUTH_DIR si non standard) :
  oauth_revoquer.py --lister            nombre de jetons vivants par client
  oauth_revoquer.py --client CLIENT_ID  revoque tous les jetons de ce client
  oauth_revoquer.py --tout              revoque tous les jetons OAuth (re-consentement)
  oauth_revoquer.py --migrer            reecrit un etat herite en clair par empreinte (#14)

N'affiche jamais un jeton. Ne touche ni au registre des clients ni au Bearer statique
(VAULT_MCP_TOKEN), qui se tourne a la main. Effet immediat : le serveur relit l'etat
a chaque requete, aucun redemarrage n'est necessaire.
"""

from __future__ import annotations

import argparse
import sys

from vault_mcp.oauth import MagasinOAuth


def main(argv: list[str] | None = None) -> int:
    analyseur = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    groupe = analyseur.add_mutually_exclusive_group(required=True)
    groupe.add_argument("--lister", action="store_true")
    groupe.add_argument("--client")
    groupe.add_argument("--tout", action="store_true")
    groupe.add_argument("--migrer", action="store_true")
    args = analyseur.parse_args(argv)

    magasin = MagasinOAuth()
    if args.lister:
        for client_id, compte in sorted(magasin.resume().items()):
            print(f"{client_id}\tacces={compte['acces']}\trafraichissements={compte['rafraichissements']}")
        return 0
    if args.migrer:
        print(f"jetons migres: {magasin.migrer_etat()}")
        return 0
    retires = magasin.revoquer_client(None if args.tout else args.client)
    print(f"jetons revoques: {retires}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
