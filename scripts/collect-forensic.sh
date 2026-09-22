#!/bin/bash
# collect-forensic.sh — capture AVANT restart (Phase 5, incident 22/09/2026).
# Aucun contenu du Vault, aucun secret. Sortie bornee + rotation.
# Usage : sudo /opt/vault-watchdog/collect-forensic.sh [raison]
set -euo pipefail
RAISON="${1:-manuel}"
TS=$(date -u +%Y%m%dT%H%M%SZ)
BASE="/var/log/vault-mcp/forensic-${TS}-${RAISON}"
mkdir -p "$BASE"
chmod 0700 "$BASE"
{
  echo "=== $TS raison=$RAISON ==="
  date -u
  uptime
  free -h
  swapon --show || true
} > "$BASE/base.txt" 2>&1
systemctl show vault-mcp -p Id,ActiveState,SubState,Result,NRestarts,MemoryCurrent,MemoryPeak,MemoryHigh,MemoryMax,MemorySwapCurrent,MemorySwapMax,OOMPolicy,Restart,ExecMainPID,ActiveEnterTimestamp > "$BASE/systemd.txt" 2>&1 || true
PID=$(systemctl show vault-mcp -p ExecMainPID --value 2>/dev/null || echo 0)
echo "$PID" > "$BASE/pid.txt"
if [ "$PID" != "0" ] && [ -d "/proc/$PID" ]; then
  cat "/proc/$PID/smaps_rollup" > "$BASE/smaps_rollup.txt" 2>&1 || true
  grep -E "Threads|VmRSS|VmSize|VmHWM|VmSwap" "/proc/$PID/status" > "$BASE/status.txt" 2>&1 || true
  ls -l "/proc/$PID/fd" > "$BASE/fd.txt" 2>&1 || true
  grep -E "vectors|deleted" "/proc/$PID/maps" > "$BASE/maps_vectors.txt" 2>&1 || true
  ss -tnp > "$BASE/ss.txt" 2>&1 || true
fi
ls -lh /opt/vault-mcp/index/ > "$BASE/index_ls.txt" 2>&1 || true
systemctl status vault-mcp vault-mcp-rs vault-index-worker vault-index-worker-reconciliation vault-reindex --no-pager -l > "$BASE/status_services.txt" 2>&1 || true
journalctl -u vault-mcp --since "-30 min" --no-pager > "$BASE/journal_vault.txt" 2>&1 || true
# Rotation : garde 10 derniers snapshots.
ls -dt /var/log/vault-mcp/forensic-* 2>/dev/null | tail -n +11 | xargs -r rm -rf
echo "forensic: $BASE"
