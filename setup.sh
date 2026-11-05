#!/usr/bin/env bash
# Prepares a fresh checkout for its first build. Run once on the Docker host, from anywhere:
#   sudo ./setup.sh                    Honcho's project folder is ../honcho, beside this checkout
#   sudo ./setup.sh /path/to/honcho    Honcho's project folder is elsewhere
# It writes files only: deploy/.env from the example (HONCHO_ROOT filled in), config/settings.json and
# config/rates.json from the examples, gives the logs and cli-secrets folders to the containers' user
# (uid 1000), writes cli-secrets/tokens.env (BRIDGE_TOKEN and TELEMETRY_TOKEN, random unless Honcho's .env has
# them), and prints the lines to add to Honcho's compose file with this host's paths filled in.
# Files that already exist are kept.
# It builds nothing, starts nothing and never edits Honcho's files. docs/SETUP.md lists the same steps by hand.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
honcho="${1:-$here/../honcho}"
if [ ! -f "$honcho/.env" ] || [ ! -f "$honcho/config.toml" ]; then
  echo "No Honcho project in $honcho (it needs .env and config.toml)." >&2
  echo "Pass Honcho's project folder: ./setup.sh /path/to/honcho" >&2
  exit 1
fi
honcho="$(cd "$honcho" && pwd)"

if [ -f "$here/deploy/.env" ]; then
  echo "kept      deploy/.env"
else
  sed -e 's/\r$//' -e "s#^HONCHO_ROOT=.*#HONCHO_ROOT=$honcho#" "$here/deploy/.env.example" > "$here/deploy/.env"
  echo "wrote     deploy/.env (HONCHO_ROOT=$honcho)"
fi

if [ -f "$here/config/settings.json" ]; then
  echo "kept      config/settings.json"
else
  cp "$here/src/config/settings.example.json" "$here/config/settings.json"
  echo "wrote     config/settings.json (the example: stock mode, reranking off)"
fi

if [ -f "$here/config/rates.json" ]; then
  echo "kept      config/rates.json"
else
  cp "$here/src/config/rates.example.json" "$here/config/rates.json"
  echo "wrote     config/rates.json (public list prices for the Cost page)"
fi

# The value of a deploy/.env line, as an absolute host path (relative paths are read from deploy/).
folder() {
  local value
  value="$(sed -n "s/^$1=//p" "$here/deploy/.env" | tail -n 1 | tr -d '\r')"
  value="${value:-$2}"
  case "$value" in /*) ;; *) value="$here/deploy/$value" ;; esac
  mkdir -p "$value"
  (cd "$value" && pwd)
}
config_dir="$(folder CONFIG_DIR ../config)"
data_dir="$(folder DATA_DIR ../data)"
logs_dir="$(folder LOGS_DIR ../logs)"
secrets_dir="$(folder SECRETS_DIR ../cli-secrets)"
mkdir -p "$secrets_dir/codex" "$secrets_dir/claude" "$data_dir/instructions"

# The random tokens Honcho's api and deriver share with this stack, in a file of their own: Honcho's .env is never
# edited. A token Honcho's .env already has keeps its value, so both sides agree.
tokens="$secrets_dir/tokens.env"
if [ -f "$tokens" ]; then
  echo "kept      $tokens"
else
  token() {
    local value
    value="$(tr -d '\r' < "$honcho/.env" | sed -n "s/^$1=//p" | tail -n 1)"
    [ -n "$value" ] || value="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    printf '%s' "$value"
  }
  bridge_token="$(token BRIDGE_TOKEN)"
  telemetry_token="$(token TELEMETRY_TOKEN)"
  {
    echo "# Written by setup.sh: tokens shared by Honcho's api and deriver and this stack. Private: never commit it."
    echo "BRIDGE_TOKEN=$bridge_token"
    echo "TELEMETRY_TOKEN=$telemetry_token"
    echo "TELEMETRY_HEADERS={\"Authorization\": \"Bearer $telemetry_token\"}"
  } > "$tokens"
  chmod 600 "$tokens"
  echo "wrote     $tokens (BRIDGE_TOKEN, TELEMETRY_TOKEN)"
fi

# The bridge runs as uid 1000: it writes its call log and the Claude login.
if [ "$(id -u)" = "0" ]; then
  chown -R 1000:1000 "$logs_dir" "$secrets_dir"
  chmod -R go-rwx "$secrets_dir"
  echo "owner     $logs_dir and $secrets_dir -> uid 1000"
else
  echo "skipped   ownership (not root). Run: sudo chown -R 1000:1000 $logs_dir $secrets_dir"
fi

if ! tr -d '\r' < "$here/deploy/.env" | grep -q '^HONCHO_WORKSPACE=.'; then
  echo "to do     set HONCHO_WORKSPACE in deploy/.env for the scheduler and the dashboard's peer pages"
fi

echo
echo "Add these lines to Honcho's compose file ($honcho), to the services each block names:"
echo
sed -n '/^# --- /,$p' "$here/deploy/honcho-mounts.yaml" \
  | sed -e "s#<CODE_ROOT>#$here#g" -e "s#<CONFIG_DIR>#$config_dir#g" \
        -e "s#<LOGS_DIR>#$logs_dir#g" -e "s#<DATA_DIR>#$data_dir#g" -e "s#<SECRETS_DIR>#$secrets_dir#g"
echo
echo "Then build and start the stack:  docker compose -f $here/deploy/compose.yaml up -d --build"
echo "and recreate Honcho's api and deriver. The dashboard is on port 8010."
