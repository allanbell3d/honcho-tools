#!/bin/bash
# Nightly pg_dump of the Honcho database. Dumps go under /backups (BACKUP_DIR), the log to
# /logs/backup.log (LOGS_DIR). BACKUP_LOG overrides the log path.
# Requires bash 4+ and GNU date (both are in pgvector/pgvector:pg15).

if [[ -z "${BASH_VERSION:-}" ]]; then
  echo "backup.sh requires bash" >&2
  exit 1
fi

set -euo pipefail

# Compose loads Honcho's whole .env into this container (env_file): JWT secret, provider keys
# and the rest. The script starts again with only what it and the PostgreSQL client read, so
# pg_dump never holds them. BACKUP_ENV_SCRUBBED marks the second start.
scrub_env() {
  local name
  local -a drop=()
  if [[ -n "${BACKUP_ENV_SCRUBBED:-}" ]]; then
    return 0
  fi
  for name in $(compgen -e); do
    case "$name" in
      PATH|HOME|HOSTNAME|LANG|LANGUAGE|TZ|TMPDIR|PWD|OLDPWD|SHLVL|_|LC_*) ;;
      PG*|POSTGRES_PASSWORD|BACKUP_*|KEEP_DAILY|KEEP_WEEKLY) ;;
      *) drop+=(-u "$name") ;;
    esac
  done
  exec env "${drop[@]+"${drop[@]}"}" BACKUP_ENV_SCRUBBED=1 bash "$0" "$@"
}
scrub_env "$@"

export TZ="${TZ:-UTC}"
export LC_ALL=C

# /backups in the container. BACKUP_ROOT moves it only for the offline tests.
BACKUP_ROOT="${BACKUP_ROOT:-/backups}"
LOG_MAX=$((5 * 1024 * 1024))
LOG="${BACKUP_LOG:-/logs/backup.log}"
# The database to dump: PGHOST, PGPORT, PGUSER and PGDATABASE from deploy/.env, else Honcho's
# service name and the honcho user and database. The password is PGPASSWORD or POSTGRES_PASSWORD.
DB_HOST="${PGHOST:-database}"
DB_PORT="${PGPORT:-5432}"
DB_USER="${PGUSER:-honcho}"
DB_NAME="${PGDATABASE:-honcho}"

usage() {
  echo "usage: backup.sh            (the nightly loop)" >&2
  echo "       backup.sh --now      (one dump and retention now; exit 0 when the dump was written)" >&2
  echo "       backup.sh --retention-plan TODAY KEEP_DAILY KEEP_WEEKLY [DATE...]" >&2
  echo "       backup.sh --seconds-until NOW_ISO HH:MM" >&2
  exit 2
}

valid_date() {
  local d=$1 got
  [[ "$d" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || return 1
  got=$(date -d "$d" +%Y-%m-%d 2>/dev/null) || return 1
  [[ "$got" == "$d" ]]
}

iso_week() {
  date -d "$1" +%G-W%V
}

normalize_count() {
  local value=$1 default=$2
  if [[ "$value" =~ ^[0-9]+$ ]] && (( value >= 1 )); then
    printf '%s\n' "$value"
  else
    printf '%s\n' "$default"
  fi
}

# Union of the newest `daily` dates and the newest date in each of the last
# `weekly` ISO weeks from `today`. A date that matches both rules is printed once.
select_keep_dates() {
  local today=$1 daily=$2 weekly=$3
  shift 3
  local -a dates=("$@")
  local -a sorted=()
  local -A keep=() newest=()
  local d w anchor n=0 i

  if ((${#dates[@]} == 0)); then
    return 0
  fi
  mapfile -t sorted < <(printf '%s\n' "${dates[@]}" | sort -ru)
  for d in "${sorted[@]}"; do
    if (( n < daily )); then
      keep["$d"]=1
      n=$((n + 1))
    fi
    w=$(iso_week "$d")
    if [[ -z "${newest[$w]:-}" ]]; then
      newest["$w"]=$d
    fi
  done
  for ((i = 0; i < weekly; i++)); do
    anchor=$(date -d "$today - $i weeks" +%Y-%m-%d)
    w=$(iso_week "$anchor")
    if [[ -n "${newest[$w]:-}" ]]; then
      keep["${newest[$w]}"]=1
    fi
  done
  for d in "${sorted[@]}"; do
    if [[ -n "${keep[$d]:-}" ]]; then
      printf '%s\n' "$d"
    fi
  done
}

retention_plan() {
  local today=$1 daily=$2 weekly=$3
  shift 3
  local -a valid=() allsorted=()
  local -A keep=()
  local d

  daily=$(normalize_count "$daily" 7)
  weekly=$(normalize_count "$weekly" 4)
  if ! valid_date "$today"; then
    echo "invalid today: $today" >&2
    exit 2
  fi
  for d in "$@"; do
    if valid_date "$d"; then
      valid+=("$d")
    else
      printf 'skip %s\n' "$d"
    fi
  done
  if ((${#valid[@]} > 0)); then
    while IFS= read -r d; do
      [[ -n "$d" ]] || continue
      keep["$d"]=1
    done < <(select_keep_dates "$today" "$daily" "$weekly" "${valid[@]}")
  fi
  if ((${#@} > 0)); then
    mapfile -t allsorted < <(printf '%s\n' "$@" | sort)
  fi
  for d in "${allsorted[@]+"${allsorted[@]}"}"; do
    if ! valid_date "$d"; then
      continue
    fi
    if [[ -n "${keep[$d]:-}" ]]; then
      printf 'keep %s\n' "$d"
    else
      printf 'delete %s\n' "$d"
    fi
  done
}

seconds_until_at() {
  local now_iso=$1 at=$2 now target day
  if [[ ! "$at" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]]; then
    echo "BACKUP_AT must be HH:MM" >&2
    return 2
  fi
  now=$(date -d "$now_iso" +%s)
  day=$(date -d "$now_iso" +%Y-%m-%d)
  target=$(date -d "$day $at" +%s)
  if (( now > target )); then
    day=$(date -d "$day 1 day" +%Y-%m-%d)
    target=$(date -d "$day $at" +%s)
  fi
  printf '%s\n' $((target - now))
}

under_backups() {
  case "$1" in
    "$BACKUP_ROOT"|"$BACKUP_ROOT"/*) return 0 ;;
    *) return 1 ;;
  esac
}

rotate_log() {
  local size
  if [[ -L "$LOG" ]]; then
    echo "backup.log is a symlink; not rotating" >&2
    return 0
  fi
  [[ -f "$LOG" ]] || return 0
  size=$(stat -c %s "$LOG" 2>/dev/null || echo 0)
  if (( size >= LOG_MAX )); then
    if [[ -L "${LOG}.1" ]]; then
      rm -f -- "${LOG}.1"
    fi
    mv -f -- "$LOG" "${LOG}.1"
  fi
}

log() {
  if [[ -L "$LOG" ]]; then
    printf '%s %s\n' "$(date -Iseconds)" "$*" >&2
    return 0
  fi
  rotate_log || true
  printf '%s %s\n' "$(date -Iseconds)" "$*" >>"$LOG"
  printf '%s %s\n' "$(date -Iseconds)" "$*" >&2
}

sleep_seconds() {
  local remain=$1 step pid
  while (( remain > 0 && stop == 0 )); do
    if (( remain > 30 )); then
      step=30
    else
      step=$remain
    fi
    sleep "$step" &
    pid=$!
    set +e
    wait "$pid"
    set -e
    if (( stop != 0 )); then
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
      return 0
    fi
    remain=$((remain - step))
  done
}

seconds_until_next() {
  seconds_until_at "$(date +%Y-%m-%dT%H:%M:%S)" "$BACKUP_AT"
}

clean_partials() {
  local p
  shopt -s nullglob
  for p in "$BACKUP_ROOT"/.honcho-*.dump.partial \
           "$BACKUP_ROOT"/.honcho-*.sha256.partial \
           "$BACKUP_ROOT"/.dump.err "$BACKUP_ROOT"/.dump.toc; do
    if ! under_backups "$p"; then
      log "refusing path $p"
      continue
    fi
    rm -f -- "$p"
  done
  shopt -u nullglob
}

# Failures a retry cannot fix: wrong credentials, user or database, or a client older than
# the server. Anything else (refused connection, DNS, a database starting or shutting down,
# a dropped connection) is treated as transient.
PERMANENT_ERRORS='password authentication failed|no password supplied|role "[^"]*" does not exist|database "[^"]*" does not exist|permission denied|server version mismatch|pg_hba.conf rejects'

# Set by dump_once on failure: transient or permanent.
FAILURE=transient

# The dump's table of contents must read back and list at least one table: pg_restore --list
# reads the archive header and TOC, so a truncated or foreign file fails here, before publish.
verify_dump() {
  local file=$1 toc="${BACKUP_ROOT}/.dump.toc"
  rm -f -- "$toc"
  if ! pg_restore --list "$file" >"$toc" 2>>"$LOG"; then
    rm -f -- "$toc"
    log "pg_restore --list could not read the dump"
    return 1
  fi
  if ! grep -q -E '^[0-9]+; [0-9]+ [0-9]+ TABLE ' "$toc"; then
    rm -f -- "$toc"
    log "the dump lists no table; ${DB_USER}@${DB_HOST}/${DB_NAME} may not be the Honcho database"
    FAILURE=permanent
    return 1
  fi
  rm -f -- "$toc"
  return 0
}

dump_once() {
  local day partial sha_partial final sha_final hash hash_line err pid status size
  FAILURE=transient
  day=$(date +%Y-%m-%d)
  partial="${BACKUP_ROOT}/.honcho-${day}.dump.partial"
  sha_partial="${BACKUP_ROOT}/.honcho-${day}.dump.sha256.partial"
  final="${BACKUP_ROOT}/honcho-${day}.dump"
  sha_final="${BACKUP_ROOT}/honcho-${day}.dump.sha256"
  err="${BACKUP_ROOT}/.dump.err"

  if [[ -L "$final" || -L "$sha_final" || -L "$partial" || -L "$sha_partial" ]]; then
    log "refusing symlink while writing honcho-${day}.dump"
    return 1
  fi
  rm -f -- "$partial" "$sha_partial" "$err"
  : >"$err"

  set +e
  pg_dump -Fc --no-password -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -f "$partial" "$DB_NAME" >"$err" 2>&1 &
  pid=$!
  wait "$pid"
  status=$?
  set -e
  if (( stop != 0 )); then
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    rm -f -- "$partial" "$err"
    log "dump interrupted"
    return 1
  fi
  if (( status != 0 )); then
    log "pg_dump exited ${status}"
    if [[ -s "$err" ]]; then
      cat "$err" >>"$LOG" || true
      cat "$err" >&2 || true
      if grep -q -i -E "$PERMANENT_ERRORS" "$err"; then
        FAILURE=permanent
      fi
    fi
    rm -f -- "$partial" "$err"
    return 1
  fi
  if [[ -s "$err" ]]; then
    cat "$err" >>"$LOG" || true
  fi
  rm -f -- "$err"
  if [[ ! -f "$partial" || -L "$partial" ]]; then
    log "pg_dump left no file"
    return 1
  fi
  size=$(stat -c %s "$partial")
  if (( size < 1 )); then
    log "pg_dump wrote an empty file"
    rm -f -- "$partial"
    return 1
  fi
  if ! verify_dump "$partial"; then
    rm -f -- "$partial"
    return 1
  fi
  if command -v sync >/dev/null 2>&1; then
    sync -f "$partial" || true
  fi
  hash_line=$(sha256sum -- "$partial")
  read -r hash _ <<<"$hash_line"
  if [[ ! "$hash" =~ ^[0-9a-f]{64}$ ]]; then
    log "checksum failed for honcho-${day}.dump"
    rm -f -- "$partial"
    return 1
  fi
  printf '%s  %s\n' "$hash" "honcho-${day}.dump" >"$sha_partial"
  mv -f -- "$partial" "$final"
  mv -f -- "$sha_partial" "$sha_final"
  if ! (cd "$BACKUP_ROOT" && sha256sum -c "honcho-${day}.dump.sha256") >>"$LOG" 2>&1; then
    log "published checksum did not match honcho-${day}.dump; removed it"
    rm -f -- "$final" "$sha_final"
    return 1
  fi
  log "wrote honcho-${day}.dump"
  return 0
}

# BACKUP_ATTEMPTS tries (default 8), waiting BACKUP_RETRY_SECONDS (default 30) after the
# first failure and doubling up to 15 minutes: about 45 minutes in all, long enough for a
# database restart or a host update at the backup mark. A permanent failure is not retried.
dump_with_retries() {
  local attempt delay=$RETRY_SECONDS
  for ((attempt = 1; attempt <= ATTEMPTS; attempt++)); do
    if (( stop != 0 )); then
      return 1
    fi
    if dump_once; then
      return 0
    fi
    if [[ "$FAILURE" == permanent ]]; then
      log "dump attempt ${attempt} failed; not retried (credentials, database name or content)"
      return 1
    fi
    if (( attempt < ATTEMPTS && stop == 0 )); then
      log "dump attempt ${attempt} of ${ATTEMPTS} failed; next in ${delay}s"
      sleep_seconds "$delay"
      delay=$((delay * 2))
      if (( delay > 900 )); then
        delay=900
      fi
    else
      log "dump attempt ${attempt} of ${ATTEMPTS} failed"
    fi
  done
  return 1
}

prune() {
  local today d base path
  local -a dates=()
  local -A keep=()
  today=$(date +%Y-%m-%d)
  shopt -s nullglob
  for path in "$BACKUP_ROOT"/honcho-*.dump; do
    base=$(basename -- "$path")
    if [[ -L "$path" ]]; then
      rm -f -- "$path"
      log "removed symlink ${base}"
      continue
    fi
    if [[ "$base" =~ ^honcho-([0-9]{4}-[0-9]{2}-[0-9]{2})\.dump$ ]]; then
      d="${BASH_REMATCH[1]}"
      if valid_date "$d"; then
        dates+=("$d")
      else
        rm -f -- "$path" "${BACKUP_ROOT}/honcho-${d}.dump.sha256"
        log "deleted invalid name ${base}"
      fi
    fi
  done
  if ((${#dates[@]} > 0)); then
    while IFS= read -r d; do
      [[ -n "$d" ]] || continue
      keep["$d"]=1
    done < <(select_keep_dates "$today" "$KEEP_DAILY" "$KEEP_WEEKLY" "${dates[@]}")
  fi
  for d in "${dates[@]+"${dates[@]}"}"; do
    if [[ -z "${keep[$d]:-}" ]]; then
      rm -f -- "${BACKUP_ROOT}/honcho-${d}.dump" "${BACKUP_ROOT}/honcho-${d}.dump.sha256"
      log "deleted honcho-${d}.dump"
    fi
  done
  for path in "$BACKUP_ROOT"/honcho-*.dump.sha256; do
    base=$(basename -- "$path")
    if [[ -L "$path" ]]; then
      rm -f -- "$path"
      log "removed symlink ${base}"
      continue
    fi
    if [[ "$base" =~ ^honcho-([0-9]{4}-[0-9]{2}-[0-9]{2})\.dump\.sha256$ ]]; then
      d="${BASH_REMATCH[1]}"
      if [[ -z "${keep[$d]:-}" || ! -f "${BACKUP_ROOT}/honcho-${d}.dump" ]]; then
        rm -f -- "$path"
        log "deleted ${base}"
      fi
    fi
  done
  shopt -u nullglob
  log "retention kept ${#keep[@]} dump(s); daily=${KEEP_DAILY} weekly=${KEEP_WEEKLY}"
}

load_counts() {
  local raw
  raw="${KEEP_DAILY:-${BACKUP_KEEP_DAILY:-7}}"
  KEEP_DAILY=$(normalize_count "$raw" 7)
  if [[ "$KEEP_DAILY" != "$raw" ]]; then
    log "KEEP_DAILY value '${raw}' is not a positive integer; using ${KEEP_DAILY}"
  fi
  raw="${KEEP_WEEKLY:-${BACKUP_KEEP_WEEKLY:-4}}"
  KEEP_WEEKLY=$(normalize_count "$raw" 4)
  if [[ "$KEEP_WEEKLY" != "$raw" ]]; then
    log "KEEP_WEEKLY value '${raw}' is not a positive integer; using ${KEEP_WEEKLY}"
  fi
  ATTEMPTS=$(normalize_count "${BACKUP_ATTEMPTS:-8}" 8)
  if (( ATTEMPTS > 20 )); then
    ATTEMPTS=20
  fi
  RETRY_SECONDS="${BACKUP_RETRY_SECONDS:-30}"
  if [[ ! "$RETRY_SECONDS" =~ ^[0-9]+$ ]] || (( RETRY_SECONDS > 900 )); then
    log "BACKUP_RETRY_SECONDS value '${RETRY_SECONDS}' is not 0-900; using 30"
    RETRY_SECONDS=30
  fi
  if [[ ! "$BACKUP_AT" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]]; then
    log "BACKUP_AT value '${BACKUP_AT}' is not HH:MM; using 03:30"
    BACKUP_AT=03:30
  fi
}

# Returns 0 when the dump was written, 1 when it failed, 2 when another run held the lock.
run_once() {
  local result=0
  if command -v flock >/dev/null 2>&1; then
    if ! flock -n 9; then
      log "another backup holds ${BACKUP_ROOT}/.backup.lock; skipping this run"
      return 2
    fi
  fi
  if [[ -z "${PGPASSWORD:-}" && -z "${POSTGRES_PASSWORD:-}" ]]; then
    log "neither PGPASSWORD nor POSTGRES_PASSWORD is set; trying ${DB_USER}@${DB_HOST} without a password"
  fi
  export PGPASSWORD="${PGPASSWORD:-${POSTGRES_PASSWORD:-}}"
  export PGCONNECT_TIMEOUT="${PGCONNECT_TIMEOUT:-10}"
  if dump_with_retries; then
    prune || log "retention failed"
  else
    result=1
    log "dump failed; applying retention to the dumps already on disk"
    prune || log "retention failed"
  fi
  if command -v flock >/dev/null 2>&1; then
    flock -u 9 || true
  fi
  return "$result"
}

prepare() {
  if [[ ! -d "$BACKUP_ROOT" || ! -w "$BACKUP_ROOT" ]]; then
    echo "backup: ${BACKUP_ROOT} is not a writable directory" >&2
    exit 1
  fi
  if [[ -L "${BACKUP_ROOT}/.backup.lock" || -L "$LOG" ]]; then
    echo "backup: refusing symlink at ${BACKUP_ROOT}/.backup.lock or ${LOG}" >&2
    exit 1
  fi
  if ! mkdir -p -- "${LOG%/*}" || [[ ! -w "${LOG%/*}" ]]; then
    echo "backup: ${LOG%/*} is not a writable directory" >&2
    exit 1
  fi
  umask 077
  exec 9>"${BACKUP_ROOT}/.backup.lock"
  BACKUP_AT="${BACKUP_AT:-03:30}"
  load_counts
}

main() {
  local offset wait
  if (( $# > 0 )); then
    usage
  fi
  prepare
  clean_partials
  offset=$(date +%z)
  log "starting tz=${TZ} offset=${offset} backup_at=${BACKUP_AT} keep_daily=${KEEP_DAILY} keep_weekly=${KEEP_WEEKLY} attempts=${ATTEMPTS}"
  # A container that was down at the mark catches up once instead of waiting a day.
  if [[ ! -e "${BACKUP_ROOT}/honcho-$(date +%Y-%m-%d).dump" && "$(date +%H:%M)" > "$BACKUP_AT" ]]; then
    log "no dump for today and ${BACKUP_AT} has passed; dumping now"
    run_once || true
  fi
  while (( stop == 0 )); do
    wait=$(seconds_until_next)
    if (( wait > 0 )); then
      log "sleeping ${wait}s until ${BACKUP_AT}"
      sleep_seconds "$wait"
      continue
    fi
    run_once || true
    if (( stop == 0 )) && (( $(seconds_until_next) == 0 )); then
      sleep_seconds 1
    fi
  done
  log "stopping"
}

stop=0
on_stop() { stop=1; }
trap on_stop TERM INT

if [[ "${1:-}" == "--retention-plan" ]]; then
  if (( $# < 4 )); then
    usage
  fi
  retention_plan "$2" "$3" "$4" "${@:5}"
  exit 0
fi

if [[ "${1:-}" == "--seconds-until" ]]; then
  if (( $# != 3 )); then
    usage
  fi
  seconds_until_at "$2" "$3"
  exit 0
fi

if [[ "${1:-}" == "--help" ]]; then
  usage
fi

if [[ "${1:-}" == "--now" ]]; then
  if (( $# != 1 )); then
    usage
  fi
  prepare
  # Partials are not cleaned here: the nightly loop may be writing one in this container.
  set +e
  run_once
  result=$?
  set -e
  exit "$result"
fi

main "$@"
