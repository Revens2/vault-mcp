#!/bin/bash
# verify_deploy.sh — prouve que l'octet deploye == main (standardise la recette PR #4).
# Lecture seule : ne modifie ni /opt/vault-mcp ni aucun service.
#
# Usage (VPS, root ou lecture sur /opt/vault-mcp) :
#   sudo bash scripts/verify_deploy.sh /tmp/vault-mcp-main.tar.gz $SHA
# Sort en 0 si : SHA tarball OK, diff vide (hors runtime), 3 fichiers cles
# identiques, units actives, ports en ecoute.
set -euo pipefail
TARBALL="${1:?usage: verify_deploy.sh <tarball> <sha256>}"
ATTENDU="${2:?usage: verify_deploy.sh <tarball> <sha256>}"
EC=0

CONSTATE=$(sha256sum "$TARBALL" | cut -d' ' -f1)
if [ "$CONSTATE" = "$ATTENDU" ]; then echo "SHA-TARBALL-OK $CONSTATE";
else echo "SHA-TARBALL-KO $CONSTATE != $ATTENDU"; EC=1; fi

VERIF=/tmp/vault-mcp-verify
rm -rf "$VERIF"; mkdir -p "$VERIF"
tar -xzf "$TARBALL" -C "$VERIF"
for f in vault_mcp/embed.py vault_mcp/index.py vault_mcp/server.py; do
  a=$(sha256sum "$VERIF/vault-mcp/$f" | cut -c1-8)
  b=$(sha256sum "/opt/vault-mcp/$f" 2>/dev/null | cut -c1-8 || echo ABSENT)
  if [ "$a" = "$b" ]; then echo "FICHIER-OK $f $a";
  else echo "FICHIER-KO $f tarball=$a deploye=$b"; EC=1; fi
done

# NOTE: pas de slash final sur les motifs de repertoires — `diff --exclude`
# ne les exclut pas avec (teste : --exclude=index/ ne mord pas).
EXCLUS=(--exclude=index --exclude=models --exclude=oauth --exclude=venv
        --exclude=bin --exclude=backups --exclude=.cache
        --exclude=__pycache__ --exclude=*.env --exclude=*.bak* --exclude=*.tmp
        --exclude=*.log --exclude=plan.md --exclude=errors.md)
if diff -r -q "${EXCLUS[@]}" "$VERIF/vault-mcp/" /opt/vault-mcp/ > /tmp/vault-mcp-verify-diff.txt 2>&1; then
  echo "DIFF-VIDE-OK"
else
  echo "DIFF-NON-VIDE (voir /tmp/vault-mcp-verify-diff.txt)"; EC=1
fi

systemctl is-active --quiet vault-mcp.service && echo "UNIT-OK vault-mcp active" || { echo "UNIT-KO vault-mcp"; EC=1; }
ss -tln 2>/dev/null | grep -q ':8787' && echo "PORT-OK 8787" || { echo "PORT-KO 8787"; EC=1; }
ss -tln 2>/dev/null | grep -q ':8788' && echo "PORT-OK 8788 (nginx)" || echo "PORT-ABSENT 8788 (non bloquant)"
systemctl --failed --no-legend 2>/dev/null | grep -q . && { echo "FAILED-UNITS (voir systemctl --failed)"; EC=1; } || echo "NO-FAILED-OK"

rm -rf "$VERIF"
[ "$EC" -eq 0 ] && echo "VERIFY-OK" || echo "VERIFY-KO"
exit "$EC"
