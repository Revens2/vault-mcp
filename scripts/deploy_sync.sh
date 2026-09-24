#!/bin/bash
# deploy_sync.sh — synchronise /opt/vault-mcp (vps-etude) depuis main, de facon reproductible.
# Remplace le `cp + chown + restart` manuel (issue #6) : l'octet deploye == main est verifie.
#
# Usage (VPS, en root) :
#   Poste : git archive --format=tar.gz --prefix=vault-mcp/ main -o /tmp/vault-mcp-main.tar.gz
#           SHA=$(sha256sum /tmp/vault-mcp-main.tar.gz | cut -d' ' -f1)
#           scp /tmp/vault-mcp-main.tar.gz vps-etude:/tmp/
#   VPS   : sudo bash scripts/deploy_sync.sh /tmp/vault-mcp-main.tar.gz $SHA [--apply]
#
# Sans --apply : DRY-RUN lecture seule (verifie SHA, extrait en staging, affiche le diff).
# Avec --apply : backup horodate + rsync + chown + daemon-reload. Le restart reste
# manuel (voir docs/runbook-deploy.md) : ce script ne touche jamais au runtime seul.
set -euo pipefail
TARBALL="${1:?usage: deploy_sync.sh <tarball> <sha256> [--apply]}"
ATTENDU="${2:?usage: deploy_sync.sh <tarball> <sha256> [--apply]}"
MODE="${3:---dry-run}"
[ "$(id -u)" -eq 0 ] || { echo "ERREUR: lancer en root (sudo)" >&2; exit 1; }
[ -f "$TARBALL" ] || { echo "ERREUR: tarball absent: $TARBALL" >&2; exit 1; }

CONSTATE=$(sha256sum "$TARBALL" | cut -d' ' -f1)
[ "$CONSTATE" = "$ATTENDU" ] || { echo "ERREUR: SHA tarball $CONSTATE != attendu $ATTENDU" >&2; exit 2; }
echo "SHA-OK $CONSTATE"

STAGING=/tmp/vault-mcp-staging
rm -rf "$STAGING"; mkdir -p "$STAGING"
tar -xzf "$TARBALL" -C "$STAGING"
SRC="$STAGING/vault-mcp"
[ -d "$SRC/vault_mcp" ] || { echo "ERREUR: arborescence inattendue dans $TARBALL" >&2; exit 2; }

EXCLUS=(index/ models/ oauth/ venv/ __pycache__/ .git/
        '*.env' '*.bak*' '*.tmp' '*.log' 'plan.md' 'errors.md')
FILTRE=(); for e in "${EXCLUS[@]}"; do FILTRE+=(--exclude="$e"); done

echo "== diff staging -> /opt/vault-mcp (hors runtime) =="
if rsync -ani --delete "${FILTRE[@]}" "$SRC/" /opt/vault-mcp/; then
  echo "DIFF-AFFICHE (vide = deja synchro)"
fi

if [ "$MODE" != "--apply" ]; then
  echo "DRY-RUN OK — relancer avec --apply pour deployer (backup + rsync + chown + daemon-reload)"
  rm -rf "$STAGING"
  exit 0
fi

TS=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP="/opt/vault-mcp-backup-$TS"
cp -a /opt/vault-mcp "$BACKUP"
echo "BACKUP $BACKUP"
rsync -a --delete "${FILTRE[@]}" "$SRC/" /opt/vault-mcp/
chown -R juliann-app:juliann-app /opt/vault-mcp
chmod 600 /opt/vault-mcp/mcp.env 2>/dev/null || true
systemctl daemon-reload
rm -rf "$STAGING"
echo "APPLY OK — restart manuel : systemctl restart vault-mcp (voir docs/runbook-deploy.md)"
echo "Rollback : cp -a $BACKUP/* /opt/vault-mcp/ + chown + daemon-reload + restart"
