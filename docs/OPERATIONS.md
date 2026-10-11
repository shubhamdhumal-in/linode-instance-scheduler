<!--
docs/OPERATIONS.md -- CUSTOMER-FACING DISTRIBUTION

Day-2 operations reference for this Instance Scheduler deployment:
running the poller, backup & recovery, security operations, upgrading,
and troubleshooting. Read the project README first for what this is and
how to deploy it, and INSTANCE-SCHEDULER-DEFINITIVE-GUIDE.html for the
full architectural reference. Written for the default single-VM
deployment (DEPLOYMENT.md); if you're running this on Kubernetes/LKE
instead (DEPLOYMENT-LKE.md), the same day-2 concepts apply -- only
"Installing and configuring" and the two systemd sections below are
VM-specific, everything else (backup & recovery, security operations,
troubleshooting) is identical either way.

(c) Linode Instance Scheduler | Developed by Sandip Gangdhar | 2026
-->

# Operations Guide

## Installing and configuring

This section covers the default VM deployment. Running this on an existing Kubernetes/LKE
cluster instead is a separate path with its own guide, DEPLOYMENT-LKE.md -- skip ahead to
"What you're running" below if that's what you're on; everything from there onward applies
regardless of which deployment model you chose.

**The quick way:** `sudo ./install.sh install` from a clone sets up everything below — service
user, environment, credentials, SSH key, services, dashboard and an hourly backup timer — and
`sudo ./install.sh recover` sets up a replacement host with full recovery (see "Moving to a new
host" below, and `DEPLOYMENT.md`). The rest of this section is the manual equivalent.

The tool is a plain Python program with no database server or other infrastructure of its own
to stand up — cloning the repository, creating a virtual environment, and installing its
dependencies is the entire install. Run this on one centralized, persistent machine your whole
team uses (not a personal laptop) — the local registry lives only on whatever machine runs
these commands, so a second, disconnected clone on someone else's laptop would have no idea
what's already onboarded:

```bash
git clone https://github.com/sandipgangdhar/linode-instance-scheduler.git
cd linode-instance-scheduler/spike
python3 -m venv .venv
source .venv/bin/activate
pip install --require-hashes -r requirements.lock
```

`requirements.lock` pins every dependency with its SHA-256 hashes; `--require-hashes` makes `pip`
refuse anything that isn't exactly what was released.

Configuration is one flat file, `.env`, read once at process startup:

```bash
cp .env.example .env
```

Open `.env` and fill in `LINODE_API_TOKEN` — a Linode Personal Access Token scoped read/write to
**Linodes**, **Volumes**, and **IPs** (add **VPCs** (Read) too if any managed instance uses a VPC
interface), the only setting required to run anything at all. **Account** (Read Only) is enough
to satisfy the tool's own startup check. Every other variable is optional and only matters if
you're using the specific feature it enables — see "Environment configuration reference" below
for the full list.

Confirm the install itself is working (a purely local check, no token required yet):

```bash
python instance_manager.py list
```

This prints `No instances onboarded yet.` (or a real list, if some are already onboarded) once
the environment and dependencies are set up correctly. To confirm the token itself is valid:

```bash
python -c "import linode_engine as e; c = e.build_client(e.load_token()); e.auth_check(c); print('token OK')"
```

From there, onboarding your first instance and running the scheduler/API server as supervised
background processes (below) is the rest of getting to a genuinely running deployment. See the
project README for the fuller first-node walkthrough (creating a node, attaching storage,
Path B migration if you're bringing an existing instance under management) and the Definitive
Guide's own Deployment Model chapter for why installation looks like this.

## What you're running

Two long-lived processes matter for day-to-day operation:

- **The scheduler (`poll`)** — checks every managed instance's effective schedule every 15
  seconds (and again whenever a start or stop finishes) and hands each due start or stop to a
  pool of workers. When it is stopped (`systemctl stop`, an upgrade, a pod shutdown) it takes no
  new work and waits for any start or stop already running to finish before exiting.
  Nothing about manual `start`/`stop` requires this to be running, but no schedule or group
  membership is enforced automatically unless it is.
- **The API server (`serve-api`)** — serves the REST API and, once built, the web dashboard.
  Only needed if you want dashboard/API access; the CLI works independently of it.

Both are ordinary long-running processes meant to run under your own process supervisor, not
inside an interactive shell session.

### High availability — one supervised process, not active-active or active-passive

Run exactly **one** instance of `poll`, ever — never two running at once, and never a hot
standby waiting to take over. This is a deliberate design choice, not a gap:

- **The scheduler doesn't serve live traffic.** It evaluates schedules on a short, fixed
  interval with a tolerant matching window (an hour by default), not on every
  incoming request. If the process is briefly down — a crash, a host reboot, a deploy — the
  next due action just fires a few seconds late once your process supervisor restarts it.
  Nothing downstream is waiting on it in real time, so nothing breaks. Manual `start`/`stop`
  (CLI or API) keeps working the entire time regardless, since it doesn't depend on `poll`
  being up at all.
- **Active-active would trade that harmless delay for a real correctness risk.** Two scheduler
  processes with no genuine coordination between them (a distributed lock, leader election, a
  consensus protocol — the actual infrastructure "active-active" requires) could race to act on
  the same instance at the same moment, or have one fire a start while the other is mid-firing
  a stop for the same node. That's a new failure mode traded in to solve a problem — a few
  seconds of scheduler downtime — that a plain restart already solves for free.
- **Active-passive needs its own always-on standby, plus failure-detection machinery** (health
  checks, a way for the standby to agree it should take over, a shared or replicated view of
  state) to be worth anything — real infrastructure, kept running and paid for continuously, to
  protect against an outage a single supervised process already recovers from in seconds.
- **This is a cost decision as much as an architectural one.** The whole point of this product
  is eliminating payment for compute that isn't earning its keep. Running a second, always-on
  scheduler instance purely as insurance against a multi-second restart would mean paying for
  exactly the kind of idle, always-on capacity this tool exists to help you stop paying for —
  just moved onto the tool's own infrastructure instead of your managed fleet. The model used
  here costs nothing extra: one process, under whatever process supervisor already ships with
  your host OS, on the one machine you're already running this from.

If the underlying data is ever lost too (not just the process being briefly down), recovery
still doesn't need a second live system standing by — see "Backup & recovery" below for the
tag-based and Object Storage-backed recovery path, and the Definitive Guide's own Deployment
Model chapter for the fuller comparison against active-active/active-passive designs.

### Running the scheduler under systemd

VM deployment only. On Kubernetes/LKE, the Deployment in DEPLOYMENT-LKE.md already supervises
both processes the same way systemd does here (automatic restart on crash, automatic start on
node reboot) -- there's nothing further to set up; skip to "Backup & recovery" below.

```ini
# /etc/systemd/system/instance-scheduler-poll.service
[Unit]
Description=Instance Scheduler - scheduler loop
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/instance-scheduler
ExecStart=/opt/instance-scheduler/.venv/bin/python3 instance_manager.py poll
Restart=always
RestartSec=15
# Stopping lets a start/stop already in progress finish first (up to 15 minutes).
TimeoutStopSec=900
StandardOutput=append:/var/log/instance-scheduler-poll.log
StandardError=append:/var/log/instance-scheduler-poll.log

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now instance-scheduler-poll.service
```

`Restart=always` plus `WantedBy=multi-user.target` covers both a process crash (auto-restart)
and a full host reboot (auto-start on boot) — confirm both with:

```bash
systemctl is-active instance-scheduler-poll.service     # should say "active"
systemctl is-enabled instance-scheduler-poll.service     # should say "enabled"
```

### Running the API server / dashboard under systemd

Same pattern, a second unit:

```ini
# /etc/systemd/system/instance-scheduler-api.service
[Unit]
Description=Instance Scheduler - API server & dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/instance-scheduler
ExecStart=/opt/instance-scheduler/.venv/bin/python3 instance_manager.py serve-api
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
```

## Environment configuration reference

Configured via a single `.env` file in the deployment directory (copy `.env.example` and fill
in real values — never commit the real file).

| Variable | Required | What it does |
|---|---|---|
| `LINODE_API_TOKEN` | Yes | A Linode Personal Access Token scoped read/write to Linodes, Volumes, IPs and StackScripts (the last for the SSH key passphrase's recovery copy; add VPCs (Read) too if any managed instance uses a VPC interface); Account (Read Only) is enough to satisfy the tool's own startup check. The one credential that actually performs every create/delete/tag/volume operation. Used by the CLI, the scheduler, and the API server alike. |
| `LINODE_SSH_KEY_PATH` | No (has a default) | Path to this deployment's own SSH private key, used for every reachability check and `authorized_keys` capture. See the Definitive Guide's Security section for why this is one deployment-wide key, never a per-instance stored credential. |
| `LINODE_OAUTH_CLIENT_ID` / `LINODE_OAUTH_CLIENT_SECRET` | Only if using the dashboard's "Login with Linode" | Register your own OAuth Client in your own Linode account (Cloud Manager → Profile → OAuth Apps) — see "Setting up dashboard login" below. |
| `LINODE_OAUTH_REDIRECT_URI` | Only if using dashboard login | Must exactly match the redirect URI configured on the OAuth App above. |
| `LINODE_API_MAX_REQUESTS_PER_SECOND` | No (default 10) | Cap on Linode API requests per second for one process; all parallel `poll` workers share it. A rate-limited (429) response also pauses every worker for as long as the API asks. `0` removes the cap. |
| `ACTIVITY_LOG_RETENTION_DAYS` | No (default 30) | How long the dashboard's Activity entries are kept. Service log files under `state/logs/` rotate by size (5 MB, three old files) regardless. |
| `API_ALLOWED_ORIGINS` | No | Comma-separated browser origins allowed to call the API cross-origin (CORS). Leave unset unless a separately-hosted frontend needs to call this API from a different origin. |
| `LINODE_OBJ_STORAGE_BUCKET` / `LINODE_OBJ_STORAGE_ENDPOINT` / `LINODE_OBJ_STORAGE_ACCESS_KEY` / `LINODE_OBJ_STORAGE_SECRET_KEY` | No | Optional Object Storage backup of each instance's full record, for fuller disaster recovery than tags alone provide. See "Setting up Object Storage backup" below. All four must be set together — leave all four unset to skip this layer entirely. |
| `LIS_SSH_KEY_PASSPHRASE` | Yes (generated by `install.sh`) | Encrypts the stored copies of the deployment SSH key (database, every snapshot, Object Storage). At least 12 characters. Its recovery copy lives in a private StackScript in your Linode account. |
| `LIS_ENV_BACKUP_RECIPIENTS` | No | Your age public key(s). An encrypted copy of `.env` and the deployment SSH key is kept in Object Storage that only your age identity opens (`env-backup`, `env-restore`, `install.sh recover --env-from-backup`). |
| `LIS_HEARTBEAT_URL` | No | An uptime monitor's ping URL; the scheduler pings it every minute so the monitor can tell you when this host is gone. |
| `LIS_BACKUP_KEEP_ALL_HOURS` / `LIS_BACKUP_KEEP_DAILY_DAYS` | No (48 / 30) | Pruning of old database snapshots in Object Storage: everything from the last N hours, then one a day for N days. |
| `LIS_ALERT_WEBHOOK_URL` | No | Alerts as a JSON POST (Slack/Teams incoming webhooks accept it). See "Alerts" below. |
| `LIS_ALERT_EMAIL_TO`, `LIS_ALERT_SMTP_HOST`, `LIS_ALERT_SMTP_PORT`, `LIS_ALERT_SMTP_SECURITY`, `LIS_ALERT_SMTP_USER`, `LIS_ALERT_SMTP_PASSWORD`, `LIS_ALERT_EMAIL_FROM` | No | Alerts by email over SMTP (port 587 with starttls by default). Linode blocks outbound SMTP ports on new accounts until support opens them. |
| `LIS_DELETION_GRACE_DAYS` | No (default 7) | Days `offboard` and `backup-delete` keep resources, recoverable, before deleting them. `0` deletes at once. See "Offboarding and pending deletions" below. |
| `LIS_RECOVERY_DRILL_DAYS` | No (off) | Run a recovery drill every N days (e.g. `7`). See "Recovery drills" below. |
| `LIS_ALLOW_STANDBY_CHANGES` | No | `1` lets a standby host (another scheduler holds the lease) make changes anyway. Normally left unset. |
| `LINODE_SSH_PREVIOUS_KEY_PATH` | Set automatically | During a deployment SSH key rotation, the previous key, offered alongside the new one until every node has moved. Removed when the rotation finishes; don't set it by hand. |

## Backup & recovery

This tool already protects every managed instance's identity, schedule, and group membership
against total local database loss automatically, at no setup cost — see "How recovery actually
works" below. Object Storage is an *additional*, optional layer that closes the remaining gap
(recovering a stopped instance's network configuration and SSH access fully, not just partially)
and is worth setting up for anything beyond a purely disposable dev/test fleet.

### How recovery actually works

Two independent backups exist alongside the local database, and neither is something you have to
maintain by hand:

- **Linode's own resource tags** — every onboard, start, and stop writes the instance's identity (OS
  volume, data volumes, reserved IP), individual schedule, group membership, a simple network
  configuration (if it's a single public interface), and a reference to any SSH key that's also
  registered on your Linode account, directly onto the resources themselves. This happens
  automatically, for every managed instance, with nothing to configure. Hooks are referenced from
  tags too: a pointer to each hook's stored copy, or (without Object Storage) a short command
  written inline.
- **Object Storage** (optional) — the same operations also back up a complete record of
  everything tags can't hold: a more complex network configuration (multiple interfaces, VPC,
  VLAN), any SSH key not registered on your Linode account, and the instance's plan/firewall/
  placement-group/maintenance-policy/watchdog/label/tags. It also holds every pre-stop hook and
  post-start check (commands, script paths, and uploaded scripts, for instances and groups), each
  stored under a fingerprint of its own content that the instance's tags point at. And it holds one
  record per schedule group (schedule, hooks, and the groups it depends on), updated on every change
  to the group — the only way a group with **no members** survives a database loss, since an empty
  group has no volume to carry tags. This layer only exists if you configure it (below).

Both are best-effort and self-healing: a failed backup never blocks the real stop/onboard
operation, and the very next time that instance is touched, its full current state is backed up
again from scratch — nothing needs to be manually retried. Tags heal the same way: every start and stop
rewrites an instance's schedule, group, start-order, manual-only, identity and hook tags from the
database, so a tag write that failed when a setting was changed (shown as a warning) is corrected
within one cycle. An individual schedule is also kept in the instance's Object Storage record, and
`rebuild` uses that copy when the schedule tags are missing or unreadable. See the Definitive Guide, Part 5, for
the full mechanics, including exactly what's read from where during recovery and a complete
table of edge cases.

### Setting up Object Storage backup

1. In Cloud Manager: **Object Storage → Create Bucket.** Any region is fine; note the endpoint
   shown for it.
2. **Object Storage → Access Keys → Create Access Key.** Scope it to just this one bucket, not
   account-wide, if given the option.
3. Save the settings with `backup-config` (or the dashboard's **System backup** page). It writes a
   small test object, reads it back and deletes it, and only saves the settings (to `.env`, mode
   600) if that works; it prompts for the keys so they never appear in your shell history:
   ```bash
   python instance_manager.py backup-config --bucket your-bucket-name \
       --endpoint https://your-region.linodeobjects.com
   ```
   The running scheduler and API pick the settings up within seconds -- no restart needed. (Setting
   the four `LINODE_OBJ_STORAGE_BUCKET`/`_ENDPOINT`/`_ACCESS_KEY`/`_SECRET_KEY` lines in `.env` by
   hand also works.)
4. Check it any time: `python instance_manager.py backup-config` shows whether Object Storage is
   configured and how the last backup went; `--test` re-runs the write/read/delete test.

No backfill step is needed — the very next stop or onboard for each instance starts backing it up
from that point on. An instance that isn't touched again after this is configured won't have an
Object Storage backup until it is. Run `backup` once right after configuring it to back up every
instance and group immediately.

### Scheduling whole-system backups with `backup`

The entire local registry is one plain SQLite file, `state/instances.db` — there's no database
server involved, so a consistent snapshot of it is a complete backup on top of the two layers
above. Rather than copying the file directly (a raw copy against a live, in-use database risks
capturing a torn/inconsistent snapshot), use the tool's own `backup` command, which takes a
genuinely consistent snapshot via SQLite's own backup mechanism and, if Object Storage is
configured, also re-syncs every currently-onboarded instance's own Object Storage record in the
same pass — closing the gap for any instance that hasn't individually been stopped or onboarded
in a while:

```bash
python instance_manager.py backup --backup-dir /path/to/backup
```

Give it `--backup-dir`, rely on Object Storage alone, or both — it refuses with a clear error if
neither is configured, since there'd be nothing to actually do. Run it on whatever schedule
matches your own risk tolerance via cron or a systemd timer:

```ini
# /etc/systemd/system/instance-scheduler-backup.service
[Unit]
Description=Instance Scheduler - whole-system backup

[Service]
Type=oneshot
WorkingDirectory=/opt/instance-scheduler
ExecStart=/opt/instance-scheduler/.venv/bin/python3 instance_manager.py backup --backup-dir /var/backups/instance-scheduler
```

```ini
# /etc/systemd/system/instance-scheduler-backup.timer
[Unit]
Description=Run the Instance Scheduler backup daily

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
systemctl daemon-reload
systemctl enable --now instance-scheduler-backup.timer
```

The command exits non-zero if anything didn't complete (a per-instance Object Storage re-sync
failure, either snapshot destination failing) — wire this into your own monitoring via the exit
code, not just by reading its log output. Also worth backing up, by whatever means you already
use for plain files: `state/known_hosts` (the tool's own SSH trust store — losing it just means
the next `start` for each instance needs its host key re-established, not lost data).

### Recovering from a lost or corrupted registry

If `state/instances.db` is deleted or genuinely corrupted (not a valid database file), the tool
detects this on its next attempted access and tells you exactly what to do. The short version:

```bash
# If the file exists but is corrupted, move it aside first (skip this step if it's simply gone):
mv state/instances.db state/instances.db.corrupted

# A fresh, empty database is created automatically the next time any command runs. Then:
python instance_manager.py rebuild
```

`rebuild` scans your account for the tags this tool writes onto every managed resource and
reconstructs the registry from them, then consults Object Storage (if configured) to fill in
anything tags alone can't hold — no dependency on the deleted file either way. An instance that
was **running** at the moment the database was lost always recovers completely, including its
network configuration and SSH access list, read fresh off the live instance. An instance that was
**stopped** at that moment recovers as fully as what's available: if its network configuration
was simple enough to tag-encode, or an Object Storage backup exists for it, or both, it comes
back fully usable — `stopped`, ready for a normal `start`, no manual step required. Only if
neither source had what was needed does it come back flagged `needs_manual_recovery`. Bring one
of those the rest of the way back:

1. Boot it once from its recorded OS volume and reserved IP (via Cloud Manager, or the raw
   Linode API — the tool itself won't do this step for you, since it has no live network
   configuration to boot it with).
2. `python instance_manager.py reset-host-key --ip <its reserved IP>` — a fresh boot presents a
   genuinely new SSH host key, which needs to be explicitly trusted once.
3. `python instance_manager.py onboard --name <name> --instance-id <the booted instance's ID> --force`
   — hands full management back to the tool, capturing everything live from the now-running
   instance.

If an instance keeps coming back `needs_manual_recovery` after a rebuild and you expected it to
recover fully, the most common cause is that Object Storage wasn't configured (or wasn't yet
backed up for that specific instance) at the time local state was lost — see "How recovery
actually works" above, and the Definitive Guide's Part 5.6 for the complete list of edge cases
and exactly what's at risk in each.

### Moving to a new host

If the deployment host itself is lost, set up a fresh one with `sudo ./install.sh recover`. Give it
the same API token and bucket; the SSH key and its passphrase come back on their own (the
passphrase from its recovery copy in your Linode account). It restores the newest database
snapshot — usually the one uploaded within a minute of the last change — and the trusted host
keys, runs `rebuild` for anything newer and `heal` to match the account (adopting an instance the
snapshot thinks is stopped), starts the services, and lists nodes left off their schedule for you
to approve. `--no-start` lets you review first, `--rebuild-only` skips the snapshot. Shut the old
host's poller down first: two pollers must never run at once. Full details, including what to
check afterwards: `DEPLOYMENT.md` §8.1.

Keep Object Storage configured: it holds the snapshots, the instance and group records, the host
keys and the encrypted SSH key. Everything else needed comes from your Linode account. With your age
public key set (`LIS_ENV_BACKUP_RECIPIENTS`), the bucket also holds your own encrypted copy of
`.env` and the SSH key; `sudo ./install.sh recover --env-from-backup --age-identity key.txt` then
asks only for the bucket settings.

The bucket also holds a copy of the installed release (`deployment/release/latest.tar.gz`: code,
built dashboard, `requirements.lock` and every dependency as a wheel), uploaded whenever the code
changes. If GitHub, PyPI or nodejs.org can't be reached, fetch it with any S3 tool using the bucket
keys, extract it, and run `sudo ./install.sh recover` from inside it:

```bash
aws s3 cp s3://<bucket>/deployment/release/latest.tar.gz . --endpoint-url https://<region>.linodeobjects.com
tar xzf latest.tar.gz && cd linode-instance-scheduler && sudo ./install.sh recover
```

`release-backup` uploads it now (`--force` even if unchanged, `--output FILE` for a local copy).

### Self-healing and missed schedules

The scheduler compares every node with the account every five minutes (`heal` does it on demand):
an instance holding a node's OS volume that the records don't point at is recorded as the node; a
record pointing at a deleted instance becomes stopped; a blank leftover from an unfinished start
(no disks, no volumes, 15+ minutes old) is deleted; a stale "in progress" mark is cleared. Nothing
that can hold data is deleted.

After an outage longer than the scheduler's one-hour catch-up window, nodes stay in whatever state
they were in. `schedule-catch-up` (and the dashboard's Instances page) lists them; `--apply` brings
them in line after you confirm.

### Alerts

With a webhook and/or email set up (the System backup page's Alerts form, `alerts-config`, or
`LIS_ALERT_WEBHOOK_URL` / `LIS_ALERT_EMAIL_TO` + SMTP settings in `.env`), an alert goes out when the scheduler hasn't checked in for 10 minutes (sent by the API service), backups fail,
the deployment SSH key isn't stored in every place, or nodes stay off their schedule — again every
6 hours while it lasts, and once when resolved. Also covered: a failed daily restore test, the API
token rejected or expiring within 14 days, the database disk nearly full, Shutdown Watchdog off on
this host, this host standing by because another scheduler holds the lease, a pending deletion
overdue or failing, bucket versioning that can't be turned on, a failed or missing recovery drill
(when drills are on), and the host clock more than 60 seconds off Linode's API time or not
time-synchronized (`timedatectl set-ntp true`). `alerts-test`
sends a test message; `alerts-check` prints what needs attention and exits 1 if anything, for your
own monitoring. For the whole host being gone: Shutdown Watchdog (`host-watchdog --enable`, done by
`install.sh`) boots it again after a crash, and `LIS_HEARTBEAT_URL` lets an uptime monitor notice
the pings stopping.

### One active scheduler, and restore testing

With Object Storage the active scheduler holds a lease in the bucket (renewed every 30 seconds,
expired after 3 minutes). Another scheduler that finds it fresh stands by and alerts; `recover`
takes the lease over at once, and `scheduler-lease --take-over` moves it by hand. Every day the
newest snapshot is downloaded and checked and the key copies decrypted (`backup-verify` on demand);
a database that fails SQLite's integrity check is never uploaded. An upgrade with `install.sh
install` takes a snapshot first, in `backups/pre-upgrade-<time>/`.

A standby host refuses changes: its API answers `409` to anything but reads (and the take-over),
and the CLI refuses changing commands, naming the active host. Make changes there, or take over if
it's gone. `LIS_ALLOW_STANDBY_CHANGES=1` overrides. While the bucket can't be reached, the
scheduler that was active carries on and a host that wasn't active stays standby;
`scheduler-lease --take-over` overrides.

### Recovery drills

`recovery-drill` proves a new host could take over: it creates a throwaway `g6-nanode-1` (tagged
`lis-recovery-drill`; `--region` to choose), installs the release copy from the bucket with a
minimal `.env`, and runs `install.sh recover --drill`, which restores the latest snapshot and the
SSH key and then checks every node's volumes and reserved IP exist and running nodes answer over SSH
with the restored key. The drill host is read-only — no lease, no rebuild or heal, no uploads, no
services — and the VM is always deleted (`--keep-vm` to inspect it, then delete it yourself). About
10–15 minutes of a nanode per drill. `LIS_RECOVERY_DRILL_DAYS=7` runs one weekly from the
scheduler; a failed or missing drill is alerted.

### Bucket versioning

Versioning is turned on for the backup bucket when backups are configured (and again by the health
check if it's found off), with a lifecycle rule expiring old versions after 30 days; other
lifecycle rules are kept. The access key needs permission to change bucket settings, otherwise an
alert says so.

```bash
python instance_manager.py bucket-protection                       # status (--enable to turn it on)
python instance_manager.py bucket-protection --key full-backups/instances-continuous.db       # versions
python instance_manager.py bucket-protection --key full-backups/instances-continuous.db --restore-version <VERSION_ID>
```

### Restoring one item from a snapshot

To undo a mistaken change to one node's schedule or hooks, or to one group, without restoring the
whole database:

```bash
python instance_manager.py restore-item schedule --name web-1 --show   # look first
python instance_manager.py restore-item schedule --name web-1          # from the newest snapshot
python instance_manager.py restore-item group --name dev --with-members --snapshot <key>
```

`--from-file PATH` uses a local snapshot. The dashboard's System backup page has **Restore one item
from a snapshot**, which shows the snapshot's version next to the current one first.

### Offboarding and pending deletions

`offboard` and `backup-delete` hold their resources for a grace period (`LIS_DELETION_GRACE_DAYS`,
default 7) before deleting anything. The resources keep all their tags plus
`lis-pending-delete:<date>` / `lis-pending-untag:<date>` and `lis-pending-of:<name>`; when due, the
active scheduler carries out the deletion, re-checking each resource (anything no longer marked,
attached or assigned, or claimed by another name is left alone). `--now` on either command skips the
grace period.

```bash
python instance_manager.py pending-deletions                 # what's waiting (--all for history, --json)
python instance_manager.py pending-deletions --recover 4     # back, stopped, with schedule, group, hooks, mode, holidays
python instance_manager.py pending-deletions --delete-now 4  # delete now (type the name, or --yes)
```

The dashboard's **Pending deletion** page does the same. `onboard` refuses a name that's pending
deletion. After a lost database, `rebuild` restores a pending node as a pending deletion, never as
a live node.

### Resolving a stuck per-instance lock

Every mutating operation on a given instance takes a lock for its duration. If a process is
killed mid-operation (a crashed process, a killed SSH session), the lock is released
automatically the moment that process exits — this is an OS-level file lock, not something the
tool has to remember to clean up. If you still see a `Configuration error: ... already
operating on '<name>'` refusal with nothing actually running against that instance:

```bash
python instance_manager.py clear-lock --name <name>
```

The scheduler clears a mark like this by itself within five minutes, and the instance's dashboard
page has a **Clear lock** button while it's set. All three refuse while a start or stop really is
running.

### Resolving an orphaned migration volume

A Path B migration (see the Definitive Guide, Part II.2) that's abandoned partway through can
leave its destination volume behind, still billing, with no instance ever using it:

To cancel a migration that hasn't been finished — the original boots from its own disk again and
the destination volume is deleted — use `migrate-abandon --name <name>`, or **Cancel this
migration** on the dashboard's migration page.

```bash
python instance_manager.py migrate-orphans              # list every orphaned migration volume found
python instance_manager.py migrate-orphans --cleanup     # permanently delete them, after re-verifying each one is genuinely orphaned
```

### Rolling back a migration

A migration started with `migrate-start --backup` keeps a powered-off clone of the original
instance and its volumes. To put the original back:

```bash
python instance_manager.py backup-list                   # what's kept, since when
python instance_manager.py stop --name web-1             # only if it's onboarded and running
python instance_manager.py rollback --name web-1         # original addresses, label and volumes back
python instance_manager.py backup-delete --name web-1    # once you're sure; deleted after the grace period (--now for at once)
```

`rollback` refuses while the node is running as a managed instance, or when an original VPC
address has since been taken by another instance. Its migrated volumes are kept; delete them when
no longer needed.

## Security operations

### Setting up dashboard login ("Login with Linode")

1. In Cloud Manager: **Profile → OAuth Apps → Create an App.**
2. Set the callback URL to `https://<your-deployment-host>/oauth/callback` — this must exactly
   match `LINODE_OAUTH_REDIRECT_URI` in your `.env`.
3. Copy the generated Client ID and Client Secret into `LINODE_OAUTH_CLIENT_ID` /
   `LINODE_OAUTH_CLIENT_SECRET`.
4. Restart the API server (`systemctl restart instance-scheduler-api`).

This is entirely separate from `LINODE_API_TOKEN` — the OAuth login only ever identifies who's
calling; it's never used to talk to Linode directly. See the Definitive Guide's Security
section for the full reasoning.

### Rotating the SSH trust for an instance

Deliberate key rotation — after infrastructure changes, or simply as routine hygiene — always
goes through the tool, never by hand-editing `state/known_hosts`:

```bash
python instance_manager.py reset-host-key --name <name>
# or, for an instance not yet under management, by address:
python instance_manager.py reset-host-key --ip <address>
```

A steady-state `start` that encounters an *unexpected* host-key change (one you didn't just
deliberately trigger) is refused outright as a `SECURITY WARNING` — see Troubleshooting below.

### Reviewing hook changes

Pre-stop hooks and post-start checks run as `root` on your instances, so anyone who can set one —
from the CLI, the API, or the dashboard — can run a root command on that instance. Treat dashboard
access accordingly. Every hook change is recorded with who made it, when, and the full new command:

```bash
python instance_manager.py hooks-show --name <name>      # what applies now, and where it comes from
```

The dashboard's Hooks card on each instance lists recent hook changes and runs; the API serves the
same record at `GET /instances/{name}/hook-events`. An uploaded script is identified in that
history by its size and a SHA-256 fingerprint, so you can tell exactly which version ran.

With Object Storage configured, hooks are stored in your bucket under a `hooks/` prefix, named by a
fingerprint of their own content and never overwritten. Treat write access to that bucket like
shell access to your instances. `rebuild` refuses to restore any hook whose content no longer
matches the fingerprint its tag records, so a modified object is detected rather than run.

### Rotating credentials

`rotate-credentials` replaces a credential in place, with no restart:

```bash
python instance_manager.py rotate-credentials api-token      # new token, same scopes and lifetime
python instance_manager.py rotate-credentials bucket-key     # new Object Storage key, same bucket access
python instance_manager.py rotate-credentials oauth-secret   # reset the dashboard login's client secret
python instance_manager.py rotate-credentials ssh-key        # new deployment SSH key on every node
python instance_manager.py rotate-credentials ssh-key --continue   # move nodes not moved yet
```

- `api-token`: the new token is written to `.env` and picked up by the running services; the old
  one is revoked only once the scheduler has switched to the new one.
- `bucket-key`: the new key is tested against the bucket and written to `.env`, then the old key is
  deleted.
- `oauth-secret`: the new secret is written to `.env`; the old one stops working at once.
- `ssh-key`: the old key stays usable (encrypted in the database and bucket, and offered by every
  SSH connection via `LINODE_SSH_PREVIOUS_KEY_PATH`) until every node trusts the new key. Running
  nodes move at once — new key added, a login with only the new key proven, old key removed;
  stopped nodes after their next start, which the scheduler handles. After a restore mid-rotation,
  `ssh-key-restore` restores the previous key too. If you use the assisted migration copy, update
  the Lish key in your Linode profile to the new public key.

Over the API, `POST /system/credentials/rotate` does the same (an admin-level action). You can still rotate in Cloud Manager and edit `.env` by hand; the running services read
the changed values without a restart.

## Upgrading

1. Read the release notes for the version you're upgrading to.
2. Back up `state/instances.db` first (see above) — cheap insurance before any upgrade.
3. Pull the new release, reinstall dependencies (`pip install --require-hashes -r requirements.lock`, and
   `npm ci && npm run build` under `web/` if you run the dashboard).
4. Restart both services:
   ```bash
   systemctl restart instance-scheduler-poll instance-scheduler-api
   ```
5. Confirm both came back up clean:
   ```bash
   systemctl status instance-scheduler-poll instance-scheduler-api   # both "active (running)"
   python instance_manager.py list                                   # registry still reads correctly
   ```

The database schema is migrated automatically and idempotently on first connection after an
upgrade — no separate migration step to run by hand.

On Kubernetes/LKE, the equivalent of steps 3–5 above is rebuilding and pushing a new image tag,
then re-running `deploy-lke.sh --image <new-tag>` — see DEPLOYMENT-LKE.md's own Upgrading
section for the exact commands. Step 2 (back up first) and the schema-migration note both still
apply unchanged.

## Monitoring & health checks

There's no separate metrics pipeline — the signal to watch is the tool's own state and logs.
The dashboard's **Activity** page shows all of the below in one place, with a banner that turns
red when the scheduler stops checking in; **Logs** tails the scheduler, API and backup output and
each assisted migration copy's Rescue Mode console session (also on disk under `state/logs/`,
`lish-<name>.log` for a console session, removed automatically after `ACTIVITY_LOG_RETENTION_DAYS`
days without new output, or sooner with **Delete session** or `console-session-delete --name X`);
and **Console** runs any of the commands below from the browser.

- **Are both processes actually running?** `systemctl is-active instance-scheduler-poll
  instance-scheduler-api`.
- **Is the fleet in the state you expect?** `python instance_manager.py list` — cross-check any
  instance that's `stopped` when you didn't expect it against `history --name <name>` (a
  scheduled stop shows `triggered_by=schedule` in the audit trail; if you don't see that, it
  didn't come from this tool's own scheduling).
- **Did a scheduled action actually fire when it should have?** `python instance_manager.py
  history --name <name> --limit 10` shows the real recorded outcome, not just whether the
  poller is running.
- **Did hooks run and pass?** `status --name <name>` names the instance's hooks and flags a
  post-start check whose latest run failed; the dashboard's Hooks card shows each recent run's
  result and output. A failed pre-stop hook or post-start check also makes `poll --once` exit
  non-zero, the same as any other failed scheduled action.
- **Any instance stuck `needs_manual_recovery`?** `list` shows this status plainly — see
  "Recovering from a lost or corrupted registry" above for the fix.
- **Is anything at risk?** `alerts-check` (exit 1 if so) — the same conditions alerts are sent
  for; the dashboard's System backup page shows them too.

## Rolling out to production — best practices checklist

The same practices the Definitive Guide explains in Part IX.5, as a checklist with the commands
for each step. Work through it top to bottom: plan, deploy, pilot on non-production instances for
a week, rehearse at your real scale, then onboard production in waves.

### 1. Before you deploy

- [ ] **Inventory the fleet** — region, plan, networking (public / VPC / VLAN), local disk or
      Block Storage boot volume, data volumes. Plan maintenance windows for every instance on
      local disk: it needs a one-time migration with downtime (`migrate-start` / `migrate-resume`)
      before it can be onboarded.
- [ ] **cloud-init with the Akamai datasource** on every instance's image (23.3.1+, or an older
      build with the datasource backported). `migrate-start` checks this automatically for
      instances being migrated; images with no cloud-init at all aren't supported.
- [ ] **A supported OS image** — see the Definitive Guide's list of tested distributions.
- [ ] **Schedules agreed with application owners**, including each group's timezone. Prefer one
      group per schedule; keep individual schedules for genuine exceptions.
- [ ] **Hooks decided** — which instances need a pre-stop hook (clean shutdown of a database or
      queue) or a post-start check (application readiness).
- [ ] **Account limits checked** with Linode support against your peak: every managed instance
      running at once, plus every OS/data volume and reserved IP, which exist permanently.
- [ ] **Regional capacity** for your plans at your peak start time, for fleets of 100+.
- [ ] **Instances with only VPC/VLAN interfaces**: the scheduler host must reach them over SSH
      at their private address — run it inside the same VPC (or on the same VLAN). For VPC 1:1
      NAT (either networking model), reserve the NAT address if its public address must stay the
      same.

### 2. Deploy

- [ ] **One deployment per Linode account**, on a small dedicated VM in the same region as the
      fleet (or one LKE deployment — DEPLOYMENT-LKE.md). Exactly one scheduler process.
- [ ] **Dedicated API token** with only the scopes in the Environment configuration reference
      above, used by nothing else.
- [ ] **One deployment SSH keypair**, private key only on the scheduler host
      (`LINODE_SSH_KEY_PATH`), public key added to each instance. Never hand the tool root
      passwords.
- [ ] **SSH allowed** from the scheduler host to every managed instance (Cloud Firewall rules,
      VPC routing).
- [ ] **Scheduler and API server under systemd** (sections above), both enabled to start at boot:
      `systemctl enable --now instance-scheduler-poll instance-scheduler-api`.
- [ ] **Dashboard behind HTTPS** (reverse proxy) with "Login with Linode" configured — never
      exposed on plain HTTP.
- [ ] **Object Storage backup configured** (bucket-scoped key) and a scheduled whole-system
      `backup` (see "Scheduling whole-system backups" above). Confirm one run:
      `python instance_manager.py backup --backup-dir /var/backups/instance-scheduler`.

### 3. Onboard and schedule

- [ ] **Non-production first, in waves.** For each instance:
      `onboard --name <name> --instance-id <id>`, then one manual cycle —
      `stop --name <name>` and `start --name <name>` — and confirm the application is healthy
      before adding a schedule.
- [ ] **Groups**: `group-create`, `group-schedule-set --group-name <group> --timezone <tz> ...`,
      `group-add --name <name> --group-name <group>`. A window crossing midnight is one
      overnight rule (a stop time earlier than the start time stops the next morning).
- [ ] **Manual-only nodes**: for anything that should never follow a schedule,
      `set-mode --name <name> --manual` (not in a group with a schedule).
- [ ] **Scripts and pipelines**: an API token per consumer with the narrowest scopes and limits
      (`api-token-create --name <name> --scopes read,operate --groups <group> --expires-days 90`, or single operations such as `--scopes instances:stop` for a job that should only stop nodes; `api-token-scopes` lists them);
      scripts check `start`/`stop` exit codes (3 = busy, retry; 5 = start order not satisfied) or
      use `?wait=true` over the API. Review `api-token-list` (last used) periodically and revoke
      what's unused.
- [ ] **Start order**: where a group needs others up first (applications and their database and
      cache), `group-depends --group-name <app-group> --on <db-group>,<cache-group>`, and give
      those groups a post-start readiness check so "up" means accepting connections. Configure
      Object Storage (or keep a member in every depended-on group) so the full order survives a
      database loss.
- [ ] **Hooks**: `hooks-set --name <name>` (or `--group-name <group>`) with `--pre-stop` /
      `--post-start` commands or `--pre-stop-script` / `--post-start-script` uploads. Verify with
      `hooks-run`.
- [ ] **Large fleets (100+)**: keep `poll --max-parallel` at its default of 10 to start with, and
      stagger group start times by 5–10 minutes.
- [ ] **Re-key monitoring and billing reports** off the Linode instance ID (it changes on every
      start) onto the instance label, tags, or IP address (these stay the same).
- [ ] **Maintenance windows**: disable a group's schedule rather than working around it —
      `group-schedule-set ... --disabled`, then re-run without `--disabled` afterwards.

### 4. Monitor (daily during the pilot, weekly after)

- [ ] `systemctl is-active instance-scheduler-poll instance-scheduler-api`.
- [ ] `python instance_manager.py list` — every instance in the state its schedule says.
- [ ] `history --name <name>` for any instance that surprised you; failed starts/stops are
      retried for an hour automatically, but a persistent one needs a person.
- [ ] Scheduler output for "insufficient capacity" or 429 (rate limit) messages — see
      Troubleshooting.
- [ ] Instances shown as `stopped` are really gone in Cloud Manager (billing actually stopped).
- [ ] Each group's configured versus actual savings on the dashboard — a widening gap means the
      schedule is being routinely overridden.

### 5. Go / no-go for production

| Stage | Do | Move on when |
|---|---|---|
| Pilot, one week | Non-production instances on their real schedules, with hooks where needed. One recovery drill on a copy of the deployment: restore from a `backup` snapshot, or `rebuild` from scratch. | Seven days with no unexplained failed start/stop; the drill recovers every instance, schedule, group, and hook; application owners confirm their services come back after each daily cycle. |
| Scale rehearsal | A real start and stop of as many instances as production will have, in the production region and plans, timed. | The morning start finishes within your tolerance, with nothing still failing after the retry window. |
| Production, in waves | Onboard 20–30 production instances per wave, a few days apart, watching each wave's first cycles. | Each wave runs cleanly before the next. |
| Steady state | Upgrade only to tagged releases, non-production deployment first, `backup` immediately before (see Upgrading). | — |

## Troubleshooting

| Symptom | Most likely cause | What to check / do |
|---|---|---|
| Instance shows `unreachable` | Created successfully but the network reachability check hasn't yet succeeded, or the instance was powered off out-of-band (e.g. directly via Cloud Manager, not through this tool) | Run `start` again — the tool detects a confirmed-gone or genuinely powered-off instance and recovers automatically; a second `start` completes the recovery rather than creating a duplicate |
| A node never starts or stops on its schedule, and `poll` shows nothing for it | It's manual-only | `list` shows `[manual-only]`; `set-mode --name <name> --auto` to let schedules govern it again |
| An API call returns 403 with an API token | The token lacks the scope this route needs, or the node/group is outside its limits | `api-token-list` for the token's scopes and limits; create a new token with the right ones (tokens can't be edited) |
| An API call returns 401 with an API token | The token was revoked, has expired, or was mistyped | `api-token-list`; create a new one if needed |
| Nodes listed as "not in the state their schedule wants" | The scheduler was down for longer than its one-hour catch-up window | Review them on the Instances page or with `schedule-catch-up`, then apply |
| An alert says the deployment SSH key isn't fully protected | No passphrase in `.env`, Object Storage unreachable, or the API token lacks StackScripts read/write | `ssh-key-protect` shows which copy is missing and why |
| A scheduled action didn't fire | The scheduler process (`poll`) wasn't running for longer than the catch-up window (default one hour), the instance's effective schedule isn't what you expect, or a manual start/stop after the scheduled time took precedence (by design) | `systemctl is-active instance-scheduler-poll`; `python instance_manager.py schedule-show --name <name>`; `history --name <name>` shows whether a manual action came after the scheduled time; confirm whether an individual schedule is silently overriding a group's (Part III.1 of the Definitive Guide) |
| A scheduled start keeps failing (e.g. "insufficient capacity") | The region is temporarily short of that plan | Nothing to do while it's inside the catch-up window — `poll` retries it every cycle, and each failure is in `history` and makes `poll --once` exit non-zero. If it persists, start it on another plan or spread the fleet's start times |
| `poll` output shows many "429 Too Many Requests" responses, or a large fleet's starts run slower than expected | Parallel workers are hitting the Linode API rate limit; every worker is pausing for the time the API asks | Usually nothing — requests resume on their own and anything that still fails is retried within the catch-up window. If it's persistent, lower `poll --max-parallel` (default 10) or `LINODE_API_MAX_REQUESTS_PER_SECOND` (default 10), and check nothing else is using the same API token heavily |
| `SECURITY WARNING` on `start` | The instance is presenting a different SSH host key than the one on record — could be a genuine security event, or simply an infrastructure change you made yourself (a rebuild via Cloud Manager, a disk swap) | Independently confirm the new key is legitimate (Lish console) before running `reset-host-key` — never bypass this check casually |
| `migrate-start`'s copy command prints `Could not identify the disks unambiguously` | Two devices match the original disk's size, or the new volume isn't attached in Rescue Mode | Nothing was copied. Check the listed devices (`lsblk`): the original disk is the size `migrate-start` reported, the new volume is the new size. If the volume is missing, run `migrate-start --force` |
| The copy ends with `COPY_FAILED` | The block copy hit an error | Don't run `migrate-resume`. Run `migrate-start --force` to start over with a fresh volume |
| `migrate-copy` says the key is not one of the profile's Lish keys | The deployment's public key isn't registered for Lish, or the profile allows only password login | Add the key in Cloud Manager: Profile → LISH Console Settings, allow keys; `migrate-copy --check` confirms |
| `onboard` refuses with "still on local disk" | The instance hasn't been migrated onto a Block Storage volume yet | Run the Path B migration (`migrate-start` / `migrate-resume`) first — see the Definitive Guide, Part II.2 |
| `onboard` refuses with "was offboarded and is pending deletion" | That name was offboarded and is inside its grace period | `pending-deletions` — recover it, delete it now, or use another name |
| Any change on the dashboard or API returns 409 "This host is a standby", or the CLI refuses with "this host is a standby" | Another scheduler holds the lease | Make the change on the active host. If it's gone for good, `scheduler-lease --take-over` (or System backup → Active scheduler) here first |
| An alert about the clock | The host clock is more than 60 seconds off Linode's API time, or not time-synchronized | `timedatectl set-ntp true`; check `timedatectl status` |
| `onboard` refuses with "IP not reserved" | The instance's public IP is still an ordinary, ephemeral one | Reserve it first via Cloud Manager, or use the one-time in-flow "reserve and retry" option on the dashboard's onboarding screen |
| A stop/start command refuses with "already operating on" | A per-instance lock is held — normally released automatically when the holding process exits | Confirm nothing is actually mid-operation on that instance, then `clear-lock --name <name>` |
| `rebuild` recovered an instance as `needs_manual_recovery` instead of `stopped` | Neither tags nor Object Storage had a complete-enough record for it at the moment of loss — most commonly, Object Storage wasn't configured yet, or that specific instance hadn't been stopped/onboarded since it was | See "Recovering from a lost or corrupted registry" above for the manual-boot recovery steps, and Part 5.6 of the Definitive Guide for the full edge-case table |
| A group's members show "waiting on dependency" or "waiting on dependents" and never act | The group it depends on isn't fully running and ready (or, on the way down, a dependent group isn't fully stopped) — often a member that failed to start, is `unreachable`, or whose post-start check failed | `group-show --group-name <group>` to see the dependency; `list` for each member's status; `history --name <member>` and `hooks-run --name <member> --post-start` for a failing readiness check. Fix the blocking member, or `group-depends --group-name <group> --remove <other-group>` (or `--clear`) to remove the order |
| Group's timezone or schedule doesn't seem to apply | An individual schedule exists and is silently overriding the group entirely (working as designed, not a bug) | `schedule-show --name <name>` to check for an individual schedule; clear it if the group should govern instead |
| A stop fails with "pre-stop hook ... Stop aborted" and the instance stays running | The instance's pre-stop hook exited non-zero, timed out, or couldn't be reached over SSH, and its failure policy is `abort` (the default) — by design, nothing was shut down or deleted | Read the hook output printed with the error (also in the Hooks card and `hook-events`), fix the cause, and stop again. To stop without the hook, use `stop --skip-hooks` (dashboard: "Stop anyway, without the pre-stop hook"). A manual-override auto-stop retries the stop on every poll tick until it succeeds |
| A start reports "post-start check still failing" | The instance started and is running, but its readiness check never passed within its timeout | The instance is deliberately left running. Check the service on the instance, then re-run the check with `hooks-run --name <name> --post-start`. If the check needs longer on a cold boot, raise `--post-start-timeout` |
| A configured schedule's savings percentage looks wrong | The schedule is disabled, or a manual-override auto-revert cycle is skewing actual vs. scheduled uptime | Check `schedule-show`'s `enabled` field; compare scheduled vs. actual savings — a persistent gap usually means overrides are happening more than expected |

## Reference

- `INSTANCE-SCHEDULER-DEFINITIVE-GUIDE.html` — full architectural reference: how the delete/
  recreate cycle works, the two onboarding paths, scheduling and override precedence, the REST
  API, security model, and deployment model.
- `python instance_manager.py <command> --help` — authoritative, always-current flag reference
  for any command in this guide.
