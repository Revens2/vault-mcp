#!/bin/bash
# Miroir fidele du vault Obsidian depuis Google Drive.
# Source de verite unique du RAG : "G:\Mon Drive\Obsidian Vault" -> gdrive: -> ici.
#
# Pourquoi "sync" et pas "copy" : copy ne supprime jamais. Une note effacee sur Drive
# resterait indexee et remonterait dans les resultats de recherche -- un RAG qui repond
# du faux avec assurance. --backup-dir garantit qu aucune suppression n est definitive.
set -uo pipefail

SRC="gdrive:Obsidian Vault"
DEST="/srv/vault-mirror"
TRASH_ROOT="/srv/vault-mirror-trash"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RETENTION_DAYS=14

command -v rclone >/dev/null || { echo "rclone absent"; exit 1; }
mkdir -p "$DEST" "$TRASH_ROOT"

# Verrou PARTAGE avec /usr/local/bin/vault_spool_push.sh (adr/0020). Les deux
# ecrivent dans $DEST. Sans le meme flock DES DEUX COTES, celui du pousseur ne
# protege de rien : un `sync` demarrant pendant qu une intention est en vol vers
# Drive supprimerait du miroir la note fraichement creee. L attente est alignee
# sur TimeoutStartSec=1h de l unite.
# TICKET DE PRIORITE. Pose AVANT de se mettre en attente du verrou.
#
# `flock` n est pas equitable : un candidat en attente n a aucune garantie de
# passer avant un nouvel arrivant. vault_spool_push.sh est declenche par un
# .path a chaque intention deposee et par un timer toutes les ~13 s ; quand la
# tache ChatGPT produit des analyses en rafale, ses passages courts se
# re-servent en boucle et cette synchro attend.
#
# Mesure du 2026-09-06 : run demarre a 19:00:56, verrou obtenu a 19:57:33 --
# 56 min 37 s d attente pour un `-w 3600`. Il est passe a 3 min 23 s de
# l expiration, apres quoi le miroir aurait echoue et alerte sur Telegram, et le
# RAG serait reste en retard jusqu au passage suivant.
#
# Le ticket ne prend aucun verrou et n autorise aucune ecriture concurrente : il
# dit seulement au pousseur « rends la main a la fin de l intention en cours ».
TICKET="${VAULT_LOCK_WANTED:-/run/lock/vault-mirror.wanted}"
: > "$TICKET" 2>/dev/null || true
# Retire meme en cas d interruption : un ticket oublie ferait ceder le pousseur
# apres chaque intention, indefiniment.
trap 'rm -f "$TICKET"' EXIT

exec 9>/run/lock/vault-mirror.lock
if ! flock -w 3600 9; then
  echo "verrou vault-mirror non obtenu"
  exit 1
fi
rm -f "$TICKET"

# Exclusions :
#   .obsidian/.trash/.temp/.git -- etat local d Obsidian, aucune valeur semantique
#   livesync_log_*.md           -- journaux de sync, jusqu a 1,3 Mo piece, zero valeur
EXCLUDES=(
  --exclude "/.obsidian/**"
  --exclude "/.trash/**"
  --exclude "/.temp/**"
  --exclude "/.git/**"
  --exclude "livesync_log_*.md"
  --exclude "**/.DS_Store"
)

# Le vault est fait de milliers de petits fichiers : c est la latence par requete
# Drive qui domine, pas la bande passante. Mais un pacer trop court (10ms, burst 200)
# declenche le rate-limit Drive et son backoff exponentiel : constate le 2026-08-14,
# la synchro est tombee a 6 Ko/s pendant plus de dix minutes. On reste donc sur le
# pacer par defaut, avec un plafond explicite de requetes par seconde.
# 29/09/2026 : --fast-list retire. Sur Drive il renvoie par intermittence des
# listings INCOMPLETS sans erreur (mesure : 0 fichier en 144 s, contre 18 735
# sans). rclone sync supprimait alors du miroir des centaines de notes encore
# presentes sur Drive (300 a 555 par passage, ConvIA surtout), le RAG les
# retirait de l index puis les reindexait au passage suivant.
# Listing par dossier : ~6 min, compatible avec le cycle de 30 min.
TUNING=(
  --max-delete 1500
  --transfers 8
  --checkers 16
  --tpslimit 8
  --retries 5
  --low-level-retries 20
  --stats-one-line
  --stats 1m
)

# === BEGIN ALERT-LOGIC ===
# Anti-spam Telegram par signature d'erreur (2026-09-21).
# Contexte : rclone echoue parfois en "corrupted on transfer" sur des .md ConvIA
# quand Drive rejoue des uploads pendant le sync ; la tentative suivante reussit
# presque toujours (constate : Attempt 1/5 failed -> Attempt 2/5 succeeded).
# Avant, chaque echec complet (les 5 tentatives) sortait en exit 1, donc
# OnFailure -> occurrence infra_alerts -> rappels Telegram, meme avec un miroir
# frais de 30 minutes : du spam pour un transitoire connu.
#
# Regle desormais :
#   - signature CONNUE (transitoire) + miroir recemment OK -> exit 0, silence ;
#   - signature INCONNUE (changement de pattern) -> exit 1, alerte ;
#   - signature connue mais miroir BLOQUE (plus de $MAX_AGE_S sans succes ou
#     $MAX_CONSEC echecs consecutifs) -> exit 1, alerte.
# L'etat vit dans $STATE_DIR (ecrit sous le meme flock que le sync).
# Ce bloc est pur (assignations + fonctions) : sourcable tel quel pour tests.
STATE_DIR="/var/lib/vault-mirror-sync"
MAX_CONSEC=4
MAX_AGE_S=14400

state_last_ok() {
    local v
    v=$(cat "$STATE_DIR/last_success" 2>/dev/null || printf '0')
    [[ "$v" =~ ^[0-9]+$ ]] || v=0
    printf '%s' "$v"
}

state_consec() {
    local v
    v=$(cat "$STATE_DIR/consec_fail" 2>/dev/null || printf '0')
    [[ "$v" =~ ^[0-9]+$ ]] || v=0
    printf '%s' "$v"
}

classify_rclone_log() {
    local log="$1" residuals
    residuals=$(grep -a 'ERROR :' "$log" 2>/dev/null | sed -E 's/^.*ERROR : //' | sort -u || true)
    if [ -z "$residuals" ]; then
        printf 'unknown'
        return
    fi
    if printf '%s\n' "$residuals" | grep -aqiE 'rate.?limit|too many requests'; then
        printf 'transient-rate-limit'
        return
    fi
    if printf '%s\n' "$residuals" | grep -aqvE 'corrupted on transfer|sizes differ|\.partial|not deleting (files|directories) as there were errors|Attempt [0-9]+/[0-9]+ failed'; then
        printf 'unknown'
        return
    fi
    printf 'transient-corrupted-transfer'
}
# === END ALERT-LOGIC ===
mkdir -p "$STATE_DIR" 2>/dev/null || true
RCLONE_LOG=$(mktemp /tmp/vault-mirror-rclone.XXXXXX)
trap 'rm -f "$TICKET" "$RCLONE_LOG"' EXIT
if rclone sync "$SRC" "$DEST" --backup-dir "$TRASH_ROOT/$STAMP" "${EXCLUDES[@]}" "${TUNING[@]}" >"$RCLONE_LOG" 2>&1; then
  # Un backup-dir vide est cree a chaque passage sans suppression : le retirer.
  rmdir "$TRASH_ROOT/$STAMP" 2>/dev/null
  find "$TRASH_ROOT" -maxdepth 1 -mindepth 1 -type d -mtime +$RETENTION_DAYS -exec rm -rf {} + 2>/dev/null
  echo "miroir a jour : $(find "$DEST" -type f -name '*.md' | wc -l) notes, $(du -sh "$DEST" | cut -f1)"
  NOW_OK=$(date +%s)
  printf '%s' "$NOW_OK" > "$STATE_DIR/last_success" 2>/dev/null || true
  printf '0' > "$STATE_DIR/consec_fail" 2>/dev/null || true
  printf 'ok' > "$STATE_DIR/last_sig" 2>/dev/null || true
  exit 0
fi
SIG=$(classify_rclone_log "$RCLONE_LOG")
NOW=$(date +%s)
LAST_OK=$(state_last_ok)
CONSEC=$(state_consec)
[ "$LAST_OK" -gt "$NOW" ] && LAST_OK=$NOW
AGE=$(( NOW - LAST_OK ))
CONSEC=$(( CONSEC + 1 ))
printf '%s' "$CONSEC" > "$STATE_DIR/consec_fail" 2>/dev/null || true
printf '%s' "$SIG" > "$STATE_DIR/last_sig" 2>/dev/null || true
# Queue du log pour diagnostic (journal), sans spammer Telegram.
tail -c 2000 "$RCLONE_LOG" 2>/dev/null || true
if [ "$SIG" = "unknown" ]; then
  echo "ALERTE (nouvelle signature d'echec : $SIG) : ECHEC rclone sync"
  exit 1
fi
if [ "$CONSEC" -ge "$MAX_CONSEC" ] || [ "$AGE" -ge "$MAX_AGE_S" ]; then
  echo "ALERTE (miroir bloque : ${CONSEC} echecs consecutifs, dernier succes il y a ${AGE}s, signature $SIG) : ECHEC rclone sync"
  exit 1
fi
echo "transitoire connu ($SIG) : echec ${CONSEC}/${MAX_CONSEC}, dernier succes il y a ${AGE}s : pas d'alerte Telegram"
exit 0
