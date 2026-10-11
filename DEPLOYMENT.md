# Deployment Guide — running this in production

This is the operations-focused companion to `README.md`. That document covers the day-to-day
commands (onboarding a node, setting a schedule, using the API); this one covers standing the
tool up as a persistent, unattended service: choosing a host, installing it, configuring
credentials, running the scheduler and API as real services that survive a reboot, backups, and
upgrades.

It assumes you've already read `README.md` §1–§4 (what this is, prerequisites, building or
migrating your first node) and have at least one node ready to manage. If you haven't done that
yet, start there — this guide is about running the tool itself, not about the Linode instances it
manages.

This guide covers the **default deployment model: a single VM**. If you already run a Linode
Kubernetes Engine (LKE) cluster and would rather run this tool there instead of provisioning a
separate host, see `DEPLOYMENT-LKE.md` instead — same application, same data model,
just packaged as a container. Nothing below changes if you're on LKE; the two guides are
alternatives, not sequential reading.

---

## Quick start: `install.sh`

`install.sh`, at the top of the repository, does everything in §2–§8 for you on a fresh host:
system packages, a dedicated service user, the Python environment, credentials, the deployment SSH
key, the poller and API services, the dashboard, and an hourly backup timer. The rest of this
guide explains each piece, for anyone who wants to set it up by hand or understand what the
script did.

```
git clone https://github.com/sandipgangdhar/linode-instance-scheduler.git
cd linode-instance-scheduler
sudo ./install.sh install --domain scheduler.example.com
```

It asks for your Linode API token, your Object Storage bucket (strongly recommended — it's what
makes moving to a new host lossless) and optionally a "Login with Linode" OAuth app. It generates
the deployment SSH key and a passphrase that encrypts its stored copies, and shows the passphrase
once at the end (see §8). With `--domain` (or when you answer the domain question),
it also installs Caddy and serves the dashboard at `https://<domain>`, with a certificate Caddy
obtains and renews on its own — the domain's A record must already point at this host's public
IP, and ports 80 and 443 must be open (Cloud Firewall and any host firewall; the script opens ufw
itself). Use the same domain in the OAuth app's callback URL: `https://<domain>/oauth/callback`.
It installs to `/opt/linode-instance-scheduler` and prints
the deployment's SSH public key at the end — add that key to every instance you'll manage. For a
non-interactive run, put the settings in a file and pass `--env-file <file> --yes`. Nothing is saved until every question is answered, so an interrupted
run (Ctrl+C, a dropped SSH session) leaves no half-written configuration; re-running asks again
for anything still missing, such as Object Storage or the "Login with Linode" settings. Run `sudo ./install.sh --help` for every option.

Re-running `install` on the same host is safe: it keeps the existing `.env`, SSH key and database,
updates the code and dependencies, rebuilds the dashboard and restarts the services — so after a
`git pull` it doubles as the upgrade command.

| Command | What it does |
|---|---|
| `sudo ./install.sh install` | First-time install (or upgrade, when re-run) |
| `sudo ./install.sh recover` | Set up a replacement host and restore everything — see §8.1 |
| `sudo ./install.sh start` | Start the services (after `recover --no-start`) |
| `sudo ./install.sh backup-now` | Back up right now |
| `sudo ./install.sh status` | Services, latest backups, managed instances |

The dashboard needs Node.js 20.19+ or 22.12+ to build. If the host's own Node.js is older or
missing, the script downloads the official Node.js release from nodejs.org, checks it against the
published SHA-256 checksums, uses it for the build, and removes it afterwards.

---

## 1. Deployment model — what you're actually setting up

This tool is **self-hosted, one deployment per Linode account** — there's no shared service, no
multi-tenant backend, nothing running outside infrastructure you control. A deployment is:

- **One host** (a small Linode instance works fine for this — see §2) running the Python code in
  this repository.
- **A local SQLite database** (`state/instances.db`) that's the source of truth for which
  instances/volumes/IPs belong to which logical name, what their schedules are, and their audit
  history. This is genuinely local state, not a mirror of something else — see §8 for why that's
  safe and how to back it up.
- **Up to three long-running pieces**, all optional except the first if you want automation at
  all:
  1. **The scheduler (`poll`)** — the process that actually fires `start`/`stop` when a schedule
     says it's due. Nothing is automated without this running somewhere.
  2. **The REST API (`serve-api`)** — optional; only needed if you want a web dashboard or your
     own scripts talking to this tool over HTTP instead of shelling out to the CLI.
  3. **The web dashboard** — optional; a static build served by the API process, not a separate
     service.

If you only ever plan to run commands by hand from a terminal, you don't need any long-running
service at all — just the CLI, run whenever you need it. This guide is for the common case where
you want schedules to actually enforce themselves unattended.

---

### Network layout for VPC and VLAN nodes

If the instances you'll schedule use a VPC or VLAN, give them their own VPC subnet and their own
VLAN label, and don't create other instances there by hand. While a node is stopped nothing holds
its private address: in a VPC another instance can be given it (the node is then moved to a free
address at its next start), and on a VLAN an instance given the same address conflicts with it
silently once both run, because Linode doesn't check VLAN addresses. Place this host in that same
subnet (or on that VLAN) so it can reach the nodes; the tool doesn't count its own host as a
conflict. Onboarding warns about any other instances it finds there, and
`instance_manager.py status --name <node> --check-network` repeats the check.

## 2. Choosing and preparing a host

**Requirements:**
- **Python 3.10 or newer.**
- **Outbound HTTPS access** to `api.linode.com` (every real operation) and, if you use the REST
  API with "Login with Linode," `login.linode.com` too. No inbound access is required unless
  you're exposing the REST API/dashboard to other people (§6).
- **SSH access (TCP 22) to every managed instance** — every start ends with a real SSH
  reachability check, and every stop reads the instance's current SSH keys. For an instance
  with only VPC/VLAN interfaces (no public IP), that means this host must sit inside the same
  VPC (or on the same VLAN), since the tool reaches it at its private address.
- **Node.js 20.19+ or 22.12+**, only if you're building the web dashboard (§7) — not needed for
  the CLI or poller alone. (`install.sh` fetches a suitable one for the build if the host's is
  older.)
- A **persistent, centralized machine** — not a laptop that sleeps or gets reimaged. A small,
  always-on Linode instance is a natural, if slightly self-referential, choice: nothing about
  running this tool's own host through this tool itself is required or recommended — keep the
  deployment host as a normal, separately-managed instance.

A single small instance (the cheapest general-purpose plan) is more than enough — this tool's own
resource use is minimal (a Python process ticking every few minutes, a small SQLite file, an
optional lightweight API server). Sizing should be driven by how many *managed* instances you
have, not by this tool's own overhead, and even fleets of dozens of managed nodes don't meaningfully
change that.

Create the deployment host itself the normal way (Cloud Manager, or your own existing
provisioning process) — nothing here is specific to it beyond the requirements above.

---

## 3. Installing the code

```
git clone https://github.com/sandipgangdhar/linode-instance-scheduler.git
cd linode-instance-scheduler
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Pick a real, deliberate install location — e.g. `/opt/linode-instance-scheduler` — rather than a
home directory, so the path is stable across whoever administers this host over time. The
commands and systemd units below assume this repository lives at a fixed path; substitute your
own throughout.

---

## 4. Configuring credentials

```
cp .env.example .env
chmod 600 .env
```

`chmod 600` matters here — `.env` holds real, sensitive credentials (see below), and by default a
freshly-copied file is often group/world-readable depending on your host's umask. This file is
already gitignored; never commit it, and never paste its contents anywhere outside this host.

Fill in:

- **`LINODE_API_TOKEN`** — a Personal Access Token scoped to **Linodes** (Read/Write), **Volumes**
  (Read/Write), and **IPs** (Read/Write); add **VPCs** (Read) too if any managed instance uses a
  VPC interface, and **StackScripts** (Read/Write) for the passphrase's recovery copy (§8).
  **Account** (Read Only) is enough to satisfy the tool's own startup check. Create one at Cloud
  Manager → Profile → API Tokens. This is the one credential every part of the tool
  uses to actually talk to Linode — the CLI, the poller, and the API server alike.
- **`LINODE_OAUTH_CLIENT_ID`/`LINODE_OAUTH_CLIENT_SECRET`/`LINODE_OAUTH_REDIRECT_URI`** — only
  needed if you're running the REST API/dashboard with "Login with Linode" (§6). Leave blank if
  you're only ever using the CLI and/or poller.
- **`LINODE_SSH_KEY_PATH`** — optional; only needed if your SSH keypair (see `README.md` §2)
  isn't at the tool's default path (`~/.ssh/linode_spike_key`). The CLI's own `--ssh-key` flag and
  this variable both point at the same default, so if you generated your key at the default path
  you don't need to set this at all.
- **`LIS_SSH_KEY_PASSPHRASE`** — encrypts the stored copies of the deployment SSH key (§8). At
  least 12 characters; `install.sh` generates one.
- **`LIS_ENV_BACKUP_RECIPIENTS`** — your age public key(s), for the encrypted copy of these
  settings only you can open (§8).
- **`LIS_ALERT_WEBHOOK_URL`, `LIS_ALERT_EMAIL_TO` and the `LIS_ALERT_SMTP_*` settings** —
  optional alerts (§8.2).
- **`API_ALLOWED_ORIGINS`** — only needed if a browser-based client (the dashboard, or your own
  frontend) will call the REST API from a *different* origin than the one serving it. Leave unset
  to disable CORS entirely — correct for the common case where the dashboard is served by the same
  `serve-api` process it calls.

**Verify it's all wired up correctly** before moving on:

```
python instance_manager.py list
```

This should print `No instances onboarded yet.` (or a real list, if you've already onboarded
some) — it's a purely local check and doesn't touch the Linode API at all, so it only confirms
your Python environment, not your token. To check the token itself:

```
python -c "import linode_engine as e; c = e.build_client(e.load_token()); e.auth_check(c); print('token OK')"
```

---

## 5. Running the scheduler as a persistent service

Nothing is automated until `poll` is actually running continuously somewhere. Run it under a real
process supervisor so it survives crashes and reboots — don't run it in a bare terminal/`screen`
session for anything beyond a quick test.

**systemd unit** (`/etc/systemd/system/linode-scheduler-poll.service`):

```ini
[Unit]
Description=Linode Instance Scheduler - poller
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=linode-scheduler
WorkingDirectory=/opt/linode-instance-scheduler
EnvironmentFile=/opt/linode-instance-scheduler/.env
ExecStart=/opt/linode-instance-scheduler/.venv/bin/python instance_manager.py poll
Restart=always
RestartSec=5
# Stopping lets a start/stop already in progress finish first (up to 15 minutes).
TimeoutStopSec=900

[Install]
WantedBy=multi-user.target
```

Create a dedicated, unprivileged system user to run this as (`useradd --system --home
/opt/linode-instance-scheduler --shell /usr/sbin/nologin linode-scheduler`), and make sure it owns
the repository directory (`chown -R linode-scheduler:linode-scheduler
/opt/linode-instance-scheduler`) — this process holds your Linode API token in memory and manages
real infrastructure, so it shouldn't run as `root` or as an interactive user's own account.

```
sudo systemctl daemon-reload
sudo systemctl enable --now linode-scheduler-poll
sudo systemctl status linode-scheduler-poll
journalctl -u linode-scheduler-poll -f     # live logs — this is where poll's own output goes
```

`Restart=always` is the deliberate choice here, not a stronger form of high availability: this
project's own design is "fast, safe recovery," not "never goes down" (see `README.md`'s own
troubleshooting notes on this) — a brief restart after a crash self-heals, and running two active
pollers at once is a correctness risk (both could race to fire the same schedule), not a safety
net. Don't scale this to more than one instance.

**Prefer cron over a supervisor?** `poll --once` runs exactly one check and exits — schedule that
on whatever interval you'd otherwise poll on (every minute or every few minutes). The continuous
`poll` is the better choice: it checks every 15 seconds and reacts as soon as a start or stop
finishes, so a start order (group dependencies) advances without waiting for the next cron run. Either approach is fine; pick whichever this host's existing operational conventions
already use.

Every schedule edit (via the CLI or, if you run it, the API) takes effect on the poller's *next*
tick automatically — there's nothing to reload or restart when you change a schedule.

---

## 6. Running the REST API and dashboard as a persistent service (optional)

Skip this section entirely if you only use the CLI and poller.

**systemd unit** (`/etc/systemd/system/linode-scheduler-api.service`):

```ini
[Unit]
Description=Linode Instance Scheduler - REST API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=linode-scheduler
WorkingDirectory=/opt/linode-instance-scheduler
EnvironmentFile=/opt/linode-instance-scheduler/.env
ExecStart=/opt/linode-instance-scheduler/.venv/bin/python instance_manager.py serve-api --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```
sudo systemctl daemon-reload
sudo systemctl enable --now linode-scheduler-api
```

**Binds to `127.0.0.1` by default, deliberately.** Nothing outside this host can reach it directly
— that's intentional, not a bug to work around by binding `0.0.0.0` and exposing it raw. Put a
real reverse proxy in front for anything beyond local testing, both for TLS (real "Login with
Linode" OAuth callback URLs must be `https://`) and so the API/dashboard aren't the first thing an
attacker's port scan finds.

**Example using Caddy** (simplest option — automatic HTTPS via Let's Encrypt, no manual
certificate management; `install.sh --domain <name>` sets this up for you, writing only its own
marked block in `/etc/caddy/Caddyfile` and keeping any other sites there):

```
your-deployment-host.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

That's the entire config — Caddy handles obtaining and renewing the TLS certificate on its own.
If you already run nginx or another proxy for other services on this host, a plain
`proxy_pass http://127.0.0.1:8000;` block works identically; the tool has no opinion on which
proxy you use.

**Registering "Login with Linode" OAuth** (skip if you'll only ever call the API with your own
tooling and don't need browser login):

1. Cloud Manager → Profile → OAuth Apps → Create an App. Callback URL:
   `https://your-deployment-host.example.com/oauth/callback` — must match your `.env`'s
   `LINODE_OAUTH_REDIRECT_URI` **exactly**, including the scheme (`https://`, once you have a real
   proxy/TLS in front — see above).
2. Copy the **Client ID** and **Client Secret** into `.env` (§4).
3. Restart the API service (`sudo systemctl restart linode-scheduler-api`) so it picks up the new
   values.
4. Visit `https://your-deployment-host.example.com/login` and confirm a real login completes and
   lands you on the dashboard (or, if you haven't built it yet, a plain JSON pointer at `/docs` —
   see §7).

Each deployment registers its **own** OAuth App, in its **own** Linode account — there's no shared
login service any other deployment of this tool uses.

---

## 7. Building the web dashboard (optional)

Only relevant if you're running the REST API (§6). Requires Node.js 20.19+ or 22.12+ **on the machine
doing the build** — this can be the deployment host itself, or your own laptop/CI, since only the build
*output* needs to reach the deployment host.

**Building on the deployment host directly** (simplest):

```
cd web
npm install
npm run build
cd ..
sudo systemctl restart linode-scheduler-api
```

`npm run build` produces `web/dist/`; `serve-api` serves it automatically at `/` and `/ui/*` the
next time it starts, with no separate configuration. If `web/dist/` doesn't exist yet, `/` degrades
gracefully to a JSON pointer at `/docs` (the API's own interactive documentation) instead of a
broken page — so it's safe to skip this section entirely and add the dashboard later.

**Building elsewhere and deploying just the output** — if you'd rather not install Node on the
production host at all: run the same `npm install && npm run build` on your own machine, then copy
the resulting `web/dist/` directory to the same path on the deployment host (`rsync -av web/dist/
your-host:/opt/linode-instance-scheduler/web/dist/`), and restart the API service there.

Re-run the build (and restart the service) after pulling a new version of this repository if
`web/src/` changed — see §9.

---

## 8. Backups and disaster recovery

Three things live only on the deployment host, and a replacement host needs all three:

| What | Where | How it's protected |
|---|---|---|
| The database (instances, schedules, groups, hooks, start order, API tokens, history) | `state/instances.db` | `backup`: a full snapshot to Object Storage and/or a local directory. Also, independently: Linode tags on every managed resource plus per-instance, per-group and per-API-token records in Object Storage, which `rebuild` and `restore` read (a token revoked after a snapshot stays revoked) |
| The trusted SSH host keys of your instances | `state/known_hosts` | `backup` saves a copy to Object Storage and next to each local snapshot |
| The deployment SSH private key | `keys/deploy_key` (or `LINODE_SSH_KEY_PATH`) | Kept automatically, encrypted, in the database (so in every snapshot) and in Object Storage; its public half in your Linode profile's SSH keys; the passphrase's recovery copy in a private StackScript in your account |

**Routine backups.** `install.sh` installs a timer that runs `backup --backup-dir
/opt/linode-instance-scheduler/backups` every hour and keeps two weeks of local snapshots. By hand,
or from your own cron:

```
python instance_manager.py backup --backup-dir /opt/linode-instance-scheduler/backups
```

`backup` takes a consistent snapshot even while the poller and API are writing. With Object Storage
configured it also re-uploads every instance and group record, a full database snapshot and the
trusted host keys. Local snapshots alone are lost with the host — copy them elsewhere, or use Object
Storage.

**Backup on every change.** With Object Storage configured, the scheduler also uploads a fresh
database snapshot within about a minute of any change — a start or stop, a schedule, group, hook,
holiday or token change, the activity log — to one object, `full-backups/instances-continuous.db`,
overwritten each time. Losing the host then loses at most about a minute of changes, not up to an
hour. The hourly snapshots stay as a history to go back to.

**Old snapshots** are pruned after each hourly backup: every snapshot from the last 48 hours is
kept, then the newest one of each day for 30 days, then none
(`LIS_BACKUP_KEEP_ALL_HOURS`/`LIS_BACKUP_KEEP_DAILY_DAYS`). The newest snapshot and the continuous
one are never deleted.

**The SSH key.** Every managed instance trusts exactly one key, the deployment's own. A replacement
host can't manage anything without it, so it's kept recoverable automatically (`install.sh`, every
backup, and the scheduler every hour do this; `ssh-key-protect` does it on demand):

- encrypted (AES-256-GCM, key derived by scrypt from `LIS_SSH_KEY_PASSPHRASE`) in the database, so
  every snapshot carries it, and in Object Storage;
- its public half under your Linode profile's SSH keys;
- the passphrase itself in a private StackScript in your Linode account (`lis-recovery-…`, visible
  only to your account's users with StackScripts access; don't run or delete it).

A replacement host fetches the passphrase with the API token it needs anyway, so recovery needs
nothing extra from you.

**Your own encrypted copy of the settings (recommended).** Give `install.sh` your age public key
(`--env-backup-recipient age1…`, or answer the question; make the keypair on your own machine with
`age-keygen -o key.txt` and keep `key.txt` in your password manager). The scheduler then keeps
`deployment/env.age` — the whole `.env`: API token, bucket keys, OAuth, alert settings, the key
passphrase — and `deployment/ssh-key.age` in your bucket, encrypted to that key and re-uploaded
whenever either changes. This host can write them but never read them back. On a replacement host
you then enter only the bucket settings and your age identity:

```
sudo ./install.sh recover --env-from-backup --age-identity ~/key.txt
```

Set it up later with `env-backup --set-recipients age1…` or on the System backup page. The copies
are plain age files: `age -d -i key.txt env.age` opens them anywhere. This adds no new way in: anyone holding a full-access Linode token can
already get root on every instance (rescue mode, root password reset, Lish), and the passphrase
opens nothing without the encrypted key, which needs your Object Storage keys or a database
snapshot. Keeping your own copy of the passphrase (shown once at install) is still a good idea.
The System backup page and `alerts-check` show whether every copy is in place.

### 8.1 Moving to a new host (the old one is lost)

On a fresh host, with the old one shut down or gone:

```
git clone https://github.com/sandipgangdhar/linode-instance-scheduler.git
cd linode-instance-scheduler
sudo ./install.sh recover
```

Give it the same API token and the **same bucket** the old host used. Leave the passphrase question
blank: it's fetched from your Linode account. It then, before starting anything:

1. installs everything exactly as `install` does, but leaves the services stopped;
2. fetches and decrypts the SSH key from the bucket, or from the restored database (or takes the
   original key file with `--ssh-key <path>`); with `--env-from-backup`, every setting and the key
   come from your age-encrypted copy instead, and only the bucket settings are asked;
3. restores the newest database snapshot from the bucket — usually the continuous one, a minute or
   so old (`--snapshot <key>` for an older one, `--from-file <path>` for a local snapshot) — and
   the trusted host keys;
4. runs `rebuild`, which reads Linode's tags and the bucket's instance and group records to add
   anything onboarded or changed after that snapshot, without touching what the snapshot restored —
   including a node whose tags were removed, found through its Object Storage record;
5. runs `heal`, which compares every node with the account: a node the snapshot thinks is stopped
   but whose OS volume is attached to a running instance is recorded as that instance (never a
   second one created); a leftover blank instance from a start that never finished is deleted; a
   stale "in progress" mark is cleared;
6. starts the services and takes a fresh backup;
7. lists nodes that aren't in the state their schedule wants (if the outage lasted longer than the
   scheduler's one-hour catch-up window) and asks whether to start/stop them now.

Add `--no-start` to stop after step 4 and review `instance_manager.py list` first; then run
`sudo ./install.sh start`.

**With no snapshot at all** (`--rebuild-only`, or a bucket with none), `rebuild` alone still brings
back every instance, its schedule, group membership, each group's schedule, hooks and start order
(including groups with no members, from their Object Storage records), and a stopped instance's
network and SSH settings. Two things exist only in the database and need redoing: API tokens (create
new ones) and the audit history. A manual-override timer that was counting down is reported and
not re-armed.

**What to check afterwards:**

- An instance shown as `needs_manual_recovery`: boot it once from Cloud Manager, then
  `onboard --name <name> --instance-id <id> --force`.
- Nodes left off their schedule: the dashboard's Instances page lists them for review, or run
  `schedule-catch-up` (add `--apply` to act on them after confirming).
- An unfinished migration: resume it, or cancel it (`migrate-abandon`, or "Cancel this migration"
  in the dashboard) — the original instance boots from its own disk again.
- An instance that refuses to start with a host-key warning was onboarded after the last backup
  of the host keys — confirm it's really your instance, then `reset-host-key --name <name>`.
- If the new host has a different address or name: point DNS and your reverse proxy at it, and
  update the OAuth app's redirect URI and `LINODE_OAUTH_REDIRECT_URI` in `.env`.

Never run the old and new hosts' pollers at the same time — two pollers acting on the same
instances can race each other. `recover` asks you to confirm the old host is stopped. The SSL
certificate needs nothing: Caddy on the new host obtains a new one as soon as the domain points
at it.

**Without `install.sh`**, the same steps by hand: install as in §3–§6 with the original SSH key in
place and the services stopped, then:

```
python instance_manager.py ssh-key-restore --save-passphrase   # passphrase from your Linode account
python instance_manager.py restore              # newest snapshot + host keys (--list, --snapshot, --from-file)
python instance_manager.py ssh-key-restore --from-db --force --save-passphrase   # if the bucket had no key copy
python instance_manager.py rebuild
python instance_manager.py heal
python instance_manager.py schedule-catch-up   # review; --apply to act
sudo systemctl start linode-scheduler-poll linode-scheduler-api
```

### 8.2 Alerts

Set either or both channels on the dashboard's **System backup** page (Alerts form, then **Send
test alert**), with `alerts-config` (`--email-to`, `--smtp-host`, `--smtp-port`, `--smtp-user`,
`--smtp-password` to be prompted, `--webhook-url`), or in `.env`. Running services pick changes up
without a restart. Use the SMTP server of a mail account you already have (Google Workspace:
`smtp.gmail.com`:587 with an app password; Microsoft 365: `smtp.office365.com`:587) or a sending
service (SendGrid, Mailgun, Amazon SES, Brevo; port 2525 avoids Linode's SMTP block). The settings:

- **Webhook** — `LIS_ALERT_WEBHOOK_URL`. Receives a JSON POST `{"text", "event", "details",
  "host"}`; Slack and Microsoft Teams incoming webhooks accept it as is.
- **Email** — `LIS_ALERT_EMAIL_TO` (comma-separated), `LIS_ALERT_SMTP_HOST`,
  `LIS_ALERT_SMTP_PORT` (587), `LIS_ALERT_SMTP_SECURITY` (`starttls`, `ssl` or `none`),
  `LIS_ALERT_SMTP_USER`, `LIS_ALERT_SMTP_PASSWORD`, `LIS_ALERT_EMAIL_FROM`. Linode blocks
  outbound SMTP ports on new accounts until you ask support to open them; a mail provider that
  accepts another port works too.

An alert is sent when one of these starts, again every 6 hours while it lasts, and once when it
clears: the scheduler hasn't checked in for 10 minutes (noticed by the API service), backups are
failing or the daily restore test fails, the deployment SSH key isn't stored in every place, nodes
stay off their schedule, the Linode API token is rejected or expires within 14 days, the disk
holding the database is nearly full, Shutdown Watchdog is off on this host, or this host is
standing by because another scheduler holds the lease (§8.3).

**When the whole host is gone** nothing on it can send an alert. Two things cover that:
`install.sh` turns on Linode's **Shutdown Watchdog** for this host, so a crash or unexpected
power-off is followed by Linode booting it again (and systemd restarting the services); and
`LIS_HEARTBEAT_URL` (`--heartbeat-url`) takes a ping URL from an uptime monitor such as
Healthchecks.io, Better Stack or Uptime Kuma. The scheduler pings it every minute, and the monitor
tells you when the pings stop. Linode's own resource-usage email alerts can't do this: they fire
only when CPU, disk or network usage goes *above* a threshold, and a dead host has no usage.
`host-watchdog` shows the watchdog, `--enable` turns it on.

### 8.3 One active scheduler at a time

With Object Storage configured, the active scheduler holds a lease object in the bucket and renews
it every 30 seconds; a lease not renewed for 3 minutes has expired. A scheduler that finds a fresh
lease it doesn't hold **stands by**: it doesn't start, stop or heal anything and doesn't upload
backups, and it raises a `lease` alert. That makes it safe if an old host comes back after a new
one took over. `recover` takes the lease over straight away (you confirmed the old host is gone);
otherwise a replacement takes it once the old lease expires, and a cleanly stopped scheduler gives
it up at once. `scheduler-lease` shows who holds it; `scheduler-lease --take-over` (or the System
backup page) makes this host the active one. Without Object Storage there's no shared place for a
lease, so make sure only one scheduler runs.

### 8.4 Restore testing and upgrades

Every day the scheduler restore-tests the backups without touching the live database: it downloads
the newest snapshot, checks its checksum and SQLite's integrity check, that it isn't more than a day
old, that the continuous copy holds every node, and that the stored copies of the SSH key decrypt
to the real key. A failure is alerted. `backup-verify` runs it on demand. No snapshot that fails
the integrity check is ever uploaded, so a damaged database can't replace a good copy.

Re-running `install.sh install` to upgrade first takes a snapshot with the old code into
`backups/pre-upgrade-<time>/`; the end of the run says how to go back to the previous version with
it.
`alerts-test` sends a test message; `alerts-check` lists what would be alerted right now and exits
1 if anything — handy for your own monitoring. The System backup page shows the same.

---

## 9. Upgrading

With `install.sh`: pull the new version into your clone and re-run it — it updates the code and
dependencies, rebuilds the dashboard and restarts the services, keeping `.env`, the SSH key and the
database:

```
cd linode-instance-scheduler && git pull && sudo ./install.sh install
```

By hand:

```
cd /opt/linode-instance-scheduler
git pull
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart linode-scheduler-poll linode-scheduler-api   # the second only if you run it
```

If `web/src/` changed in the update, also rebuild the dashboard (§7) before restarting the API
service. The database schema upgrades itself automatically and safely on first connection after
an update — there's no separate migration step to run by hand.

---

## 10. Security checklist

- `.env` is `chmod 600`, owned by the same unprivileged user the services run as, and never
  committed to version control (already gitignored).
- The dedicated SSH keypair (`README.md` §2) is used **only** for this tool — never a personal or
  shared key — so it can be revoked independently without affecting anything else.
- `LINODE_API_TOKEN` is scoped to exactly what's needed (§4), not a token with broader account
  access than this tool actually uses.
- The REST API binds to `127.0.0.1` and sits behind a real TLS-terminating reverse proxy (§6) —
  never exposed directly on a public interface.
- If an OAuth Client Secret or API token is ever pasted somewhere it shouldn't have been (a chat
  log, a support ticket, a shared document), rotate it: Cloud Manager lets you reset an OAuth
  App's secret and revoke/recreate a Personal Access Token independently, with no code changes
  needed here beyond updating `.env` and restarting the affected service.
- Run services as a dedicated unprivileged system user (§5), never as `root`.
