#!/bin/bash
# Pre-requis canary vault : le Bearer edge est le `mcp.env` prod PARTAGE en
# lecture seule (SANS copie — la facade retransmet le Bearer client a
# l'upstream, meme emetteur, meme magasin). Ce script verifie la lisibilite
# (longueur du fichier uniquement, jamais de valeur) et supprime tout fichier
# canary orphelin.
set -euo pipefail
sudo -u juliann-app test -r /opt/vault-mcp/mcp.env
echo "[bearer] mcp.env prod lisible par juliann-app (partage sans copie)"
ORPHELIN=/opt/vault-mcp-rs/.mcp_token
if [ -e "$ORPHELIN" ]; then
  sudo rm -f "$ORPHELIN"
  echo "[bearer] fichier canary orphelin supprime"
fi
sudo wc -c /opt/vault-mcp/mcp.env
