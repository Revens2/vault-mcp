#!/usr/bin/env bash
# vault-mcp-healthcheck.sh — sonde applicative vault-mcp (127.0.0.1:8787)
# Usage: vault-mcp-healthcheck.sh [--dry-run]  (ou DRY_RUN=1)
# Codes retour: 0=OK  1=WARN  2=DOWN/CRIT
# Restart vault-mcp seulement apres 3 echecs HTTP consecutifs (anti-flap),
# sauf en dry-run (aucun restart, mention "would-restart").
set -u
PORT=8787
URL="http://127.0.0.1:${PORT}/mcp"
SVC="vault-mcp"
STATE_DIR="/var/lib/vault-mcp-healthcheck"
FAIL_FILE="${STATE_DIR}/fails"
MEMMAX_BYTES=8589934592   # 8 Go (MemoryMax vault-mcp, ne pas changer ce soir)
DRY=0
if [ "${1:-}" = "--dry-run" ] || [ "${DRY_RUN:-0}" = "1" ]; then DRY=1; fi

mkdir -p "$STATE_DIR" 2>/dev/null || true
fails=0
[ -r "$FAIL_FILE" ] && fails=$(cat "$FAIL_FILE" 2>/dev/null | tr -cd '0-9') || fails=0
[ -z "$fails" ] && fails=0

# --- 1. Sonde applicative : POST initialize, timeout 5s ---
BODY='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"hc","version":"0"}}}'
CURL_OUT=$(curl -s -o /dev/null -w '%{http_code} %{time_total}' -m 5 -X POST "$URL" \
  -H 'Content-Type: application/json' -d "$BODY" 2>/dev/null || echo "000 5.000")
HTTP_CODE=$(echo "$CURL_OUT" | awk '{print $1}')
HTTP_TIME=$(echo "$CURL_OUT" | awk '{print $2}')
HTTP_ALIVE=0
[ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "401" ] && HTTP_ALIVE=1

# --- 2. Recv-Q sur :8787 (ss -tln) ---
RECVQ="?"
RECVQ_LINE=$(ss -tlnp 2>/dev/null | grep ":${PORT} " | head -1 || true)
if [ -n "$RECVQ_LINE" ]; then RECVQ=$(echo "$RECVQ_LINE" | awk '{print $2}'); fi

# --- 3. RSS vs MemoryMax ---
RSS_KB="?"
RSS_BYTES=0
if systemctl show "$SVC" -p MemoryCurrent --value 2>/dev/null | grep -q '[0-9]'; then
  MEMCUR=$(systemctl show "$SVC" -p MemoryCurrent --value 2>/dev/null)
  if [ "$MEMCUR" != "[not set]" ] && [ "$MEMCUR" -gt 0 ] 2>/dev/null; then
    RSS_BYTES=$MEMCUR
    RSS_KB=$((MEMCUR/1024))
  fi
fi
RSS_MB="?"
[ "$RSS_KB" != "?" ] && RSS_MB=$((RSS_KB/1024))

# --- 4. Swap ---
SWAP_USED="?"; SWAP_TOTAL="?"; SWAP_PCT="?"
if command -v free >/dev/null 2>&1; then
  read -r SWAP_TOTAL SWAP_USED <<< "$(free -m 2>/dev/null | awk '/^Swap:/{print $2, $3}')"
  if [ -n "$SWAP_TOTAL" ] && [ "$SWAP_TOTAL" -gt 0 ] 2>/dev/null; then
    SWAP_PCT=$((SWAP_USED*100/SWAP_TOTAL))
  fi
fi

# --- 5. CLOSE-WAIT sur :8787 ---
CLOSEWAIT=$(ss -tn state close-wait 2>/dev/null | grep -c ":${PORT}" || true)
[ -z "$CLOSEWAIT" ] && CLOSEWAIT=0

# --- Seuils ---
STATUS="OK"; RC=0; REASONS=""
warn() { STATUS="WARN"; RC=1; REASONS="${REASONS:+$REASONS, }$1"; }
crit() { STATUS="CRIT"; RC=2; REASONS="${REASONS:+$REASONS, }$1"; }

if [ "$HTTP_ALIVE" -ne 1 ]; then
  crit "HTTP_DOWN(${HTTP_CODE}/${HTTP_TIME}s)"
fi
case "$RECVQ" in ''|'?') : ;; *)
  if [ "$RECVQ" -ge 50 ] 2>/dev/null; then crit "RecvQ=${RECVQ}"; elif [ "$RECVQ" -ge 10 ] 2>/dev/null; then warn "RecvQ=${RECVQ}"; fi ;;
esac
if [ "$RSS_BYTES" -gt 0 ] 2>/dev/null; then
  if [ "$RSS_BYTES" -gt 6442450944 ] 2>/dev/null; then crit "RSS=${RSS_MB}Mo>6Go"; elif [ "$RSS_BYTES" -gt 4294967296 ] 2>/dev/null; then warn "RSS=${RSS_MB}Mo>4Go"; fi
fi
case "$SWAP_PCT" in ''|'?') : ;; *)
  if [ "$SWAP_PCT" -ge 90 ] 2>/dev/null; then crit "swap=${SWAP_PCT}%"; elif [ "$SWAP_PCT" -ge 75 ] 2>/dev/null; then warn "swap=${SWAP_PCT}%"; fi ;;
esac
if [ "$CLOSEWAIT" -gt 50 ] 2>/dev/null; then crit "CLOSEWAIT=${CLOSEWAIT}"; elif [ "$CLOSEWAIT" -gt 20 ] 2>/dev/null; then warn "CLOSEWAIT=${CLOSEWAIT}"; fi

# --- Sonde profonde (vraie recherche) toutes les 5 min, ajout 29/09/2026 ---
# Le 401/200 d'initialize ne prouve pas que les outils repondent : un processus
# fige en reclaim le passe encore. 2 echecs profonds consecutifs => restart.
DEEP="skip"; DEEP_FILE="${STATE_DIR}/deep_fails"
deep_fails=$(cat "$DEEP_FILE" 2>/dev/null | tr -cd '0-9'); [ -z "$deep_fails" ] && deep_fails=0
if [ "$HTTP_ALIVE" -eq 1 ] && { [ $(( $(date +%s) / 60 % 5 )) -eq 0 ] || [ "$deep_fails" -gt 0 ]; }; then
  DEEP=$(timeout 60 /usr/local/bin/vault-mcp-deep-probe.py 45 2>&1 | tail -1)
  case "$DEEP" in
    DEEP_OK*) deep_fails=0 ;;
    *) deep_fails=$((deep_fails+1)); crit "${DEEP:-DEEP_FAIL timeout}" ;;
  esac
  echo "$deep_fails" > "$DEEP_FILE" 2>/dev/null || true
fi

# --- Compteur anti-flap (echecs HTTP consecutifs) ---
HTTP_FAIL=0; [ "$HTTP_ALIVE" -ne 1 ] && HTTP_FAIL=1
if [ "$HTTP_FAIL" -eq 1 ]; then
  fails=$((fails+1)); echo "$fails" > "$FAIL_FILE" 2>/dev/null || true
else
  [ "$fails" != "0" ] && echo "0" > "$FAIL_FILE" 2>/dev/null || true
  fails=0
fi

ACTION="none"
if { [ "$HTTP_FAIL" -eq 1 ] && [ "$fails" -ge 3 ]; } || [ "$deep_fails" -ge 2 ]; then
  if [ "$DRY" -eq 1 ]; then
    ACTION="would-restart(3xFAIL,dry-run)"
  else
    logger -t vault-mcp-healthcheck "RESTART ${SVC} (HTTP fails=${fails} HTTP=${HTTP_CODE}, deep_fails=${deep_fails})" 2>/dev/null || true
    if systemctl restart "$SVC" 2>/dev/null; then
      ACTION="restarted"
      logger -t vault-mcp-healthcheck "RESTART ${SVC} OK" 2>/dev/null || true
    else
      ACTION="restart-FAILED"
      logger -t vault-mcp-healthcheck "RESTART ${SVC} ECHEC" 2>/dev/null || true
    fi
    echo "0" > "$FAIL_FILE" 2>/dev/null || true
    echo "0" > "$DEEP_FILE" 2>/dev/null || true
  fi
fi

TS=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
echo "${TS} vault-mcp ${STATUS} - HTTP=${HTTP_CODE}/${HTTP_TIME}s RecvQ=${RECVQ} RSS=${RSS_MB}Mo/7168Mo Swap=${SWAP_USED}/${SWAP_TOTAL}Mo(${SWAP_PCT}%) CLOSEWAIT=${CLOSEWAIT} fails=${fails} deep=${DEEP%% *}/${deep_fails} action=${ACTION}${REASONS:+" [$REASONS]"}"
exit $RC
