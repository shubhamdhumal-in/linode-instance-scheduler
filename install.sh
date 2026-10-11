#!/usr/bin/env bash
# Linode Instance Scheduler -- install on a fresh host, or recover onto a replacement host.
#
#   sudo ./install.sh install   [options]   first-time install
#   sudo ./install.sh recover   [options]   new host after the old one was lost: full recovery
#   sudo ./install.sh backup-now            take a backup right now (database, groups, host keys)
#   sudo ./install.sh start                 start the services (after `recover --no-start`)
#   sudo ./install.sh status                services, last backup, managed instances
#
# Run it from the directory you cloned the repository into. See DEPLOYMENT.md, "Installing with
# install.sh" and "Moving to a new host", for what each step does and why.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/linode-instance-scheduler"
SERVICE_USER="linode-scheduler"
ENV_FILE_IN=""
SSH_KEY_IN=""
SSH_KEY_FROM_BACKUP=0
NEW_SSH_KEY=0
NEW_PASSPHRASE=""
KEY_FROM_SNAPSHOT=0
PASSPHRASE_FILE=""
WITH_API=1
API_PORT=8000
DOMAIN=""
CADDYFILE="/etc/caddy/Caddyfile"
CADDY_BEGIN="# BEGIN linode-instance-scheduler (managed by install.sh)"
CADDY_END="# END linode-instance-scheduler"
WITH_DASHBOARD=1
SNAPSHOT=""
FROM_FILE=""
KNOWN_HOSTS_IN=""
REBUILD_ONLY=0
BACKUP_SCHEDULE="hourly"
BACKUP_KEEP_DAYS=14
ASSUME_YES=0
NO_START=0
# The dashboard build needs Node.js 20.19+ or 22.12+. When the system's Node is older or missing,
# this official release is downloaded from nodejs.org, checked against its published SHA-256,
# used for the build only, and removed afterwards.
NODE_BUILD_VERSION="22.12.0"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARNING:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; [[ -n "${ENV_STAGE:-}" ]] && rm -f "$ENV_STAGE"; exit 1; }

usage() {
  cat <<'EOF'
Usage: sudo ./install.sh <install|recover|start|backup-now|status> [options]

install       Install on this host: system packages, a service user, a Python environment,
              credentials (.env), the deployment SSH key, the poller and API services, the web
              dashboard, and a backup timer.
recover       Same, on a replacement host, then restore everything from your backups before
              starting anything: the latest database snapshot in Object Storage (or a local
              snapshot file), the trusted host keys, then `rebuild` from Linode tags and Object
              Storage to pick up anything newer than the snapshot. Without any snapshot,
              `rebuild` alone recovers instances, schedules, groups, start order and hooks.
start         Start the services (after `recover --no-start`).
backup-now    Run the backup now (database snapshot, every instance and group record, host keys).
status        Show service state, the latest backups and the managed instances.

Options:
  --dir DIR                 Install location (default /opt/linode-instance-scheduler)
  --user NAME               Service user (default linode-scheduler)
  --env-file PATH           Use this .env instead of prompting for credentials
  --ssh-key PATH            The deployment SSH private key to use. install: optional (one is
                            generated if not given). recover: the ORIGINAL key from the old host
  --ssh-key-from-backup     recover: fetch the stored key from Object Storage (also the default
                            when no --ssh-key is given: Object Storage, then the restored database)
  --new-ssh-key             recover: generate a new key instead. Only if the old one is truly
                            gone -- managed instances won't trust it until you add it to each one
  --backup-ssh-key          (kept for older scripts -- the key is now always stored encrypted, in the
                            database, Object Storage and your Linode profile's SSH keys)
  --passphrase-file PATH    Read that passphrase from a file instead of prompting
  --no-api                  Don't install the REST API / dashboard service
  --api-port PORT           API port on 127.0.0.1 (default 8000)
  --domain NAME             Serve the dashboard at https://NAME: installs Caddy, which gets and
                            renews the certificate. NAME's DNS must point at this host and ports
                            80/443 must be open. Asked interactively if not given
  --no-dashboard            Don't build the web dashboard
  --snapshot KEY            recover: a specific Object Storage snapshot (default: the latest)
  --from-file PATH          recover: a local snapshot file instead of Object Storage
  --known-hosts PATH        recover: trusted host keys file (default: from Object Storage)
  --rebuild-only            recover: skip the snapshot; rebuild from tags and Object Storage
  --no-start                recover: restore everything but don't start the services, so you can
                            review `instance_manager.py list` first; then run `install.sh start`
  --backup-schedule WHEN    hourly (default) or daily
  --backup-keep-days N      Local snapshots to keep, in days (default 14)
  --yes                     Don't ask for confirmation
EOF
}

[[ $# -ge 1 ]] || { usage; exit 1; }
MODE="$1"; shift
case "$MODE" in install|recover|start|backup-now|status) ;; -h|--help|help) usage; exit 0 ;; *) usage; exit 1 ;; esac

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) INSTALL_DIR="$2"; shift 2 ;;
    --user) SERVICE_USER="$2"; shift 2 ;;
    --env-file) ENV_FILE_IN="$2"; shift 2 ;;
    --ssh-key) SSH_KEY_IN="$2"; shift 2 ;;
    --ssh-key-from-backup) SSH_KEY_FROM_BACKUP=1; shift ;;
    --new-ssh-key) NEW_SSH_KEY=1; shift ;;
    --backup-ssh-key) shift ;;  # the key is always stored now; kept for older scripts
    --passphrase-file) PASSPHRASE_FILE="$2"; shift 2 ;;
    --no-api) WITH_API=0; shift ;;
    --api-port) API_PORT="$2"; shift 2 ;;
    --domain) DOMAIN="$2"; shift 2 ;;
    --no-dashboard) WITH_DASHBOARD=0; shift ;;
    --snapshot) SNAPSHOT="$2"; shift 2 ;;
    --from-file) FROM_FILE="$2"; shift 2 ;;
    --known-hosts) KNOWN_HOSTS_IN="$2"; shift 2 ;;
    --rebuild-only) REBUILD_ONLY=1; shift ;;
    --backup-schedule) BACKUP_SCHEDULE="$2"; shift 2 ;;
    --backup-keep-days) BACKUP_KEEP_DAYS="$2"; shift 2 ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    --no-start) NO_START=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

[[ $EUID -eq 0 ]] || die "run as root (sudo ./install.sh $MODE ...)"
INSTALL_DIR="$(realpath -m "$INSTALL_DIR")"
PY="$INSTALL_DIR/.venv/bin/python"
KEY_PATH="$INSTALL_DIR/keys/deploy_key"
BACKUP_DIR="$INSTALL_DIR/backups"
UNIT_DIR="/etc/systemd/system"
POLL_UNIT="linode-scheduler-poll"
API_UNIT="linode-scheduler-api"
BACKUP_UNIT="linode-scheduler-backup"

confirm() {
  [[ $ASSUME_YES -eq 1 ]] && return 0
  local answer
  read -r -p "$1 [y/N] " answer </dev/tty
  [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]]
}

# Every product command runs as the service user, from the install directory, so it reads
# $INSTALL_DIR/.env and writes $INSTALL_DIR/state like the services do.
im() { (cd "$INSTALL_DIR" && runuser -u "$SERVICE_USER" -- "$PY" instance_manager.py "$@"); }

env_has() { grep -Eq "^$1=.+" "$INSTALL_DIR/.env" 2>/dev/null; }

# --- system packages -----------------------------------------------------------------------------

install_packages() {
  log "Installing system packages"
  local pkgs
  if command -v apt-get >/dev/null; then
    pkgs=(python3 python3-venv python3-pip openssh-client rsync ca-certificates curl xz-utils)
    DEBIAN_FRONTEND=noninteractive apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${pkgs[@]}" >/dev/null
  elif command -v dnf >/dev/null || command -v yum >/dev/null; then
    local pm; pm=$(command -v dnf || command -v yum)
    pkgs=(python3 python3-pip openssh-clients rsync ca-certificates curl xz)
    "$pm" install -y -q "${pkgs[@]}" >/dev/null
  elif command -v zypper >/dev/null; then
    pkgs=(python3 python3-pip openssh-clients rsync ca-certificates curl xz)
    zypper -n -q install "${pkgs[@]}" >/dev/null
  else
    warn "no supported package manager found -- install Python 3.10+, openssh-client and rsync yourself"
  fi
  command -v systemctl >/dev/null || die "systemd is required"
  python3 - <<'EOF' || die "Python 3.10 or newer is required (found $(python3 --version 2>&1))"
import sys
sys.exit(0 if sys.version_info >= (3, 10) else 1)
EOF
}

# --- code, user, Python environment -----------------------------------------------------------------

install_code() {
  [[ -f "$SRC_DIR/instance_manager.py" ]] || die "run this from the cloned repository (instance_manager.py not found next to install.sh)"
  if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    log "Creating service user $SERVICE_USER"
    useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
  fi
  mkdir -p "$INSTALL_DIR"
  if [[ "$SRC_DIR" != "$INSTALL_DIR" ]]; then
    log "Copying the code to $INSTALL_DIR"
    rsync -a --delete \
      --exclude .git --exclude .venv --exclude .env --exclude state --exclude keys \
      --exclude backups --exclude web/node_modules --exclude web/dist \
      "$SRC_DIR/" "$INSTALL_DIR/"
  fi
  mkdir -p "$INSTALL_DIR/state" "$INSTALL_DIR/keys" "$BACKUP_DIR"
  log "Setting up the Python environment"
  [[ -x "$PY" ]] || python3 -m venv "$INSTALL_DIR/.venv"
  "$INSTALL_DIR/.venv/bin/pip" install -q --upgrade pip
  "$INSTALL_DIR/.venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"
  chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
  chmod 750 "$INSTALL_DIR"
  chmod 700 "$INSTALL_DIR/keys" "$BACKUP_DIR" "$INSTALL_DIR/state"
}

# --- credentials ----------------------------------------------------------------------------------

# .env is written through a private staging copy and only replaced, in one rename, after every
# question is answered -- an interrupted run (Ctrl+C, a lost SSH session) leaves the old file as
# it was and no half-written configuration behind.
ENV_STAGE=""
cleanup_env_stage() { [[ -n "$ENV_STAGE" ]] && rm -f "$ENV_STAGE"; ENV_STAGE=""; }

stage_set() {  # stage_set KEY VALUE -- replace or append in the staging file, never echo the value
  local tmp
  tmp=$(mktemp "$INSTALL_DIR/.env.tmp.XXXXXX")
  grep -v "^$1=" "$ENV_STAGE" > "$tmp" 2>/dev/null || true
  printf '%s=%s\n' "$1" "$2" >> "$tmp"
  cat "$tmp" > "$ENV_STAGE"; rm -f "$tmp"
}
stage_has() { grep -Eq "^$1=.+" "$ENV_STAGE" 2>/dev/null; }

ask_secret() {  # ask_secret "Prompt" [required] -> stdout
  local v
  while :; do
    read -r -s -p "$1: " v </dev/tty; printf '\n' >/dev/tty
    [[ -n "$v" || "${2:-}" != required ]] && break
    printf '  (required)\n' >/dev/tty
  done
  printf '%s' "$v"
}
ask() {  # ask "Prompt" "default" [required] -> stdout
  local v
  while :; do
    read -r -p "$1${2:+ [$2]}: " v </dev/tty
    v="${v:-$2}"
    [[ -n "$v" || "${3:-}" != required ]] && break
    printf '  (required)\n' >/dev/tty
  done
  printf '%s' "$v"
}

ask_object_storage() {
  [[ "$MODE" == "recover" ]] && log "Object Storage: use the SAME bucket the old host backed up to"
  stage_set LINODE_OBJ_STORAGE_BUCKET "$(ask 'Bucket name' '' required)"
  stage_set LINODE_OBJ_STORAGE_ENDPOINT "$(ask 'Endpoint URL (e.g. https://in-maa-1.linodeobjects.com)' '' required)"
  stage_set LINODE_OBJ_STORAGE_ACCESS_KEY "$(ask_secret 'Access key' required)"
  stage_set LINODE_OBJ_STORAGE_SECRET_KEY "$(ask_secret 'Secret key' required)"
}

ask_oauth() {
  local uri
  stage_set LINODE_OAUTH_CLIENT_ID "$(ask 'OAuth client ID' '' required)"
  stage_set LINODE_OAUTH_CLIENT_SECRET "$(ask_secret 'OAuth client secret' required)"
  while :; do
    uri=$(ask 'OAuth callback URL (exactly as entered in the OAuth app)' "https://${DOMAIN:-$(hostname -f)}/oauth/callback" required)
    [[ "$uri" =~ ^https:// || "$uri" =~ ^http://localhost(:[0-9]+)?/ ]] && break
    printf '  Linode only accepts https:// callback URLs (http:// only for localhost).\n' >/dev/tty
  done
  stage_set LINODE_OAUTH_REDIRECT_URI "$uri"
}

stage_obj_complete() {
  stage_has LINODE_OBJ_STORAGE_BUCKET && stage_has LINODE_OBJ_STORAGE_ENDPOINT \
    && stage_has LINODE_OBJ_STORAGE_ACCESS_KEY && stage_has LINODE_OBJ_STORAGE_SECRET_KEY
}
stage_obj_partial() { grep -q "^LINODE_OBJ_STORAGE_" "$ENV_STAGE" 2>/dev/null && ! stage_obj_complete; }
stage_oauth_complete() {
  stage_has LINODE_OAUTH_CLIENT_ID && stage_has LINODE_OAUTH_CLIENT_SECRET && stage_has LINODE_OAUTH_REDIRECT_URI
}

configure_env() {
  local file="$INSTALL_DIR/.env"
  umask 077
  ENV_STAGE=$(mktemp "$INSTALL_DIR/.env.new.XXXXXX")
  trap 'cleanup_env_stage; exit 130' INT TERM
  if [[ -n "$ENV_FILE_IN" ]]; then
    [[ -f "$ENV_FILE_IN" ]] || die "no file at $ENV_FILE_IN"
    log "Using credentials from $ENV_FILE_IN"
    cat "$ENV_FILE_IN" > "$ENV_STAGE"
  elif [[ -f "$file" ]]; then
    cat "$file" > "$ENV_STAGE"
  fi

  if stage_has LINODE_API_TOKEN; then
    log "Keeping the existing credentials in $file"
  else
    [[ $ASSUME_YES -eq 0 ]] || { cleanup_env_stage; die "no LINODE_API_TOKEN -- pass --env-file with it (and Object Storage settings) for a non-interactive run"; }
    log "Credentials (saved to $file, readable only by $SERVICE_USER, once every question is answered)"
    stage_set LINODE_API_TOKEN "$(ask_secret 'Linode API token (Linodes, Volumes, IPs read/write; VPCs read)' required)"
  fi

  # Optional sections: asked when missing or half-filled (a re-run fills in what an earlier one
  # skipped). Non-interactive runs never prompt.
  if [[ $ASSUME_YES -eq 0 ]] && ! stage_obj_complete; then
    if stage_obj_partial; then
      warn "Object Storage settings are incomplete in $file"
    fi
    if [[ "$MODE" == "recover" ]] || confirm "Configure Object Storage backups (strongly recommended)?"; then
      ask_object_storage
    fi
  fi
  if [[ $ASSUME_YES -eq 0 && $WITH_API -eq 1 && -z "$DOMAIN" ]]; then
    DOMAIN=$(ask "Domain name for the dashboard, already pointing at this host (blank to skip HTTPS)" "$(caddy_managed_domain)")
  fi
  if [[ $ASSUME_YES -eq 0 && $WITH_API -eq 1 ]] && ! stage_oauth_complete; then
    if confirm "Set up \"Login with Linode\" for the dashboard now (needs an OAuth app)?"; then
      ask_oauth
    fi
  fi
  if [[ -n "$DOMAIN" ]] && stage_has LINODE_OAUTH_REDIRECT_URI \
      && ! grep -q "^LINODE_OAUTH_REDIRECT_URI=https://$DOMAIN/oauth/callback$" "$ENV_STAGE"; then
    warn "LINODE_OAUTH_REDIRECT_URI doesn't match https://$DOMAIN/oauth/callback -- login only works when the OAuth app's callback URL, .env and the domain all agree."
  fi
  stage_set LINODE_SSH_KEY_PATH "$KEY_PATH"
  # The passphrase that encrypts the stored copies of the deployment SSH key. Generated once on a
  # first install (shown once at the end); on recovery it must be the original one.
  if ! stage_has LIS_SSH_KEY_PASSPHRASE; then
    if [[ "$MODE" == "install" ]]; then
      NEW_PASSPHRASE=$(head -c 48 /dev/urandom | base64 | tr -d '/+=\n' | cut -c1-32)
      stage_set LIS_SSH_KEY_PASSPHRASE "$NEW_PASSPHRASE"
    elif [[ $ASSUME_YES -eq 0 ]]; then
      local pp
      pp=$(ask_secret "SSH key passphrase from the original install (blank: fetch it from your Linode account)")
      [[ -n "$pp" ]] && stage_set LIS_SSH_KEY_PASSPHRASE "$pp"
    fi
  fi

  stage_has LINODE_API_TOKEN || { cleanup_env_stage; die "LINODE_API_TOKEN is empty"; }
  if stage_obj_partial; then
    warn "Object Storage settings are incomplete -- backups go to the local directory only until all four LINODE_OBJ_STORAGE_* lines are set (or re-run install)."
  fi
  chown "$SERVICE_USER:$SERVICE_USER" "$ENV_STAGE"; chmod 600 "$ENV_STAGE"
  mv -f "$ENV_STAGE" "$file"
  ENV_STAGE=""
  trap - INT TERM
}

obj_configured() {
  env_has LINODE_OBJ_STORAGE_BUCKET && env_has LINODE_OBJ_STORAGE_ENDPOINT \
    && env_has LINODE_OBJ_STORAGE_ACCESS_KEY && env_has LINODE_OBJ_STORAGE_SECRET_KEY
}

check_token() {
  log "Checking the API token"
  (cd "$INSTALL_DIR" && runuser -u "$SERVICE_USER" -- "$PY" - <<'EOF') || die "the API token didn't work (see above). Fix the LINODE_API_TOKEN line in $INSTALL_DIR/.env, or delete that file to be asked again, then re-run."
from dotenv import load_dotenv
load_dotenv(".env")
import linode_engine as engine
engine.auth_check(engine.build_client(engine.load_token()))
print("    token OK")
EOF
}

# --- the deployment SSH key -----------------------------------------------------------------------

run_with_passphrase() {  # run an im command that may read a passphrase
  local args=("$@")
  if [[ -n "$PASSPHRASE_FILE" ]]; then
    local copy; copy=$(mktemp); cp "$PASSPHRASE_FILE" "$copy"; chown "$SERVICE_USER" "$copy"; chmod 600 "$copy"
    im "${args[@]}" --passphrase-file "$copy" || { rm -f "$copy"; return 1; }
    rm -f "$copy"
  elif { true </dev/tty; } 2>/dev/null; then
    im "${args[@]}" </dev/tty
  else
    im "${args[@]}" </dev/null   # no terminal: the passphrase must come from .env or the escrow
  fi
}

install_key_file() {  # install_key_file SOURCE
  [[ -f "$1" ]] || die "no SSH private key at $1"
  install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 600 "$1" "$KEY_PATH"
  if [[ -f "$1.pub" ]]; then
    install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 644 "$1.pub" "$KEY_PATH.pub"
  else
    ssh-keygen -y -f "$KEY_PATH" > "$KEY_PATH.pub" 2>/dev/null || warn "couldn't derive the public key (passphrase-protected key?)"
    chown "$SERVICE_USER:$SERVICE_USER" "$KEY_PATH.pub" 2>/dev/null || true
  fi
}

setup_ssh_key() {
  if [[ -n "$SSH_KEY_IN" ]]; then
    log "Installing the deployment SSH key from $SSH_KEY_IN"
    install_key_file "$SSH_KEY_IN"
  elif [[ $SSH_KEY_FROM_BACKUP -eq 1 ]]; then
    obj_configured || die "--ssh-key-from-backup needs Object Storage settings in .env"
    log "Fetching the deployment SSH key from Object Storage"
    run_with_passphrase ssh-key-restore --ssh-key "$KEY_PATH" --force || die "couldn't restore the SSH key"
  elif [[ -f "$KEY_PATH" ]]; then
    log "Keeping the existing deployment SSH key"
  elif [[ "$MODE" == "recover" && $NEW_SSH_KEY -eq 0 ]]; then
    # The ORIGINAL key is needed: managed instances only trust it. Try the encrypted copy in Object
    # Storage now; if that isn't there, the copy inside the database snapshot is tried once the
    # snapshot is restored (fetch_key_from_snapshot).
    if obj_configured && run_with_passphrase ssh-key-restore --ssh-key "$KEY_PATH" --force --save-passphrase; then
      log "Fetched the deployment SSH key from Object Storage"
    else
      KEY_FROM_SNAPSHOT=1
      log "The deployment SSH key will be taken from the restored database"
      return 0
    fi
  else
    log "Generating a new deployment SSH key"
    runuser -u "$SERVICE_USER" -- ssh-keygen -q -t ed25519 -N "" -C "linode-instance-scheduler@$(hostname)" -f "$KEY_PATH"
  fi
  chmod 600 "$KEY_PATH"
}

fetch_key_from_snapshot() {
  [[ $KEY_FROM_SNAPSHOT -eq 1 ]] || return 0
  if run_with_passphrase ssh-key-restore --from-db --ssh-key "$KEY_PATH" --force --save-passphrase; then
    chmod 600 "$KEY_PATH"
    return 0
  fi
  die "the ORIGINAL deployment SSH key couldn't be recovered -- managed instances only trust it.
    Give it with --ssh-key PATH (or put the right passphrase in .env as LIS_SSH_KEY_PASSPHRASE).
    Only if it is truly lost: --new-ssh-key (then add the new public key to every instance, e.g.
    from Lish or rescue mode). Your instances and their data are not affected either way."
}

protect_ssh_key() {
  log "Storing the deployment SSH key encrypted (database, Object Storage, Linode SSH keys)"
  im ssh-key-protect --ssh-key "$KEY_PATH" || warn "the deployment SSH key isn't fully protected yet (see above); the scheduler retries every hour"
}

# --- dashboard ------------------------------------------------------------------------------------

node_is_new_enough() {  # node_is_new_enough PATH_TO_NODE
  "$1" -e 'const [a,b]=process.versions.node.split(".").map(Number); process.exit((a===20&&b>=19)||(a===22&&b>=12)||a>22?0:1)' 2>/dev/null
}

build_dashboard() {
  [[ $WITH_API -eq 1 && $WITH_DASHBOARD -eq 1 ]] || return 0
  [[ -d "$INSTALL_DIR/web" ]] || return 0
  local node_dir="" tmp=""
  if command -v node >/dev/null && command -v npm >/dev/null && node_is_new_enough "$(command -v node)"; then
    node_dir="$(dirname "$(command -v node)")"
  else
    local arch
    case "$(uname -m)" in x86_64) arch=x64 ;; aarch64|arm64) arch=arm64 ;; *) arch="" ;; esac
    if [[ -z "$arch" ]] || ! command -v curl >/dev/null; then
      warn "can't fetch Node.js for the dashboard build here -- skipping it (the API still works; see DEPLOYMENT.md to build it elsewhere)"
      return 0
    fi
    log "Fetching Node.js $NODE_BUILD_VERSION for the dashboard build (removed afterwards)"
    tmp=$(mktemp -d)
    local base="https://nodejs.org/dist/v$NODE_BUILD_VERSION" file="node-v$NODE_BUILD_VERSION-linux-$arch.tar.xz"
    if ! curl -fsSL "$base/$file" -o "$tmp/$file" || ! curl -fsSL "$base/SHASUMS256.txt" -o "$tmp/SHASUMS256.txt"; then
      warn "couldn't download Node.js -- skipping the dashboard"; rm -rf "$tmp"; return 0
    fi
    if ! (cd "$tmp" && grep " $file\$" SHASUMS256.txt | sha256sum -c --status); then
      warn "the Node.js download failed its checksum -- skipping the dashboard"; rm -rf "$tmp"; return 0
    fi
    tar -C "$tmp" -xJf "$tmp/$file"
    node_dir="$tmp/node-v$NODE_BUILD_VERSION-linux-$arch/bin"
  fi
  log "Building the web dashboard"
  if (cd "$INSTALL_DIR/web" && PATH="$node_dir:$PATH" npm ci --silent --no-audit --no-fund >/dev/null \
        && PATH="$node_dir:$PATH" npm run build --silent >/dev/null); then
    rm -rf "$INSTALL_DIR/web/node_modules"
  else
    warn "dashboard build failed -- the API still works"
  fi
  [[ -n "$tmp" ]] && rm -rf "$tmp"
  chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/web"
}

# --- HTTPS for the dashboard (Caddy) ------------------------------------------------------------

caddy_managed_domain() {  # the domain in this script's block of the Caddyfile, if any
  [[ -f "$CADDYFILE" ]] || return 0
  awk -v b="$CADDY_BEGIN" -v e="$CADDY_END" '$0==b{f=1;next} $0==e{f=0} f && /\{[[:space:]]*$/ {print $1; exit}' "$CADDYFILE"
}

caddyfile_is_stock() {  # the package's placeholder config (serves /usr/share/caddy on :80)
  [[ -f "$CADDYFILE" ]] || return 0
  local content
  content=$(grep -vE '^[[:space:]]*(#|$)' "$CADDYFILE")
  [[ -z "$content" ]] && return 0
  grep -q '/usr/share/caddy' <<<"$content" && [[ $(grep -cE '\{[[:space:]]*$' <<<"$content") -le 1 ]]
}

# This host's addresses, one per line: its own interfaces plus, through the Linode API, every
# public address of the instance it runs on (a VPC 1:1 NAT address isn't on any interface).
host_addresses() {
  hostname -I | tr ' ' '\n'
  (cd "$INSTALL_DIR" && runuser -u "$SERVICE_USER" -- "$PY" - 2>/dev/null <<'EOF') || true
from dotenv import load_dotenv
load_dotenv(".env")
import linode_engine as engine
import instance_manager as im
print("\n".join(sorted(im.scheduler_host_addresses(engine.build_client(engine.load_token())))))
EOF
}

setup_https() {
  [[ -n "$DOMAIN" ]] || DOMAIN=$(caddy_managed_domain)  # a re-run keeps the earlier domain
  [[ -n "$DOMAIN" ]] || return 0
  [[ "$DOMAIN" =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$ ]] \
    || { warn "'$DOMAIN' isn't a valid domain name -- skipping HTTPS setup"; return 0; }
  log "Setting up HTTPS for https://$DOMAIN (Caddy)"
  if ! command -v caddy >/dev/null; then
    if command -v apt-get >/dev/null; then
      DEBIAN_FRONTEND=noninteractive apt-get install -y -qq caddy >/dev/null
    elif command -v dnf >/dev/null; then
      dnf install -y -q caddy >/dev/null
    elif command -v zypper >/dev/null; then
      zypper -n -q install caddy >/dev/null
    fi
    command -v caddy >/dev/null || {
      warn "couldn't install Caddy from the system packages -- install it (https://caddyserver.com/docs/install), then re-run with --domain $DOMAIN"
      return 0
    }
  fi

  # DNS should already point here, or the certificate request fails.
  local resolved
  resolved=$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk 'NR==1{print $1}')
  if [[ -z "$resolved" ]]; then
    warn "$DOMAIN doesn't resolve yet -- create an A record pointing at this host's public IP; Caddy keeps retrying"
  elif ! host_addresses | grep -qx "$resolved"; then
    warn "$DOMAIN resolves to $resolved, which isn't an address of this host -- point its A record here; Caddy keeps retrying"
  fi

  # Write only our own marked block; never clobber a site someone else configured.
  local block backup="" tmp
  block=$(printf '%s\n%s {\n    reverse_proxy 127.0.0.1:%s\n}\n%s\n' "$CADDY_BEGIN" "$DOMAIN" "$API_PORT" "$CADDY_END")
  mkdir -p "$(dirname "$CADDYFILE")"
  tmp=$(mktemp)
  if [[ -f "$CADDYFILE" ]] && grep -qxF "$CADDY_BEGIN" "$CADDYFILE"; then
    awk -v b="$CADDY_BEGIN" -v e="$CADDY_END" '$0==b{skip=1} !skip{print} $0==e{skip=0}' "$CADDYFILE" > "$tmp"
    printf '%s\n' "$block" >> "$tmp"
  elif caddyfile_is_stock; then
    printf '%s\n' "$block" > "$tmp"
  else
    cat "$CADDYFILE" > "$tmp"
    printf '\n%s\n' "$block" >> "$tmp"
  fi
  if [[ -f "$CADDYFILE" ]]; then
    backup="$CADDYFILE.bak-$(date +%Y%m%d%H%M%S)"
    cp -p "$CADDYFILE" "$backup"
  fi
  cat "$tmp" > "$CADDYFILE"; rm -f "$tmp"
  if ! caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null 2>&1; then
    [[ -n "$backup" ]] && cp -p "$backup" "$CADDYFILE"
    warn "the Caddy configuration didn't validate -- left $CADDYFILE as it was (try: caddy validate --config $CADDYFILE)"
    return 0
  fi

  if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q '^Status: active'; then
    ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
  fi
  systemctl enable caddy >/dev/null 2>&1
  systemctl reload caddy 2>/dev/null || systemctl restart caddy

  for _ in $(seq 1 30); do
    if curl -fsS -o /dev/null --max-time 5 "https://$DOMAIN/health" 2>/dev/null; then
      log "Dashboard is live at https://$DOMAIN"
      return 0
    fi
    sleep 3
  done
  warn "https://$DOMAIN isn't answering yet. Check: the A record points here, ports 80 and 443 are open in the Linode Cloud Firewall, and 'journalctl -u caddy -n 30' shows a certificate obtained. Caddy keeps retrying on its own."
}

# --- services -------------------------------------------------------------------------------------

write_units() {
  log "Installing systemd services"
  cat > "$UNIT_DIR/$POLL_UNIT.service" <<EOF
[Unit]
Description=Linode Instance Scheduler - poller
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$PY instance_manager.py poll
Restart=always
RestartSec=5
# On stop/restart the scheduler lets a start/stop already running finish before exiting.
TimeoutStopSec=900
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
  if [[ $WITH_API -eq 1 ]]; then
    cat > "$UNIT_DIR/$API_UNIT.service" <<EOF
[Unit]
Description=Linode Instance Scheduler - REST API and dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$PY instance_manager.py serve-api --host 127.0.0.1 --port $API_PORT
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  fi
  local calendar
  case "$BACKUP_SCHEDULE" in hourly) calendar="hourly" ;; daily) calendar="*-*-* 02:17:00" ;; *) die "--backup-schedule must be hourly or daily" ;; esac
  cat > "$UNIT_DIR/$BACKUP_UNIT.service" <<EOF
[Unit]
Description=Linode Instance Scheduler - backup (database, groups, host keys)

[Service]
Type=oneshot
User=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$PY instance_manager.py backup --backup-dir $BACKUP_DIR
ExecStartPost=/usr/bin/find $BACKUP_DIR -type f -mtime +$BACKUP_KEEP_DAYS -delete
EOF
  cat > "$UNIT_DIR/$BACKUP_UNIT.timer" <<EOF
[Unit]
Description=Linode Instance Scheduler - scheduled backup

[Timer]
OnCalendar=$calendar
RandomizedDelaySec=300
Persistent=true

[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload
}

start_services() {
  log "Starting services"
  # restart, not just start: a re-run (an upgrade, or a dashboard built after the API was already
  # running) must pick up the new code.
  systemctl enable "$POLL_UNIT.service" >/dev/null 2>&1
  systemctl restart "$POLL_UNIT.service"
  if [[ $WITH_API -eq 1 ]]; then
    systemctl enable "$API_UNIT.service" >/dev/null 2>&1
    systemctl restart "$API_UNIT.service"
  fi
  systemctl enable --now "$BACKUP_UNIT.timer" >/dev/null 2>&1
}

stop_services() {
  systemctl stop "$POLL_UNIT.service" "$API_UNIT.service" 2>/dev/null || true
}

run_backup() {
  log "Taking a backup"
  im backup --backup-dir "$BACKUP_DIR" || warn "the backup reported a problem (see above)"
}

# --- modes ----------------------------------------------------------------------------------------

do_install() {
  install_packages
  install_code
  configure_env
  check_token
  setup_ssh_key
  protect_ssh_key
  build_dashboard
  write_units
  start_services
  setup_https
  run_backup
  log "Installed."
  cat <<EOF

  Deployment SSH public key -- every instance you manage must trust it (add it in Cloud Manager
  -> SSH Keys, or to /root/.ssh/authorized_keys on each instance):

$(sed 's/^/    /' "$KEY_PATH.pub" 2>/dev/null)

  Next:  cd $INSTALL_DIR && sudo -u $SERVICE_USER .venv/bin/python instance_manager.py onboard --name <name> --instance-id <id>
  $(if [[ -n "$DOMAIN" ]]; then echo "Dashboard: https://$DOMAIN"; else echo "API:   http://127.0.0.1:$API_PORT (re-run with --domain NAME for HTTPS, or see DEPLOYMENT.md)"; fi)
  Logs:  the dashboard's Activity and Logs pages, $INSTALL_DIR/state/logs/, or journalctl -u $POLL_UNIT -f
EOF
  obj_configured || warn "Object Storage isn't configured: only local snapshots in $BACKUP_DIR are kept, and they're lost with this host. Copy them off-host, or configure Object Storage and re-run install."
  if [[ -n "$NEW_PASSPHRASE" ]]; then
    printf '\n  Deployment SSH key passphrase (shown once; also in %s/.env as LIS_SSH_KEY_PASSPHRASE):\n\n      %s\n\n' "$INSTALL_DIR" "$NEW_PASSPHRASE"
    printf '  The key is stored encrypted with it in the database, every backup and Object Storage, and the\n'
    printf '  passphrase itself in a private StackScript in your Linode account, so a replacement host\n'
    printf '  fetches it with your API token. Keeping your own copy too is a good idea.\n'
  fi
}

do_recover() {
  cat <<EOF

  Recovery sets this host up as the replacement for a lost one. Make sure the OLD host is shut
  down or its poller is stopped: two pollers acting on the same instances at once can race each
  other.

EOF
  confirm "Is the old host gone or stopped?" || die "aborted"
  [[ -s "$INSTALL_DIR/state/instances.db" ]] && warn "$INSTALL_DIR already has a database -- it will be moved aside, not deleted."
  install_packages
  install_code
  configure_env
  check_token
  stop_services
  setup_ssh_key
  build_dashboard
  write_units

  local restored=0
  if [[ $REBUILD_ONLY -eq 0 ]]; then
    local args=(restore --force --yes)
    [[ -n "$KNOWN_HOSTS_IN" ]] && args+=(--known-hosts "$KNOWN_HOSTS_IN")
    if [[ -n "$FROM_FILE" ]]; then
      install -o "$SERVICE_USER" -m 600 "$FROM_FILE" "$INSTALL_DIR/state/restore-source.db"
      args+=(--from-file "$INSTALL_DIR/state/restore-source.db")
      log "Restoring the database from $FROM_FILE"
      im "${args[@]}" && restored=1
      rm -f "$INSTALL_DIR/state/restore-source.db"
    elif obj_configured; then
      [[ -n "$SNAPSHOT" ]] && args+=(--snapshot "$SNAPSHOT")
      log "Restoring the latest database snapshot from Object Storage"
      if im "${args[@]}"; then restored=1; else warn "no usable snapshot -- recovering from tags and Object Storage records only"; fi
    else
      warn "no snapshot source (no Object Storage, no --from-file) -- recovering from Linode tags only"
    fi
  fi
  if [[ $restored -eq 0 ]]; then
    local hosts_args=(restore --known-hosts-only)
    [[ -n "$KNOWN_HOSTS_IN" ]] && hosts_args+=(--known-hosts "$KNOWN_HOSTS_IN")
    { [[ -n "$KNOWN_HOSTS_IN" ]] || obj_configured; } && { im "${hosts_args[@]}" || warn "couldn't restore the trusted host keys"; }
  fi

  fetch_key_from_snapshot
  protect_ssh_key

  log "Rebuilding from Linode tags and Object Storage (adds anything newer than the snapshot)"
  im rebuild --ssh-key "$KEY_PATH" || warn "rebuild reported names it couldn't fully recover (see above)"
  log "Comparing every node with the account (adopts instances the snapshot doesn't know about)"
  im heal || warn "the self-healing check reported problems (see above)"

  if [[ $NO_START -eq 1 ]]; then
    log "Recovered, services NOT started (--no-start). Managed instances:"
    im list || true
    echo
    echo "  Review the list above, then: sudo $0 start --dir $INSTALL_DIR"
  else
    start_services
    setup_https
    run_backup
    log "Recovery complete. Managed instances:"
    im list || true
    catch_up_schedules
  fi
  cat <<EOF

  Check:
  - Anything listed as needs_manual_recovery: boot it once from Cloud Manager, then
    \`onboard --name <name> --instance-id <id> --force\`.
  - A node that refuses to start with a host-key warning was onboarded after the last backup:
    \`reset-host-key --name <name>\` (only after confirming it's really your instance).
  - API tokens and dashboard sessions come back only from a snapshot; otherwise create new tokens.
  - If this host has a new address or name, point DNS / your reverse proxy at it, and update the
    OAuth app's redirect URI and LINODE_OAUTH_REDIRECT_URI in $INSTALL_DIR/.env.
EOF
  [[ $NEW_SSH_KEY -eq 1 ]] && warn "a NEW SSH key was generated -- add $KEY_PATH.pub to every managed instance before its next start/stop."
  return 0
}

catch_up_schedules() {
  # Nodes left in the wrong state by the outage (beyond the scheduler's one-hour catch-up window):
  # shown for review, started/stopped only after a yes.
  echo
  im schedule-catch-up || return 0
  if im schedule-catch-up --json 2>/dev/null | grep -q '"name"'; then
    if [[ $ASSUME_YES -eq 0 ]] && confirm "Start/stop these now to match their schedule?"; then
      im schedule-catch-up --apply --yes --ssh-key "$KEY_PATH" || warn "some nodes didn't reach their scheduled state (see above)"
    else
      echo "  Left as they are. Review on the dashboard's Instances page, or: instance_manager.py schedule-catch-up"
    fi
  fi
}

do_status() {
  for u in "$POLL_UNIT.service" "$API_UNIT.service" "$BACKUP_UNIT.timer"; do
    printf '  %-36s %s\n' "$u" "$(systemctl is-active "$u" 2>/dev/null || true)"
  done
  local latest; latest=$(ls -1t "$BACKUP_DIR"/*.db 2>/dev/null | head -1 || true)
  printf '  %-36s %s\n' "latest local snapshot" "${latest:-none}"
  obj_configured && { echo "  Object Storage snapshots (latest 3):"; im restore --list 2>/dev/null | tail -3 | sed 's/^/    /'; }
  echo "  Managed instances:"; im list | sed 's/^/    /'
}

case "$MODE" in
  install) do_install ;;
  recover) do_recover ;;
  start) [[ -x "$PY" ]] || die "not installed in $INSTALL_DIR"; [[ -f "$UNIT_DIR/$API_UNIT.service" ]] || WITH_API=0; start_services; run_backup; catch_up_schedules ;;
  backup-now) [[ -x "$PY" ]] || die "not installed in $INSTALL_DIR"; run_backup ;;
  status) [[ -x "$PY" ]] || die "not installed in $INSTALL_DIR"; do_status ;;
esac
