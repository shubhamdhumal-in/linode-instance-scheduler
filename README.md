# Redis / TiDB Standby Automation — Setup & Usage Guide

This guide walks you through everything needed to use this automation, end to end: building a
node the normal way, pulling this tool from GitHub, configuring it, migrating an existing node
onto the storage layout the automation requires, and running it day to day.

It assumes no prior familiarity with this tool. If you follow it top to bottom on a real node,
you'll end with a working `start`/`stop` workflow for that node.

---

## 1. What this is, and why it exists

On AWS, a common pattern for Redis failover standbys and TiDB replica pools is: keep several
instances built and ready, but **stopped**, until you actually need one. Stopping an EC2
instance is free — you only pay for the storage. When you need a standby, you start it and
repoint DNS (or, for TiDB, bring a replica online), rather than debugging a live problem under
pressure.

**That exact pattern does not work on Linode as-is**, because of one billing difference:

> A *stopped* Linode instance bills at the same rate as a running one. Powering it off does not
> stop charges. The only thing that actually stops compute billing is fully **deleting** the
> instance.

So a literal "stop" isn't the right tool here. This automation instead **deletes the instance
when you don't need it, and recreates it the moment you do** — but it's built so that from your
side, it behaves exactly like stop/start:

- The **public IP never changes** across cycles (it's reserved to your account, not tied to the
  instance's lifecycle) — no DNS or allow-list updates needed.
- **Data is never lost** — it lives on a separate Block Storage volume, independent of the
  instance, and survives every delete/recreate cycle.
- The **SSH host key stays stable** — no "remote host identity changed" warnings when you
  reconnect after a recreate.
- **Label, tags, plan/size, firewall, and other instance settings** are all captured and
  reapplied automatically, so the recreated instance looks identical in Cloud Manager and in
  your cost reports — not like a brand-new resource.

The only real difference from AWS: while "stopped," you pay a small ongoing fee for the Block
Storage volume(s) and for the reserved IP sitting idle — both far cheaper than running compute
24/7, but not literally free. See [§9, Costs](#9-costs) for the numbers.

### Use cases this is built for

- **Redis failover standbys.** You keep one or more pre-configured Redis nodes ready but not
  running. When your primary misbehaves, you start a standby, point your application at it, and
  investigate the original at your leisure — rather than firefighting a live node under load.
- **TiDB replica pool.** You keep a pool of pre-configured replica nodes ready. When you need
  more read capacity, you bring one online; when load drops, you take it back down.

Both are the same underlying pattern: a named, pre-configured node that's brought up and torn
down on demand, always coming back identical. The commands in this guide are the same for
either.

---

## 2. Prerequisites

Before you start, make sure you have:

- **A Linode account** with permission to create instances, volumes, and reserved IPs.
- **A Linode Personal Access Token** — Cloud Manager → Profile (top right) → API Tokens →
  "Create a Personal Access Token." Scopes needed: **Linodes** (Read/Write), **Volumes**
  (Read/Write), **IPs** (Read/Write). Add **VPCs** (Read) too if any of your instances use a VPC
  interface. Account (Read Only) is enough to satisfy the tool's own startup check.
- **Access to this GitHub repository** (you should already have this).
- **A dedicated SSH key pair** for this automation to use. Don't reuse a personal key you use for
  other things — generate one specifically for this tool, so its access is clearly scoped and
  easy to revoke independently later. Generate it at the exact path the tool defaults to, so you
  don't need to add a `--ssh-key` flag to every command in this guide (the default is
  `~/.ssh/linode_spike_key` — a name left over from this project's prototype phase, harmless to
  use as-is; every command below also accepts `--ssh-key <path>` if you'd rather point it
  somewhere else):
  ```
  ssh-keygen -t ed25519 -f ~/.ssh/linode_spike_key -C "linode-instance-scheduler"
  ```
  You'll add the **public** half (`~/.ssh/linode_spike_key.pub`) to every node you want this
  tool to manage.
- **Nodes on a supported OS image.** Every Linode distribution image with `cloud-init` and
  Akamai's datasource works: Ubuntu 22.04/24.04/26.04, Debian 12/13, Kali, Rocky Linux 8/9/10,
  AlmaLinux 8/9/10, CentOS Stream 9/10, Fedora 43/44, openSUSE Leap 16.0, Alpine 3.21/3.24, Arch
  and Gentoo. Slackware isn't supported (its image has no `cloud-init`). The tool re-applies each
  node's network settings in whatever format that OS uses (systemd-networkd, NetworkManager,
  wicked, `/etc/network/interfaces` or netifrc) every time it recreates the node.
- **Python 3.10 or newer** and **git** installed on the machine you'll run this from — this
  should be a **centralized, persistent server** (a dedicated admin box or bastion host), not
  someone's personal laptop. See §4 for why.

---

## 3. Building a node the normal way

This section is exactly what you'd do today, with no automation involved — building a Redis
standby or TiDB replica node from scratch. If you already have existing nodes running Redis or
TiDB, skip to [§4](#4-installing-and-configuring-the-automation) and come back to
[§6](#6-one-time-migration-onto-block-storage-path-b) for what's different about bringing an
*existing* node under this tool's management.

### 3.1 Create the Linode instance

In Cloud Manager (or via the API/CLI, if you prefer): create a Linode the normal way — pick a
region, a plan sized for your workload, and a Linux image (Ubuntu 24.04 LTS is what this
automation has been validated against; other recent distros should work the same way, but
haven't been tested here). Nothing special is required at this stage — this is an entirely
ordinary Linode, local disk and all.

Add the automation's SSH public key (from [§2](#2-prerequisites)) to the instance at creation
time (Cloud Manager's "SSH Keys" field), or add it afterward:

```
ssh-copy-id -i ~/.ssh/linode_spike_key.pub root@<instance-ip>
```

**This step matters**: the automation needs to be able to SSH into your node using this key —
both to inspect it during onboarding, and because whatever's in `authorized_keys` at onboarding
time is exactly what gets carried onto every recreated instance afterward. If this key isn't
present, onboarding will fail.

### 3.2 Install and configure Redis

```
apt update && apt install -y redis-server
```

Edit `/etc/redis/redis.conf` for your actual requirements — at minimum, review:

- `bind` — restrict this to the interfaces/IPs that should actually be able to reach it (don't
  leave it open to `0.0.0.0` unless you have a firewall doing that job instead).
- `requirepass` — set a real password; don't run Redis unauthenticated.
- **Persistence** — this is the setting that matters most for this automation. Decide between
  RDB snapshots (`save` directives) and/or AOF (`appendonly yes`), and make sure whichever you
  choose is actually **writing to the data volume you'll attach in the next step**, not to local
  disk. Local disk does not survive a delete/recreate cycle; the data volume does.

Restart Redis after any config change: `systemctl restart redis-server`.

### 3.3 (TiDB) Install and configure TiDB / TiKV / PD

TiDB's own install tooling (`tiup`) and cluster topology are outside the scope of this guide —
follow TiDB's own documentation for provisioning your cluster's components. The one thing that
matters for *this* automation is the same as for Redis: **make sure each node's actual data
directory is on the Block Storage volume from the next step, not local disk.** Everything else
about how you configure and operate TiDB is unchanged by this tool.

### 3.4 Attach a Block Storage volume for your data

In Cloud Manager: create a volume sized for your data, in the **same region** as the instance,
and attach it. Then, on the instance:

```
# Confirm the device path Linode assigned it (usually /dev/sdc on a fresh image-deployed instance)
lsblk

# Format it (only once, on a brand-new volume — skip this if it already has a filesystem)
mkfs.ext4 /dev/sdc

# Mount it
mkdir -p /mnt/data
mount /dev/sdc /mnt/data
```

**Add it to `/etc/fstab` so it remounts automatically on boot — using the stable by-id path, with
`nofail`:**

```
ls -la /dev/disk/by-id/ | grep sdc     # find the scsi-0Linode_Volume_<label> entry for your volume
echo '/dev/disk/by-id/scsi-0Linode_Volume_<your-volume-label>  /mnt/data  ext4  defaults,nofail  0  2' >> /etc/fstab
```

**Why `nofail` matters, specifically**: without it, a data volume that isn't attached yet at the
exact moment the boot sequence checks `/etc/fstab` can hang the entire boot — which would lock
you out of SSH on every single recreate. `nofail` tells the boot process to continue even if
this particular mount isn't ready yet, and it always catches up correctly once the volume is
actually attached (which happens before the OS boots on every recreate this tool performs).

Point Redis's persistence files (or TiDB's data directory) at `/mnt/data`, and restart the
service so it's actually using it.

At the end of this section, you have a completely ordinary, real, running node — Redis or TiDB,
your normal config, a data volume with real data on it, reachable at whatever public IP Linode
assigned it. Nothing about it is special yet; this is exactly the starting point the automation
expects.

---

## 4. Installing and configuring the automation

This section covers the default deployment: a single, persistent VM. If you already run a
Linode Kubernetes Engine (LKE) cluster and would rather run this tool there instead, see
`DEPLOYMENT-LKE.md` instead of this section — same tool, same commands, just packaged as a
container.

**Run this from a centralized server, not your local laptop.** This tool keeps track of every
node it manages in a local registry file on whatever machine you run it from (see §8's `rebuild`
section for the full reasoning). If you run it from your own laptop, that registry only exists
there — a colleague running the same commands from *their* laptop would have no idea those nodes
are already onboarded, and you'd end up with two disconnected, out-of-sync views of the same
infrastructure. Pick one small, persistent machine (a dedicated admin box, a bastion host,
whatever you already use for this kind of operational tooling) that your whole team runs this
from, so there's exactly one source of truth for what's onboarded and what state it's in. The
`rebuild` command (§8) exists as a safety net if that machine is ever lost — it isn't a
substitute for having a single, agreed-upon place to run this from in the first place.

Clone the repository — that's where the actual tool lives:

```
git clone https://github.com/sandipgangdhar/linode-instance-scheduler.git
cd linode-instance-scheduler
```

Set up a Python virtual environment and install dependencies:

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create your local config file (this file is git-ignored — it never gets committed, and you
should never paste your token into chat, a commit, or anywhere else that isn't this file):

```
cp .env.example .env
```

Open `.env` and fill in the token from [§2](#2-prerequisites):

```
LINODE_API_TOKEN=your-real-token-here
```

Verify the Python environment and dependencies are set up correctly:

```
python instance_manager.py list
```

This should print `No instances onboarded yet.` (or a list, if you already have some) — it
confirms your local setup is working, though it doesn't check the API token yet (`list` is a
purely local command, deliberately, so it works even before you've configured anything).

To verify the token itself, run:

```
python -c "import linode_engine as e; c = e.build_client(e.load_token()); e.auth_check(c); print('token OK')"
```

If this prints `token OK`, everything is configured correctly. If it errors, double-check the
token's scopes and that `.env` is in the repo root, next to `instance_manager.py`.

---

## 5. The commands, what each one is for

Every command below is run from the repository root, with the virtual environment active
(`source .venv/bin/activate` if you started a new terminal session).

| Command | What it does |
|---|---|
| `migrate-start` | **One-time, only for a node whose OS is still on local disk.** Starts moving it onto Block Storage — creates the destination volume and boots the node into Rescue Mode. Prints one manual command for you to run. |
| `console-session-delete` | Delete a migration's Rescue Mode console session log (removed automatically after the retention period). See §6.6. |
| `rollback` / `backup-list` / `backup-delete` | Put the original system back from the backup `migrate-start --backup` kept, list backups, or delete one when you no longer need it. See §6.5. |
| `migrate-resume` | Finishes what `migrate-start` began, after you've run that one manual command. Boots the node from its new Block Storage volume and reserves its IP. |
| `migrate-copy` | Optional: runs the copy step for you in the Rescue Mode console, then `migrate-resume`. Only when a pre-migration backup is kept (§6.6). |
| `migrate-orphans` | Lists (or `--cleanup`s) destination volumes left behind by a `migrate-start --force` restart — see §6.4. You'll rarely need this. |
| `onboard` | Registers an already-running, already volume-based node with this tool by name. Pure capture — reads the node's current state, changes nothing on it. |
| `start` | Brings a stopped node back online — recreated identically at the same IP. With `--group-name`, starts every member of a group at once. See §8.13. |
| `stop` | Takes a running node offline — deletes the instance, keeps its data and IP. With `--group-name`, stops every member of a group at once. See §8.13. |
| `set-mode` | Makes a node manual-only (`--manual`: the scheduler never starts or stops it) or schedulable again (`--auto`). See §8.13. |
| `api-token-create` / `api-token-list` / `api-token-revoke` | Manage API tokens for scripts calling the REST API. See §8.13. |
| `list` | Shows every node you've onboarded and its current status at a glance. |
| `status` | Shows the full captured record for one node. |
| `history` | Shows the audit trail (create/delete events) for one node — "why was my instance down at 9am?" Works even for a node you've since `offboard`ed. |
| `schedule-set` | Sets (or replaces) one node's automatic start/stop schedule. See §8.5. |
| `schedule-show` | Shows one node's current schedule, if any. |
| `schedule-clear` | Removes one node's schedule — it goes back to manual-only control. |
| `group-create` | Creates a new, empty schedule group — give it a schedule afterward with `group-schedule-set`. See §8.6. |
| `group-schedule-set` | Sets (or replaces) a group's automatic start/stop schedule — applies to every current member. See §8.6. |
| `group-show` | Shows a group's schedule and its current members. |
| `group-list` | Lists every schedule group you've created. |
| `group-delete` | Permanently deletes a schedule group. Refuses if it still has members. |
| `group-depends` | Makes a group start only after one or more other groups are up and ready, and stop only after it is down (e.g. app servers after their database and cache). See §8.6. |
| `group-add` | Adds (or moves) a node into a schedule group. |
| `group-remove` | Removes a node from its group — asks what to do about its schedule if it doesn't have one of its own. See §8.6. |
| `poll` | Runs the scheduler — checks every node's individual AND group schedule and starts/stops it if due, and auto-reverts any expired manual override. Run it continuously (the normal way), or `--once` from cron. See §8.5/§8.6/§8.7. |
| `serve-api` | Runs the optional REST API server — the same capabilities as the CLI, over HTTP, with "Login with Linode" auth. See §8.8. |
| `extend` | Pushes an active manual-override auto-stop timer further out, or keeps a node (`--name`) or every member of a group (`--group-name`) running past today's scheduled stop. See §8.7. |
| `hooks-set` | Sets a node's (or a group's) pre-stop hook and/or post-start check — your own command, script path, or uploaded script, run on the node right before every stop and right after every start. See §8.12. |
| `hooks-show` | Shows a node's own hooks and the ones that actually apply to it (its own, or inherited from its group), or a group's hooks. |
| `hooks-clear` | Removes a node's own hooks (its group's then apply), or a group's hooks. |
| `hooks-run` | Runs a node's hook right now, without starting or stopping it — to test a hook, or re-run a failed post-start check after fixing the cause. |
| `clear-lock` | Admin escape hatch — forcibly clears a stuck in-progress operation. You should rarely need this: the scheduler clears a stale one by itself, and the dashboard has a **Clear lock** button. |
| `heal` | Compares every node with your account and fixes the local records where the account proves them wrong (an instance the records don't know about, a leftover from an unfinished start, a stale in-progress mark). The scheduler does this every few minutes. See §8.15. |
| `schedule-catch-up` | Lists nodes that aren't in the state their schedule wants (after an outage longer than the scheduler's one-hour catch-up window); `--apply` starts/stops them after you confirm. See §8.15. |
| `migrate-abandon` | Cancels an unfinished migration: the original instance boots from its own disk again and the copy's volume is deleted. See §6.4. |
| `env-backup` / `env-restore` | Your own encrypted copy of the settings (`.env`) and the deployment SSH key, encrypted to your age public key — only you can open it. See §8.10. |
| `backup-verify` | Restore-tests the backups now (the scheduler does it daily). See §8.15. |
| `scheduler-lease` | Which host is the active scheduler; `--take-over` makes this one active. See §8.15. |
| `host-watchdog` | Linode's Shutdown Watchdog on the scheduler's own host; `--enable` turns it on. See §8.15. |
| `alerts-check` / `alerts-test` | Show what needs attention right now (exit 1 if anything) / send a test alert. Alerts go to a webhook and/or email. See §8.15. |
| `reset-host-key` | Admin escape hatch — re-establishes SSH trust for a node after a genuine, confirmed key change. You should rarely need this either; see [§8](#8-day-to-day-usage) and the one-time migration note below. |
| `rebuild` | Disaster recovery — reconstructs your local registry from tags on your own Linode account, in case the machine running this tool (and its local records) is ever lost. You should rarely need this either. |
| `backup` | On-demand, whole-system backup — re-syncs every node's and group's Object Storage record, takes a full local/remote database snapshot, and saves the trusted host keys. Meant to be run on a schedule (cron/systemd timer). See §8.10. |
| `holiday-add` / `holiday-remove` / `holiday-list` | Holidays: dates on which scheduled starts are skipped (every node, one group or one node); stops still happen. See §8.14. |
| `holiday-settings` | Whether account-wide holidays apply to a group or a node (a node's own setting overrides its group's). See §8.14. |
| `backup-config` | Shows where this tool backs itself up and how the last backup went; sets, tests or removes the Object Storage settings. See §8.10. |
| `restore` | On a replacement host: puts the newest (or a chosen, or a local) database snapshot in place, plus the trusted host keys. Run `rebuild` afterward. See §8.10. |
| `ssh-key-protect` / `ssh-key-restore` | The deployment SSH key is kept recoverable automatically (encrypted in the database and Object Storage, public half in your Linode profile, passphrase in your Linode account); `ssh-key-protect` checks it now, `ssh-key-restore` gets it back on a replacement host. See §8.10. (`ssh-key-backup` stores an extra copy under a passphrase you choose.) |
| `offboard` | Permanently decommission a stopped node — releases its reserved IP, removes it from tracking, and optionally deletes its volumes. For when you're actually done with a node, not just pausing it. |
| `set-vpc-address` | Gives a stopped node's VPC interface a different address, used from its next start — for when another instance took its address while it was stopped. |
| `deregister` | Admin escape hatch — removes a node from local tracking only, with no changes to the real Linode instance, volumes, or reserved IP at all. For correcting a wrong or unsafe local record, not for decommissioning a real node (use `offboard` for that). |

> **Upgrading this tool on a fleet you already manage?** Read the one-time migration note in
> [§8](#8-day-to-day-usage) before your next `start`/`stop` — it takes one command per existing
> node and only needs to be done once.

Full detail on each below.

---

## 6. One-time migration onto Block Storage (Path B)

**Skip this whole section if the node you're onboarding was already built with its OS on a
Block Storage volume.** This is only needed for a node like the one built in §3 — local disk,
the way any ordinary Linode is created by default.

**Why this step exists at all**: this automation's entire mechanism depends on being able to
delete an instance and recreate it identically. A *local disk* is destroyed the instant its
instance is deleted — there'd be nothing to recreate from. A *Block Storage volume* is an
independent resource that survives deletion. So before this tool can manage a node, that node's
OS has to actually be living on a volume, not local disk.

This is automated end to end **except one step that genuinely can't be automated**: the actual
disk copy only runs inside Linode's Rescue Mode, which is only reachable through the interactive
Lish console — there's no way to run an arbitrary command there through the API. That one command
is the only manual step in this entire process.

### 6.1 `migrate-start`

```
python instance_manager.py migrate-start --name redis-standby-1 --instance-id <your-instance-id>
```

- `--name` — a short name **you choose**, to refer to this node by from here on. Doesn't have to
  match its Cloud Manager label.
- `--instance-id` — the numeric Linode instance ID (visible in Cloud Manager, or in the
  instance's URL).

**What this actually does:**

1. Runs two hard pre-flight checks over SSH: that `cloud-init` is installed and new enough
   (23.3.1 or newer, or an older build that already reports Akamai's datasource, as some
   Linode images ship), and that its metadata datasource is Akamai's. Both are required for a
   later step in this tool to work correctly; it refuses to proceed if either is missing. It also
   refuses outright if your instance somehow has more than one boot config — Linode's API has no
   "which one is active" field, so this tool won't guess which one to read the current disk from
   (the same refusal `onboard`/`rebuild` have for the same reason).
2. Warns (but doesn't block) if it finds hand-configured networking outside the OS's normal
   Network Helper — e.g. a custom netplan file. If you've done this deliberately, that's fine to
   proceed past, just be aware it'll be overwritten by this tool's own network handling on the
   next recreate.
3. Creates a new, appropriately-sized Block Storage volume to hold the migrated OS.
4. Boots your instance into Rescue Mode, with the original disk and the new volume attached.
5. Prints a single copy command for you to paste into the rescue console. The command finds the
   two disks itself — the new volume by its own Linode volume ID, the original disk by its
   size — so you never type device names.

**Output looks like this** (the copy command is shortened here; the real one is one long line):

```
YOUR TURN -- this is the one manual step in the whole process:
  1. In Cloud Manager, open instance <id> and click "Launch LISH Console".
  2. At the rescue shell (root@finnix), paste and run this one command. It finds the
     disks itself -- the new volume by its own ID, the original disk by its size
     (~20480MB) -- and refuses, copying nothing, if either is
     ambiguous. (Rescue Mode's device letters vary between systems, so don't type a
     dd command with fixed device names.)

       DST=$(readlink -f /dev/disk/by-id/scsi-0Linode_Volume_redis-standby-1-os-vol-1a2b3c4d); ... COPY_DONE ...

  3. Wait for COPY_DONE (this takes a few minutes). COPY_FAILED means the copy did not
     complete -- don't continue; run migrate-start --force to start over.
  4. If it printed 'Could not identify the disks', stop and check `lsblk`: the
     original disk is ~20480MB, the new volume ~25GB.
  5. Once it prints COPY_DONE, run:
       instance_manager.py migrate-resume --name redis-standby-1
```

### 6.2 The one manual step

In Cloud Manager, open the instance and click **"Launch LISH Console."** You land at the rescue
shell (`root@finnix`). Paste the copy command `migrate-start` printed, exactly as printed.

**Why it doesn't use fixed device names.** Inside Rescue Mode, which `/dev/sdX` letter the
original disk and the new volume get varies between systems — the original disk can appear as
`/dev/sdg` rather than `/dev/sda`. A `dd` with the wrong letters copies in the wrong direction and
destroys the original disk. So the printed command identifies the disks itself: the new volume
through its `/dev/disk/by-id/scsi-0Linode_Volume_<label>` link, which names that exact volume,
and the original disk as the only other device matching its known size. If it can't identify
exactly one of each, it prints `Could not identify the disks unambiguously -- nothing copied`
plus the device list, and copies nothing.

The copy itself is `dd` with `conv=fsync` (data is flushed to the volume before it reports done)
followed by `sync`. `status=progress` shows it moving. It ends with one line:

- `COPY_DONE` — the copy finished; run `migrate-resume`.
- `COPY_FAILED` — the copy did not complete (an I/O error, for example). Don't continue; run
  `migrate-start --force` to start over with a fresh volume.

For a typical node this takes a few minutes, roughly proportional to how much data is actually
on the disk.

### 6.3 `migrate-resume`

```
python instance_manager.py migrate-resume --name redis-standby-1
```

**What this actually does:**

1. Builds a real boot configuration pointing at the migrated volume, and reattaches any other
   Block Storage volumes (like your Redis/TiDB data volume) that were already on the node.
2. Shuts the node down (exiting Rescue Mode) and boots it from the new configuration.
3. Verifies it's genuinely booted from the *migrated* volume — not just that the API reports
   "running," and not just that SSH answers at all (a `dd` byte-for-byte copy means the guest's
   file contents look identical whether it's really running from the new volume or an old local
   disk still happens to be reachable; this checks the actual underlying block device instead).
   If that check doesn't match, this refuses and leaves the migration checkpointed exactly where
   it was — it will never report success against the wrong disk.
4. **Reserves the node's public IP in place** — the same address it already had, converted to a
   reserved IP so it's now permanently yours and will never change across future cycles.

When this finishes, your node is running exactly as before — same IP, same data — just with its
OS now on Block Storage instead of local disk. It's ready for the next step.

### 6.4 If something goes wrong mid-migration

**`migrate-resume` refuses with a phase error.** It only ever proceeds if `migrate-start`'s
checkpoint confirms the manual `dd` + `sync` step (§6.2) is actually done — building the real
boot config any earlier could boot into an empty or half-copied volume. If you see this and
you're certain `dd` + `sync` genuinely finished, or you want to abandon this attempt entirely,
re-run `migrate-start` with `--force` (below) rather than forcing `migrate-resume` past the check.

**Cancelling a migration.** Changed your mind, or the host running this tool was lost while a
migration waited for its copy step? Cancel it — the original instance is taken out of Rescue Mode
and boots from its own disk, which a migration never changes before `migrate-resume`, and the
destination volume (the copy) is deleted:

```
python instance_manager.py migrate-abandon --name redis-standby-1
```

It asks you to type the name (`--yes` skips that; `--no-boot` leaves the original powered off). A
kept pre-migration backup stays until you remove it (`backup-delete`). Once `migrate-resume` has
started changing the boot config, cancelling is refused — finish with `migrate-resume`, or
`rollback` if a backup was kept. The migration page in the dashboard has **Cancel this migration**
for the same thing.

**`migrate-resume` itself crashes or times out partway through.** It's safe to just run it again
— it checkpoints its own progress (creating the boot config, shutting down, booting, verifying
device identity, reserving the IP) and reconciles against the node's actual live state before
repeating anything, so a retry never re-shuts-down an already-migrated instance or creates a
second boot config. If the reachable node turns out not to actually be booted from the migrated
volume (see step 3 above), the checkpoint stays put rather than advancing — re-running
`migrate-resume` will re-check the same thing, not silently move past it. That case needs a look
in Cloud Manager (Configs) before retrying, not a blind re-run.

**Re-running `migrate-start --force` for a name that already has an in-progress migration.**
This is safe — it starts a fresh attempt (a new destination volume, new Rescue Mode boot) rather
than resuming the old one. It re-runs every pre-flight check (instance running, `cloud-init`
version, networking) against the *replacement* attempt first, before touching anything about the
old one — if any of those fail (or if creating the replacement volume itself fails, e.g. a
capacity/quota issue), no replacement is created and the old attempt's tags and checkpoint are
left exactly as they were, so a failed `--force` never leaves you with contradictory local/remote
state. Only once the replacement volume genuinely exists and is durably checkpointed is the old
attempt's destination volume touched — it's **not deleted automatically**: its ID is preserved
and printed to you, and its Cloud Manager tags are switched from "active migration" to
"orphaned" at that point. (Every migration's destination volume is tagged this way in Cloud
Manager from the moment it's created, precisely so it's discoverable by tag even if
`state/migrations.json` itself is lost mid-migration, not just once an attempt is abandoned or
completes — `migrate-orphans`' listing below merges in anything found this way even if the local
archive doesn't know about it.) The old ID is also recorded locally in `state/orphaned_migration_
attempts.json` (keyed by the node's name) once the *new* attempt's `migrate-resume` finally
succeeds — check on these anytime with:
```
python instance_manager.py migrate-orphans --name redis-standby-1
```
and delete them once you're sure they're no longer needed:
```
python instance_manager.py migrate-orphans --name redis-standby-1 --cleanup
```
(add `--volume-id <id>` to target just one, if there's more than one). Requires typing the name
to confirm, same as `offboard` (add `--yes` to skip the prompt, e.g. in a script). This file is
never cleaned up on its own otherwise — nothing scans it or reminds you it exists beyond this
command.

Like `rebuild` (above), `migrate-orphans` returns `0` only when nothing is wrong for the name(s)
checked, and non-zero if any real conflict or a scan failure is found — a conflict is still
printed as a `WARNING` either way, but if you're calling this from a script, cron job, or an
automated DR runbook, check the exit code rather than scraping the printed summary.

If you've lost `state/migrations.json` entirely and `migrate-orphans` shows a volume still
tagged "active" for a migration you know is actually dead (confirmed by hand in Cloud Manager),
mark it orphaned so it becomes eligible for `--cleanup`:
```
python instance_manager.py migrate-orphans --name redis-standby-1 --volume-id <id> --mark-orphaned
```
This only ever retags — it never deletes anything on its own, and refuses if the volume isn't
actually tagged active for that name. Same confirmation prompt (type the name, or pass `--yes`)
as `--cleanup` above.

**A newly reserved or reused IP address won't accept an SSH connection during `migrate-start`,
even though the node is genuinely reachable.** This can happen if Linode reissues an address that
this tool's own trust records (`state/known_hosts`) still remember from a previous, unrelated
node — e.g. one you `offboard`ed earlier. Since the node in question hasn't been onboarded yet,
`reset-host-key`'s usual `--name` form doesn't apply here; use `--ip` instead:
```
python instance_manager.py reset-host-key --ip <the-address> --yes
```
Only do this after independently confirming (Cloud Manager, Lish console) that the address
genuinely belongs to the node you expect.

---

### 6.5 Keeping a backup of the original, and rolling back

The one step of a migration that can't be undone on its own is the copy: once the node boots from
its new volume, `migrate-resume` deletes the old local disk. If you want a guaranteed way back to
exactly the system you had, ask `migrate-start` to keep a backup first:

```
python instance_manager.py migrate-start --name web-1 --instance-id 12345678 --backup
```

Before anything is copied, the node is powered off and Linode clones it -- its disks and boot
configuration -- and each of its attached Block Storage volumes, using Linode's own clone feature.
The clone stays **powered off**, with its private (VPC/VLAN) interfaces removed so it can never take
or clash with an address while it waits; every setting needed to put the original back (plan,
region, label, tags, every interface and address, the public address, which volume sits in which
slot) is recorded locally, in Object Storage when configured, and as a tag on the clone and its
volumes. The migration then carries on as usual.

**The backup is a billable resource until you delete it** -- a powered-off Linode still bills at
its plan's rate, and each cloned volume bills as Block Storage. Before creating it, `migrate-start`
shows the monthly cost from Linode's current price list (or a plain "billable resource" notice if
the price list can't be read) and asks you to confirm; `--yes` skips the question.

**Going back.** If anything about the migrated node is wrong, at any point -- mid-migration, after
`migrate-resume`, or after onboarding and any number of stop/start cycles:

```
python instance_manager.py rollback --name web-1
```

- The backup takes back the original public address (kept reserved), its VPC and VLAN addresses,
  its label, and its data volumes under their original labels, so the original `/etc/fstab`
  mounts them as before. It is then booted and checked over SSH.
- If the original instance still exists (a migration not finished yet), it is replaced: its public
  address is reserved first, its volumes are detached and kept, and the instance is deleted -- the
  backup holds the system it had before the migration.
- If the node is onboarded, it must be stopped first (`stop`, or `stop --skip-precapture` if it
  can't be reached); `rollback` refuses while it is running. The scheduler then stops managing the
  name; the migrated volumes are kept, never deleted -- remove them when you no longer need them.
- `rollback` refuses when an original VPC address has since been taken by another instance; free
  it first.
- `--no-boot` restores everything but leaves the instance powered off.

The restored node is your original system, outside this tool. To schedule it again, migrate and
onboard it again (with a fresh backup if you like).

**When you're happy with the migration**, delete the backup so it stops billing:

```
python instance_manager.py backup-list
python instance_manager.py backup-delete --name web-1
```

`backup-delete` permanently deletes the clone and its cloned volumes (each only after re-checking it
still carries this backup's tag). For a backup that has already been rolled back to, it only
removes the record -- that instance is your running system. In the dashboard, the same actions are
on the **Backups** page; the migration wizard offers "Keep a backup of the original first" on its
confirmation step.


### 6.6 Letting the tool run the copy (optional)

If a pre-migration backup is kept (§6.5), the tool can run the copy for you instead of you pasting
the command into the Lish console. It opens the instance's Rescue Mode console over Lish (Linode's
SSH console gateway, `lish-<region>.linode.com`), types exactly the command `migrate-start`
printed, follows `dd`'s progress, waits for `COPY_DONE`, and then runs `migrate-resume`:

```
python instance_manager.py migrate-copy --name web-1
```

**One-time setup.** The console is opened as your Linode user with this deployment's SSH key, so
that key has to be one of your profile's Lish keys. In Cloud Manager open **Profile → LISH Console
Settings**, allow key authentication, and add the deployment's public key (the `.pub` file next to
the key in `LINODE_SSH_KEY_PATH`). The tool only reads your profile to check this; it never
changes it. `--check` reports whether everything is in place and what's missing:

```
python instance_manager.py migrate-copy --name web-1 --check
```

**What it refuses.** Without a kept backup of this same instance it does nothing (the backup is
what makes the copy safe to automate: whatever happens, `rollback` puts the original back). It
also refuses unless the migration is waiting for its copy. If the copy command can't identify the
disks, or `dd` fails, nothing further happens: the migration keeps waiting for its copy, so you can
run `migrate-copy` again, do the copy yourself, or roll back. If the copy finishes but
`migrate-resume` fails, run `migrate-resume` again. `--no-resume` stops after the copy.

The console session is recorded in `state/logs/lish-<name>.log`, cleaned of terminal control codes
and with each line timestamped (UTC), so `dd`'s progress reads as one line per update. In the
dashboard, the migration's copy step shows **Run the copy for me** when a backup is kept and the
key is registered, or the setup steps above (with the key to paste) when it isn't. While the copy
runs, **Watch the console session** opens it on the Logs page, updating live. After the copy it
finishes the migration and onboards the instance, the same as the manual path.

A session log is removed automatically once it has had no new output for
`ACTIVITY_LOG_RETENTION_DAYS` days (30 by default; the scheduler checks hourly). To remove one
sooner, select it on the Logs page and click **Delete session**, or run:

```bash
python instance_manager.py console-session-delete --name web-1
```

Deleting refuses (exit code 3) while anything holds the instance's lock, such as a copy that is
still running.

## 7. Onboarding

```
python instance_manager.py onboard --name redis-standby-1 --instance-id <your-instance-id>
```

If you just did a migration in §6, use the same `--name` you used there. If your node was
*already* on Block Storage (you skipped §6), pick any name you like.

**What this actually does — pure capture, nothing is changed on your node:**

- Confirms three things and refuses cleanly if any fail:
  - The node is currently **running** (needed to inspect it over SSH).
  - Its OS is genuinely on a Block Storage volume, not local disk.
  - Its public IP is **reserved**. (If you did §6, this is already true. If not, and this
    refuses, reserve the IP yourself first via Cloud Manager or the API, then re-run.) A node
    with **no public interface at all** — only VPC and/or VLAN interfaces — has no public IP to
    reserve and is onboarded without one; see "VPC-only and VLAN-only nodes" below.
- Reads and records: its network configuration, every attached data volume (including yours from
  §3.4, with its actual mount point and filesystem), the real contents of
  `/root/.ssh/authorized_keys`, its label and tags, its plan/size, any attached firewall, and its
  maintenance-policy/watchdog settings.

You'll see a summary printed of everything it captured — worth a quick glance to confirm it
looks right (in particular, that `data_volumes` shows at least 1 entry if you expected data to be
there).

From this point on, the node is under this tool's management by the name you gave it.

**VPC-only and VLAN-only nodes.** A node whose only interfaces are VPC and/or VLAN (no public
interface) is fully supported — onboard, stop, start, schedules, groups, hooks, `rebuild`, and
`offboard` all work the same way. What's different:

- **Your scheduler host must be able to reach the node's private address over SSH** (port 22):
  every start ends with a real SSH reachability check, and every stop reads the node's current
  SSH keys first. In practice that means running the scheduler inside the same VPC (or a VPC/
  network routed to it), or on the same VLAN for a VLAN-only node. When a node has both, the VPC
  address is used.
- **Its network identity is preserved exactly**: the same VPC/VLAN address on every cycle. On
  every recreate, a VPC-only node gets its default route via the VPC subnet's gateway (the
  subnet's first address) and the region's DNS servers, the same as Linode's own network
  configuration gives it. Whether it can reach the internet through that route depends on your
  VPC setup (for example a NAT gateway), exactly as before it was onboarded.
- **There's no reserved IP**, so `status` shows none and `offboard` has nothing to release.
- **Migrating one off local disk** (`migrate-start`/`migrate-resume`, or the dashboard's wizard)
  works the same way: pre-flight and verification connect over the VPC/VLAN address, the new boot
  config keeps the node's own VPC/VLAN interfaces (no public interface is added), and no IP is
  reserved. The dashboard's "Test reachability" check also tests that address. (Every migration
  keeps all of a node's interfaces — a public + VPC or public + VLAN node keeps both.)
- **Golden rule: give scheduled nodes their own private network.** Put the instances this tool
  schedules in their own VPC subnet and on their own VLAN label, and don't create other instances
  in that subnet or on that VLAN by hand. A stopped node's instance doesn't exist, so nothing holds
  its private address while it's stopped:
  - **VPC:** another instance in the subnet can be given the address. The tool notices before the
    next start and moves the node to a free address, so it still starts, but it comes back on a
    different address.
  - **VLAN:** Linode doesn't check VLAN addresses at all. An instance given the same VLAN address
    as a stopped node starts normally, and once both run, traffic goes to whichever answers first.
    Nothing fails, so neither Linode nor this tool can catch it; a dedicated VLAN label is the only
    protection.
  - **Public IPs** never collide: every managed node's public IP is reserved and stays with your
    account while the node is stopped.

  If a subnet has to be shared, give every other instance an explicit address from the bottom of
  the range (the tool moves nodes to addresses at the top). Onboarding warns when a node's subnet
  or VLAN already holds instances this tool doesn't manage, and
  `python instance_manager.py status --name db-1 --check-network` runs the same check at any time.
  The scheduler's own host is expected in the subnet (it has to reach the nodes) and isn't counted.
- **A stopped node's VPC address can be taken — and is reclaimed automatically.** While a node
  is stopped its instance doesn't exist, so Linode treats its VPC address as unused, and an
  instance created in that subnet can be given it (Linode hands out the lowest free address).
  Before every start — scheduled, manual or API — the tool checks the node's recorded VPC
  addresses against what is actually in use. If another instance holds one, the node is moved to
  a free address in the same subnet (taken from the top of the subnet, which new instances are
  least likely to get) and started there, with nothing for anyone to run; the start's output and
  the poller's log say which address it moved from and to, so you can update anything that
  reaches it by address. Its trusted SSH host key moves with it, so the usual strict host-key
  check still applies. To choose the new address yourself instead, move the stopped node first:

  ```
  python instance_manager.py set-vpc-address --name db-1 --address 10.24.1.40
  ```

  (or "Change" on the node's **VPC address** card in the dashboard). It refuses an address outside
  the subnet, the subnet's gateway, one another managed node is recorded with, or one a running
  instance holds. Onboarding refuses an instance whose VPC address is already recorded for another
  managed node. To keep addresses stable, give other instances in that subnet their own explicit
  addresses, or keep scheduled nodes in a range nothing else is assigned from. (While a node is
  running — and during a migration — its address is held and can't be taken.)
- **SSH host keys on private addresses.** A node reached over a VPC/VLAN address with no host key
  on record for that address — after an automatic move, or if the tool's `known_hosts` file was
  lost — accepts the node's key on first contact at its next start, as onboarding does. A key
  that differs from one already on record is still refused. Nodes reached over a public reserved
  IP always use the strict check.
- **VPC 1:1 NAT** (a public address mapped onto the VPC interface instead of a separate public
  interface) is supported under both networking models. The guest is set up exactly like a
  VPC-only node, and the scheduler reaches it over its VPC address. Linode releases an ordinary
  NAT address when the instance is deleted, so unless the address is **reserved**, every start
  gives the node a new public address (the start says which). Reserve it in Cloud Manager to
  keep it: a reserved NAT address is reused on every start. Onboarding warns when it isn't
  reserved. Under the older (legacy config) model the NAT address is the instance's own public
  IPv4, so only one NAT address per node is supported; a node mapping two is refused at
  onboarding.
- **A reserved public IP that has since been assigned to another instance** (or released from
  the account) can't be given back to a stopped node, so its next start fails straight away with
  a message saying so, before anything is created. Unassign the address from the other instance
  in Cloud Manager (Networking -> Reserved IPs), then start the node again; a scheduled start is
  retried on every poller tick until its catch-up window ends.
- A **VLAN-only** node gets no default route or DNS from the tool — a VLAN has no gateway — so
  anything it needs beyond its own VLAN must come from its own configuration. Linode's metadata
  service isn't reachable from an instance whose only interface is a VLAN, so a recreated
  VLAN-only node runs from the network settings already on its disk (its VLAN address never
  changes), and onboarding writes one cloud-init setting on the node,
  `/etc/cloud/cloud.cfg.d/99-linode-instance-scheduler.cfg` (`ssh_deletekeys: false`), so its SSH
  host keys survive each recreate. The scheduler must be on the same VLAN to reach it.

**If the node has a VPC interface, you may need `--vpc-id`.** For the newer `linode` interface
model, the VPC's ID is already part of what's captured — nothing extra needed. For the older
`legacy_config` model, the API doesn't expose `vpc_id` directly on the interface, so this tool
first tries to auto-discover it by scanning your account's VPCs for the one containing the
node's subnet. That works as long as the VPC is visible to this tool's API token. If it can't
find it (a scoping issue, or the VPC belongs to a different account/project), `onboard` refuses
with a clear error and you supply it explicitly:

```
python instance_manager.py onboard --name redis-standby-1 --instance-id <id> --vpc-id <vpc-id>
```

(The VPC ID is visible in Cloud Manager under VPCs, or via the API.) You'll only ever need this
for a `legacy_config` node with a VPC interface where auto-discovery fails — most nodes never
need this flag at all.

**Re-onboarding an already-managed name with `--force`** (e.g. to re-capture after manual
changes) is safe and routine when it's the *same* underlying instance. If it resolves to a
**different** instance, volume, IP, or region than what's currently on record, you'll see an
explicit diff (old value → new value) and have to type the name to confirm — this exists
specifically to catch a mistyped `--instance-id` before it silently redirects a name you already
trust to unrelated infrastructure. If that different instance's own OS volume, data volume, or
reserved IP is *already* tagged in Cloud Manager under a **different** name (e.g. you meant to
onboard a fresh node but typed an `--instance-id` that's already managed elsewhere), onboarding is
refused outright, naming the conflicting node — there's no override flag or confirmation prompt
that lets you push through this one. That's deliberate: this tool tried, in an earlier version, to
support a confirmed "take over ownership" path, and found it couldn't actually make that safe —
suppressing the refusal never removed the *other* name's tag, so the "successful" result was a
resource claimed by two names at once, which is exactly what `rebuild`'s disaster-recovery scan is
designed to detect and refuse to guess about (for *either* name, not just the one that got
"transferred"). If you genuinely want to repoint a name at infrastructure another name currently
owns, offboard the other name first (`offboard --name <other-name>`, see §8) — that cleanly
releases its tags — then re-run this onboard. `--yes` still skips the identity-diff confirmation
above; it does not, and cannot, skip this refusal. If a `--force` re-onboard moves this name to a
**different reserved IP** than it had before (confirmed via either prompt), this tool's own
cached SSH trust for the *old* address is also cleared automatically — the same cleanup
`offboard` already does when it releases an IP — so a future reissue of that old address to an
unrelated node won't need a manual `reset-host-key --ip` first.

---

## 8. Day-to-day usage

> **One-time migration note — read this if a node in your fleet was onboarded with an older
> version of this tool, before SSH host-key verification existed.** `start`/`stop`/`rebuild`
> verify a node's SSH host key against a record this tool keeps itself (`state/known_hosts`) —
> strictly, by default, since a steady-state recreate is guaranteed to present the *same* key
> every time (see `start` below). That record has to exist first. A node with nothing recorded
> yet will fail its very next `start` with a `SECURITY WARNING` about a key mismatch — not
> because anything is actually wrong, but because there was nothing to compare against yet.
> **Fix, once per node with nothing recorded, before your next `start`/`stop`:**
> ```
> python instance_manager.py reset-host-key --name redis-standby-1 --yes
> ```
> This connects once, trusts whatever key the node currently presents, and records it. Do this
> for every node in this situation — after that, every future `start`/`stop` verifies against it
> automatically and you'll never need to touch this again unless you genuinely rotate a host's
> key on purpose (see `reset-host-key` below).

### `stop` — take a node offline

```
python instance_manager.py stop --name redis-standby-1
```

Prompts for confirmation (add `--yes` to skip it, e.g. in a script). Re-captures everything
first — network config, data volumes, label/tags, and instance settings — in case anything
changed in Cloud Manager since the last cycle, so an out-of-band change (a rename, a re-tag, a
firewall swap) is never silently lost. Then refreshes this tool's own disaster-recovery tags on
your volumes/IP before deleting the Linode instance. **The data volume(s) and the reserved IP
are never touched — only the compute instance is deleted.**

If that tag refresh fails, `stop` refuses rather than deleting anyway — the live instance is the
best remaining source of truth for disaster recovery, and deleting it while that mapping is
known to be wrong or unconfirmed would make things worse, not better. A genuine ownership
conflict (these resources are tagged for a different name) or a verification failure always
refuses; a plain transient API error also refuses by default, but can be overridden with
`--force` if you know the tagging service had a momentary blip and need to stop the instance
anyway:

```
python instance_manager.py stop --name redis-standby-1 --force
```

**If the node itself is SSH-unreachable** (a guest firewall rule, a crashed `sshd`, a wedged
network stack), the re-capture step above will retry for a while and then `stop` will refuse,
with nothing deleted — reachability can't be assumed away when re-capturing data volumes needs
a live SSH session. If you've independently confirmed (Cloud Manager, Lish console) that the
node is genuinely unreachable and you just need to stop billing, add `--skip-precapture`:

```
python instance_manager.py stop --name redis-standby-1 --skip-precapture
```

This uses the last-known data volume list from the registry instead of a fresh SSH capture. The
trade-off: a data volume attached out of band since the last `start`/`stop` cycle would be
silently missed from that list. Only use it when you've confirmed the unreachability yourself —
see [Troubleshooting](#11-troubleshooting) for the full recovery sequence if the instance ends up
deleted out of band (e.g. via Cloud Manager) instead.

### `start` — bring it back online

```
python instance_manager.py start --name redis-standby-1
```

Recreates the instance from scratch: same plan, same reserved IP, same volumes reattached at the
same device slots, same label/tags/firewall/settings, same SSH host key (no "remote host
identity changed" warning). Waits for it to actually respond over SSH — not just for the API to
report "running" — before telling you it's up, and **actively verifies the host key matches what
this tool already has on record for that node**, not just that *some* key was accepted. If it
genuinely doesn't match, `start` refuses with a `SECURITY WARNING` rather than silently
continuing — see the one-time migration note above if you're seeing this on a node you know is
fine, or `reset-host-key` below if a key change is real and confirmed.

### `list` — see everything at a glance

```
python instance_manager.py list
```

```
redis-standby-1: running  ip=172.x.x.x  region=in-bom-2  linode_id=123456
tidb-replica-1:  stopped  ip=172.x.x.x  region=in-bom-2  linode_id=None
```

(`in-bom-2` here is just an example, not a required region — your instances' regions are
captured from wherever they actually live. Any Block-Storage-capable core region is supported;
distributed compute regions aren't, since this tool requires the OS to live on a Block Storage
volume.)

### `status` — full detail on one node

```
python instance_manager.py status --name redis-standby-1
```

Prints the complete captured record for that node — useful for confirming exactly what will be
reproduced on the next `start`, or for debugging.

### `history` — "why was my instance down at 9am?"

```
python instance_manager.py history --name redis-standby-1
```

```
2026-08-25T09:03:11+00:00  delete  success  triggered_by=manual
2026-08-25T09:00:02+00:00  create  success  triggered_by=manual
```

Every real `stop`/`start` attempt that genuinely reaches the point of creating or deleting the
underlying Linode instance gets recorded here — newest first, with a `--limit N` flag (default
50) if you only want the most recent handful. A no-op call (the node was already in the state
you asked for, or you declined a confirmation prompt) is never recorded, since nothing was
actually attempted. This works even for a node you've since `offboard`ed — the history is kept
independently of whether the node is still tracked, so a past node's story isn't lost the moment
it's decommissioned.

### `clear-lock` — admin escape hatch

Every `start`/`stop` holds a lock on that node for the duration of the operation, so two
operations can never run against the same node at the same time (e.g. two people both running
`start` for the same node at once). If something crashes mid-operation and leaves a node showing
as locked when nothing is actually still running:

```
python instance_manager.py clear-lock --name redis-standby-1
```

You'll get a warning first and a `[y/N]` prompt — **only confirm after independently checking in
Cloud Manager that nothing is actually still in flight** for that node. Add `--yes` to skip the
prompt (e.g. in a script), but only once you're scripting around a case you've already verified is
safe. (If another `start`/`stop`/`rebuild`
process for this same node is genuinely still running right now, `clear-lock` refuses outright
with a clear error instead of racing it — this only ever matters for a lock left behind by
something that actually crashed.) The scheduler also clears such a mark by itself within a few
minutes, and the instance's dashboard page shows a **Clear lock** button while it's set.

### `reset-host-key` — admin escape hatch for a genuine SSH key change

Every `start`/`stop` verifies a node's SSH host key against what this tool already has on record
for it (see the one-time migration note above) — and refuses if it doesn't match, rather than
silently trusting whatever's presented. That's deliberate: it's exactly the check that would
catch a real problem (wrong disk, a misconfigured DNS/routing issue, or worse). This command is
the *only* way to move past a genuine mismatch:

```
python instance_manager.py reset-host-key --name redis-standby-1 --yes
```

**Only use this after independently confirming the new key is legitimate** — e.g. via Cloud
Manager's Lish console, not by trusting the same SSH connection that might itself be the problem.
Real reasons you'd need this: you intentionally replaced a node's disk outside this tool, or
you're migrating an already-onboarded node to a tool version with this check for the first time
(the one-time migration note above). It's not something you should need routinely.

For a node that **isn't onboarded yet** — most commonly a reserved IP Linode reused that this
tool's own records still remember from a different, earlier node (see §6.4) — use `--ip` instead
of `--name`, since there's no registry record to look the address up from yet:
```
python instance_manager.py reset-host-key --ip 172.x.x.x --yes
```
Both forms behave the same otherwise: confirm by typing the name/address, then this tool tries a
fresh connection before actually forgetting the old key — if that connection fails, the old
trust is restored automatically rather than left blank, so a failed attempt never leaves the node
in a *worse* state than before you ran the command.

### `rebuild` — recovering from a lost local machine

Your local registry (the file this tool keeps track of your nodes in) lives only on whatever
machine you run it from. If that machine is ever lost entirely — hard drive failure, an
accidentally wiped laptop, whatever — this command gets you back.

**How it works**: every time you `onboard`, `start`, or `stop` a node, this tool tags that node's OS
volume, data volume(s), and reserved IP directly in your Linode account with the mapping
("these resources belong to `redis-standby-1`"). That's the one piece of information that isn't
recoverable from Linode's API on its own — everything else about a node (its network config,
tags, size, etc.) can always be re-read from the live instance. So instead of backing up the
whole local file somewhere, the one thing that actually matters is written directly onto your
own Linode resources.

```
python instance_manager.py rebuild
```

This scans your account for those tags and rebuilds your local registry from them — no backup
file needed anywhere. Two outcomes, depending on whether each node happened to be running or
stopped at the moment you lost your local machine:

- **Node was running** — full recovery. Everything is re-read live, exactly like a fresh
  `onboard` — including the same refusal `onboard` has if the instance somehow has more than one
  boot config (Linode's API has no "which one is active" field, so this tool won't guess): that
  one name is skipped and reported, not silently recovered against the wrong config.
- **Node was stopped** — partial recovery. The tool can tell you which volumes and which IP are
  yours (so nothing is left as an orphaned, unlabeled resource you'd have to hunt down
  manually), but the finer detail (network config, SSH access, label/tags) was only ever knowable
  while that instance actually existed, and is genuinely gone — this tool won't guess at it. A
  node recovered this way is marked `needs_manual_recovery`; boot it once manually via Cloud
  Manager from the recovered `os_volume_id`, then run `onboard` again as normal to fully restore
  management.

If the tags for a name turn out ambiguous or incomplete for any reason (e.g. a stale tag left on
an old resource, or resources for one name somehow spanning more than one region — neither
should happen in normal use), that node is skipped and reported rather than guessed at; the
warning tells you exactly what to check in Cloud Manager before re-running.

**If the node had a schedule set (§8.5), or belonged to a group (§8.6), `rebuild` recovers those
too** — not just the node itself. Both are mirrored onto the node's OS volume as tags the same
way its identity is, specifically so they survive this exact scenario; nothing extra to do, they
come back automatically alongside the node in the same `rebuild` run. If the group itself was
lost too (not just this one node's own record), it's recreated from this node's own tags —
another member of the same group recovered in the same `rebuild` run just rejoins it, no
duplicate group.

**Exit code**: `rebuild` returns non-zero if any node it found failed, was skipped as ambiguous/
incomplete, or had a tag-derived name that failed validation — or if it found contradictory tags
on a resource outright. It returns `0` only when every node it found was cleanly recovered (fully
or partially). If you're calling this from a script, cron job, or an automated DR runbook, check
the exit code rather than scraping the printed summary.

**If a recovered, running node has a VPC interface under the older `legacy_config` model**, add
`--vpc-id` — the same auto-discover-then-fallback flag `onboard` uses (§7), for the same reason
(that model doesn't expose `vpc_id` directly, so it's inferred by scanning your account's VPCs).
It applies to every node recovered in that one `rebuild` run, not just one:

```
python instance_manager.py rebuild --vpc-id <vpc-id>
```

Only needed if auto-discovery can't find the recovered node's VPC (a scoping issue, or the VPC
belongs to a different account/project) — most runs never need it. If you skip it and a node
genuinely needs it, that node comes back missing
`vpc_prefix`, and its very next `start` fails with a `vpc_prefix is required...` error — that's
your signal to come back here and re-run with `--vpc-id`. Note that `rebuild` has no `--name`
flag — it always scans your whole account — so fixing just that one node still means re-running
`rebuild --vpc-id <vpc-id> --force` for the whole account again; `--force` is required since it'll
otherwise skip every name already in your local registry (see
[Troubleshooting](#11-troubleshooting) for `--force`'s exact behavior).

You'll rarely need this — it exists purely as a safety net for the worst case.

### `offboard` — permanently decommissioning a node

Everything above is about pausing a node and bringing it back identical. `offboard` is
different — it's for when you're genuinely done with a node for good, not just taking it
offline until next time.

```
python instance_manager.py offboard --name redis-standby-1
```

**Requires the node to already be stopped** — `stop --name redis-standby-1` first if it's still
running. Offboarding refuses cleanly on a running node rather than doing something surprising
with a live instance.

By default, this:
- **Releases the reserved IP** back to Linode's pool, permanently. If you ever re-onboard a
  similar node later, it gets a brand-new address — this exact one is gone for good.
- **Leaves the OS and data volumes intact** — nothing about your actual data is touched — but
  removes this tool's internal tracking tags from them, so a future `rebuild` can never mistake
  them for a node that still needs recovering.
- **Removes the node from your local registry.**

You'll be asked to type the node's name to confirm before anything happens — this is a
one-way action, not something a stray `y` at a prompt should be able to trigger by accident.

**To also permanently delete the volumes**, not just release the IP:

```
python instance_manager.py offboard --name redis-standby-1 --delete-volumes
```

This destroys the actual data on those volumes — irreversibly. Only use it once you're certain
you'll never need that data again. The default (no `--delete-volumes`) is deliberately the
safer choice: you can always delete volumes manually later via Cloud Manager once you're sure,
but you can't undo a deletion.

### `deregister` — admin escape hatch for a wrong or unsafe local record

This is different from `offboard` above, even though both remove a node from your local
registry — the important difference is what each one touches on the Linode side:

|                          | `offboard`                                     | `deregister`                          |
|--------------------------|-------------------------------------------------|----------------------------------------|
| Requires the node stopped | Yes                                             | No — works in any status              |
| Releases the reserved IP | Yes, always                                     | No — never touches it                 |
| Can delete volumes       | Optionally (`--delete-volumes`)                 | No — never touches them               |
| Removes management tags  | Yes                                              | No — leaves everything as-is          |
| Removes local registry entry | Yes                                          | Yes                                    |
| Use it for               | Genuinely decommissioning a node for good        | The *record itself* is wrong or unsafe |

Use `deregister` when the problem is with this tool's own bookkeeping, not with the actual
node — the node itself should be left completely alone:

```
python instance_manager.py deregister --name test-node-1 --yes
```

The clearest real example: a node onboarded before it had actually been migrated onto Block
Storage (its OS still on local disk). This tool's own registry doesn't distinguish that
correctly in every version, and if you ever ran `stop` on a node like that, the delete would be
real and unrecoverable — a local disk doesn't survive instance deletion the way a Block Storage
volume does. If you ever suspect a node was onboarded incorrectly, `deregister` it, verify the
node itself is untouched (check Cloud Manager — it will be, since this command never talks to
the Linode API at all), fix the actual problem (e.g. run the Path B migration properly, see §6),
then `onboard` it again.

You'll be asked to type the node's name to confirm, same as every other one-way action here.
`--yes` skips the prompt.

### 8.5 Scheduling — automate start/stop

**This is only individual, per-instance scheduling** — one node, one schedule. There's no group
scheduling yet (see the "Known limitations" note below).

**Set a schedule**, e.g. weekdays 9am–6pm India time:

```
python instance_manager.py schedule-set --name redis-standby-1 --timezone Asia/Kolkata \
  --days mon,tue,wed,thu,fri --start-time 09:00 --stop-time 18:00
```

`--timezone` is any IANA name (`Asia/Kolkata`, `US/Pacific`, `UTC`, ...) — the schedule's own
day/time rules are resolved in that timezone, DST included, not UTC. `--days` is comma-separated
3-letter days (`mon`..`sun`). **Overnight windows work too**: a `--stop-time` earlier than
`--start-time` stops the *next* day — `--days mon,tue,wed,thu,fri --start-time 22:00
--stop-time 06:00` runs each weeknight from 22:00 until 06:00 the following morning (`--days`
names the day each window *starts*). Start and stop must differ.

Need more than one rule (e.g. different hours on weekends)? Pass a full JSON array instead:

```
python instance_manager.py schedule-set --name redis-standby-1 --timezone Asia/Kolkata \
  --rules-json '[{"days_of_week":["mon","tue","wed","thu","fri"],"start_time":"09:00","stop_time":"18:00"},
                 {"days_of_week":["sat","sun"],"start_time":"10:00","stop_time":"14:00"}]'
```

**View or remove it:**

```
python instance_manager.py schedule-show --name redis-standby-1
python instance_manager.py schedule-clear --name redis-standby-1
```

`schedule-clear` just turns off automation for that node — it goes back to pure manual `start`/
`stop` control, nothing about the node itself changes.

**Run the scheduler** — nothing above actually starts/stops anything by itself until this runs:

```
python instance_manager.py poll
```

Runs forever, checking every onboarded node's schedule every 15 seconds (`--interval-seconds`
to change it) and starting/stopping whatever is due. A check only reads the local database; the
starts and stops themselves run on separate workers, so a slow start (or a long post-start check)
never delays the checks, and a check also runs the moment any start or stop finishes. A scheduled
action therefore fires within about 15 seconds of its time. Stop it with Ctrl-C: it takes no new
work and waits for any start or stop already running to finish. In production,
run it under a supervisor (e.g. a systemd unit with `Restart=always`) so it comes back up on its
own after a crash or reboot — the same way you'd run any other long-lived process. Prefer cron
over a supervisor? `poll --once` runs exactly one check and exits — schedule that on whatever
interval you'd otherwise poll on.

Every schedule edit takes effect on the next check (within about 15 seconds) automatically —
there's no separate "reload" step, and nothing to restart.

**Large fleets and catching up.** `poll` runs due starts and stops in parallel — up to 10 at a
time by default (`--max-parallel`, up to 50) — so a whole fleet due at 09:00 is started together
rather than one after another. A start or stop stays due for an hour after its scheduled time
(`--window-seconds`, default 3600) for as long as nothing has happened to that node since then.
That means:

- if `poll` was briefly down or a check ran long, the action still happens when it's back;
- if a start fails (for example, the region is temporarily out of capacity for that plan), it's
  tried again after a pause — 1, 2, 5, then every 10 minutes — until it succeeds or the hour is
  up — and each failure
  shows up in `history` and makes `poll --once` exit non-zero, so your monitoring sees it;
- a deliberate manual action after the scheduled time is always respected: stop a node by hand
  at 09:30 and the scheduler won't start it again until its next scheduled start.

A node that's already in the scheduled state (already running at its start time, say) is simply
left alone. When two scheduled times fall within the same hour, the more recent one wins.

How many to run at once is a balance: higher finishes sooner, but every start/stop makes Linode
API calls and needs capacity in the region at the same moment. For very large fleets, spreading
start times a few minutes apart across groups also helps.

**Linode API rate limits.** All parallel workers share one request budget, 10 requests per second
by default (`LINODE_API_MAX_REQUESTS_PER_SECOND`; `0` removes the cap). If Linode does answer
"too many requests" (HTTP 429), every worker pauses for as long as the API asks (its
`Retry-After`), then carries on; other temporary errors are retried with increasing, randomized
delays so workers don't all retry at the same instant. A request that creates something (such as
a new instance) is only ever re-sent after a 429 — never after a gateway error or a dropped
connection, where it might already have gone through — so a retry can't create a duplicate. If
rate limiting persists long enough that a start or stop still fails, it's recorded like any other
failure and tried again on the next check within the catch-up window. Seeing many 429s in the
`poll` output? Lower `--max-parallel` or the request cap.

**Known limitations, for now:**
- If `poll` is down for longer than the catch-up window (default one hour), a scheduled action
  from before the outage is missed, not caught up — acceptable for a cost-scheduling tool (a late
  start/stop costs at most a little extra compute), by deliberate design choice.

### 8.6 Groups — one schedule for several nodes at once

Have a set of nodes that should all follow the same schedule (e.g. a whole "Dev Environment")?
Create a group instead of setting the same schedule on each node individually — one edit updates
all of them.

**Create a group and give it a schedule:**

```
python instance_manager.py group-create --name "Dev Environment" --timezone Asia/Kolkata
python instance_manager.py group-schedule-set --group-name "Dev Environment" \
  --timezone Asia/Kolkata --days mon,tue,wed,thu,fri --start-time 09:00 --stop-time 18:00
```

`group-create` makes an empty group (name + an initial timezone); `group-schedule-set` gives it
its actual rules — the same `--days`/`--start-time`/`--stop-time` or `--rules-json` shape
`schedule-set` uses (§8.5), just applied to the whole group at once. **`--timezone` is required
on every `group-schedule-set` call, not just at creation** — it's also how you change a group's
timezone later: just re-run `group-schedule-set` with the new one. Leaving it off isn't an
option that "keeps the existing timezone" — there's no such shorthand, `--timezone` always sets
what the group's rules are resolved against from that point on.

**Add nodes to it** (also works to move a node from one group to another):

```
python instance_manager.py group-add --name redis-standby-1 --group-name "Dev Environment"
python instance_manager.py group-add --name redis-standby-2 --group-name "Dev Environment"
```

**A node's own individual schedule (§8.5) always wins over its group's**, if it has one — the
group only governs a node that has no individual schedule of its own. This lets you put a node
in a group for the common case, then give it its own schedule later if it ever needs different
hours, without having to remove it from the group first.

**View or manage the group:**

```
python instance_manager.py group-show --group-name "Dev Environment"    # schedule + members
python instance_manager.py group-list                                    # every group you have
```

**Remove a node from its group:**

```
python instance_manager.py group-remove --name redis-standby-1
```

If that node has no individual schedule of its own, this asks what to do — copy the group's
current rules into a new individual schedule for it (so its hours don't change), or leave it
manual-only. Skip the prompt with `--copy-schedule` or `--keep-manual`. If it already has its own
individual schedule, nothing to ask — it was already governed by that, not the group, and removal
just makes that explicit.

**Delete a group entirely:**

```
python instance_manager.py group-delete --group-name "Dev Environment"
```

Refuses if the group still has members — `group-remove` each one first. This is deliberate: it
guarantees a group deletion can never silently leave a node with no schedule as a side effect.

Run `poll` the same way as for individual schedules (§8.5) — one poller enforces both individual
and group schedules together, and a group also survives a total local database loss via
`rebuild` (§8), the same tag-based disaster-recovery guarantee everything else in this tool has.

#### Start order between groups (dependencies)

Some nodes only work if another set of nodes is already up — application servers that need their
database, for example. If both groups start at 9am, the app servers can finish booting first,
fail to connect, and stay broken. Tell the tool about the dependency and it orders them for you:

```
python instance_manager.py group-depends --group-name app --on db
```

With that in place:

- **Starting:** when `app`'s members are due to start, the scheduler holds them ("waiting on
  dependency") until every member of `db` is running and, if `db` has a post-start hook (§8.12),
  that check has succeeded since its latest start. `app` starts within seconds of `db` being up
  and ready — straight away when the scheduler itself started `db` (it checks again the moment a
  start finishes), otherwise on the next check, at most about 15 seconds later — whatever brought
  `db` up: its own schedule, a different schedule from `app`'s, or a manual start. A chain such as
  `web` → `app` → `db` therefore runs one link after another with no idle time between them.
  Give the database group a readiness check such as `pg_isready -q` so
  "up" means the database is really accepting connections, not just that the machine booted.
- **Stopping:** the reverse — `db`'s members wait until every member of `app` is stopped, and
  stop within seconds once they are.
- The scheduler keeps rechecking every tick for as long as `poll`'s catch-up window allows
  (an hour by default), so a slow database doesn't make the app servers miss their start. If the
  database never becomes ready within that window, the app servers aren't started that day.
- The rule follows **group membership**, so it applies even to a member of `app` that has its own
  individual schedule.
- A group can depend on **several** groups at once — `app` can wait for both `db` and `cache`:

  ```
  python instance_manager.py group-depends --group-name app --on db,cache
  ```

  `app` then starts only once every member of **both** is up and ready, and neither `db` nor
  `cache` stops until `app` is down. `--on` replaces the whole list; use `--add <group>` or
  `--remove <group>` to change one entry, and `--clear` to remove them all.
- Several groups can depend on the same group (both `app` and `reports` on `db`); `db` then stops
  only after both are down. Chains of any length are allowed (`web` → `app` → `db`), including
  diamonds (`web` on `app` and `api`, both on `db`).
- A group can't depend on itself, any change that would create a loop through any path is
  refused, and a group that others depend on can't be deleted until they stop depending on it.
- **Manual `start`/`stop` is never blocked** — you're acting deliberately — but it prints a
  warning when the dependency isn't satisfied (e.g. starting an app server while the database is
  stopped).

`group-show` lists what a group depends on and which groups depend on it; `group-list` shows each
group's dependencies. Remove them with:

```
python instance_manager.py group-depends --group-name app --clear
```

In the dashboard, tick the groups in the group page's **Start order** card; over the API,
`PATCH /groups/{name}` with `{"depends_on": ["db", "cache"]}` (`[]` clears them).

**Recovery after losing the local database.** Every member of a group carries the group's full
list of dependencies in its disaster-recovery tags, so `rebuild` restores them once the groups
are back. A group with **no members** has no volume to carry tags, so on its own it couldn't be
recovered — with Object Storage configured (§8.10), every group's definition (schedule, hooks
and dependencies) is also kept there, and `rebuild` recreates any group the tags didn't bring
back, then restores the full start order. Without Object Storage, keep at least one member in
any group others depend on, or take regular `backup` snapshots.

### 8.7 Manual override — starting a node outside its own scheduled hours

If a node has a schedule (individual, §8.5, or inherited from a group, §8.6) and you `start` it
manually outside its own scheduled "on" hours, this tool automatically arms a countdown so it
doesn't stay running forever by accident:

```
python instance_manager.py start --name redis-standby-1
```

```
'redis-standby-1' is up at 203.0.113.10.
  manually started outside its scheduled hours -- auto-stops at 2026-08-25 20:00 UTC unless
  extended (`extend --name redis-standby-1`).
```

The default window is 2 hours from when you started it. `status` and `list` both show the same
countdown for as long as it's active:

```
python instance_manager.py status --name redis-standby-1
python instance_manager.py list
```

**Need more time?** Extend it explicitly — nothing extends itself automatically, you always have
to ask:

```
python instance_manager.py extend --name redis-standby-1
python instance_manager.py extend --name redis-standby-1 --hours 4    # a different window, just this once
```

Each `extend` sets the stop to `--hours` (or the 2h default) from **now** -- but never earlier
than it already was: extending by 1 hour when 3 hours are left keeps the 3 hours. To stop sooner,
just stop it. `poll` (§8.5) is what actually enforces the auto-stop — as long as it's
running (continuously, or on a cron via `--once`), an expired override gets stopped
automatically on the next tick, the same way a normal schedule does.

**Keeping a node running past today's scheduled stop.** For a node running on its schedule (its
own or its group's), `extend` skips today's scheduled stop and keeps it running for `--hours` past
it (or from now, if the stop time has already passed):

```
python instance_manager.py extend --name redis-standby-1 --hours 3
```

The scheduler holds the scheduled stop until then, and stops the node when the extension runs out.
Tomorrow's schedule is unaffected. In the dashboard: **Keep running past today's stop** on the
node's page. A manual-only node, or one with no active schedule, has nothing to extend.

**A whole group at once:**

```
python instance_manager.py extend --group-name app --hours 2
```

Every running member that follows the group's schedule keeps running 2 hours past the group's next
scheduled stop (the group page's **Keep running past today's stop** card does the same). Members
with their own schedule, stopped members and manual-only members are skipped, and listed.

**A node's own extension overrides its group's**, the same way its own schedule does, whichever was
set first. With a 10 PM stop: the group extended by 2 hours (midnight) and one member by 1 hour --
that member stops at 11 PM and the rest at midnight. A group extension skips members with their own
extension ("has its own extension"). Repeating an extension at the same level never makes it stop
earlier.

**Start order is kept.** Extending a node or a group also holds the running members of every group
it depends on (directly or through others) until the same time, when their own stop would come
earlier -- otherwise their stop would be due while the dependent group is still up, wait for it, and
could be missed. When the extension runs out, the dependent group stops first and the groups it
depends on right after it.

**What does NOT get a timer:**
- A `start` that happens to land *inside* your node's own scheduled hours — nothing to revert
  from, the schedule already agrees it should be running.
- A `start` triggered by the scheduler itself, not a person running `start`/`extend` by hand.
- **A node with no schedule at all.** If you're running a node purely manually, on purpose, this
  tool will never impose an auto-stop on it — the override system only ever applies to a node
  that has a schedule to begin with.

**Manually stopping during scheduled "on" hours needs no special handling** — the schedule just
resumes normally at its next window, whether you stopped it manually or not.

`--override-window-hours` on `start` lets you pick a different window than the 2h default for
that one start, if you already know you'll need more (or less) time:

```
python instance_manager.py start --name redis-standby-1 --override-window-hours 6
```

### 8.8 The REST API (optional) — for a dashboard, or your own scripts

Everything above works purely through the CLI. If you want to build a web dashboard on top, or
call this tool from your own automation over HTTP instead of shelling out to the CLI, run the
API server instead:

```
python instance_manager.py serve-api --host 127.0.0.1 --port 8000
```

It binds to `127.0.0.1` by default — reachable only from the same machine, unless you pass a
different `--host` or put it behind your own reverse proxy. It uses the exact same
`LINODE_API_TOKEN` from your `.env` to actually talk to Linode; nothing extra to configure there.

**Logging in.** Instead of a separate password or shared secret, the API uses "Login with
Linode" — your team logs in with the exact same credentials they already use for Cloud Manager.
One-time setup, per deployment:

1. In Cloud Manager: **Profile → OAuth Apps → Create an App**. Set its callback URL to
   `https://<your-host>/oauth/callback` (must match exactly).
2. Copy the **Client ID** and **Client Secret** it gives you into your `.env`, alongside
   `LINODE_API_TOKEN`:
   ```
   LINODE_OAUTH_CLIENT_ID=...
   LINODE_OAUTH_CLIENT_SECRET=...
   LINODE_OAUTH_REDIRECT_URI=https://<your-host>/oauth/callback
   ```

Each deployment registers its **own** OAuth App in its **own** Linode account — this tool doesn't
operate a shared login service anyone else's deployment uses, matching how everything else here
is self-hosted and independent per customer.

**For scripts, use an API token instead** (§8.13) — a login session is meant for people in a browser.

**Using it**: visiting `GET /login` in a browser starts the flow; after logging in with Linode,
`GET /oauth/callback` redirects to `/ui/?session_token=...&expires_at=...` — the web dashboard
(§8.9) picks the token up from those query parameters automatically. Calling `/login`/
`/oauth/callback` yourself from a script instead of a browser works the same way: follow the
redirect chain and read `session_token`/`expires_at` off the final URL's query string, since
there's no separate JSON response carrying them. Every other endpoint requires that token:
`Authorization: Bearer <token>`. `POST /logout` ends the session early.

**What's exposed** — the same operations the CLI has, over HTTP: instance start/stop/status/
history/schedule (`/instances/{name}/...`), the manual-override countdown (`/instances/{name}/
extend`), groups (`/groups`, `/groups/{name}/...`), and two savings numbers —
`scheduled_savings_percent` (computed instantly from a schedule's own rules) and
`actual_savings_percent` (computed from real uptime history over the trailing 7 days by default,
`?days=N` to change that) — at `GET /instances/{name}/savings` and `GET /groups/{name}/savings`.
Full interactive documentation (every endpoint, request/response shapes) is auto-generated at
`/docs` once the server is running.

**A credential a Linode login can't replace**: the poller (`poll`, §8.5) has to keep running with
no one logged in, so it — and every API call the server itself makes to Linode — still goes
through your deployment's own `LINODE_API_TOKEN`, exactly as before. Logging in with Linode only
ever proves *who* is calling this API; it's never used to talk to Linode directly.

### 8.9 The web dashboard (optional) — a browser UI on top of the REST API

If you'd rather click around than script against the REST API directly, build the dashboard once
and it's served automatically by the same `serve-api` process:

```
cd web
npm install
npm run build
```

That produces `web/dist/`; the next time you run `serve-api`, visiting `https://<your-host>/` (or
`/ui/`) in a browser shows the dashboard instead of the plain API-docs pointer. Nothing else to
configure — it talks to the same API server it's served from, using the same OAuth setup as
§8.8.

**What it covers**: log in with the same "Login with Linode" flow §8.8 describes (a "Log in with
Linode" button, instead of visiting `/login` by hand); an Onboard page that picks an existing
Linode instance from your account and brings it under management (including a one-time SSH
password/key field for a node that doesn't yet trust this deployment's own key — it's used only
for that one attempt and never stored, see §3 above) — and, if that instance still
needs Path B migration (§6) first, a guided, three-step wizard for it, deliberately never printing
a `dd` command up front: (1) you run `lsblk` in
Lish and paste the output back — the page identifies which device is your original disk and which
is the new, empty volume purely by matching sizes against the real source-disk and
destination-volume sizes it already knows from the Linode API (entirely in your own browser — no
external service, nothing sent anywhere for this check), and shows you its best guess to confirm
or correct via two dropdowns before anything is built; (2) only once you've confirmed which device
is which does it build the actual `dd` command, with a working Copy button; (3) after you run it,
you paste `dd`'s own summary output back, and the page checks it for a clean completion (matching
"records in"/"records out" counts, no I/O error text) before letting you continue — a command that
doesn't look like it finished cleanly blocks the next step outright, with the reason shown. This
is an early, best-effort check on top of the real safety net, not a replacement for it — the
actual proof that the migration worked is still the root-device identity check performed over SSH
once you click "finish migration" (the CLI takes a different route to the same safety: its
printed copy command identifies the two disks itself and refuses if it can't, §6.2). A list of every onboarded
instance with its current status; a per-instance detail page for start/stop/extend, viewing and
editing its individual schedule, its scheduled-vs-actual savings percentages, its recent event
history, and two distinct one-way actions in its own separate cards — **Offboard** (permanently
decommissions the node on Linode too — releases the reserved IP, optionally deletes volumes, see
the `offboard` CLI section above) and **Remove from tracking** (the `deregister` CLI command's
dashboard equivalent — removes local tracking only, never touches the node itself, for a record
that's simply wrong rather than a node you're actually done with); a group list and per-group
detail page for the same schedule/savings view plus membership management. It's a thin client
over the REST API in §8.8 — every action it takes is one of that API's own endpoints, nothing the
dashboard can do that the CLI/API couldn't already do directly.

### 8.9.1 Activity, Logs and Console — seeing and running things from the dashboard

Three dashboard pages show what the backend is doing, without opening a terminal on the
scheduler host:

- **Activity** — everything the backend did and why, newest first, updating live: each start,
  stop, onboard, offboard, migration step and hook run (scheduled, manual or from the dashboard),
  every warning and error they reported, each scheduler decision (fired, waiting on a dependency,
  failed), and every console command with who ran it. Filter by level, source or text. A banner at
  the top says when the scheduler last checked in, and turns red if it stops — so a schedule that
  didn't fire because the scheduler wasn't running is obvious at a glance. Each instance's page
  has the same feed for just that instance ("Activity log"). Entries are kept for 30 days
  (`ACTIVITY_LOG_RETENTION_DAYS` in `.env`).
- **Logs** — the raw output of the scheduler (`poll`), the API/dashboard server and backups,
  tailed live, plus the console's own log and the database file's size. Each assisted migration
  copy's Rescue Mode console session is listed there too (**Migration console sessions**), one per
  instance, readable as plain text and updating live while a copy runs. A session can be deleted
  there (**Delete session**) and is removed automatically after the retention period. Engine messages appear in
  whichever service ran the operation. The same files are on disk under `state/logs/` (rotated at
  5 MB, three old files kept), so they work identically on a VM and on Kubernetes.
- **Console** — runs this tool's own commands on the scheduler host with live output: `list`,
  `status --name web-1`, `history`, `start`, `stop`, `reset-host-key`, `rebuild`, `backup` and
  the rest. It is not a shell: only this tool's commands run, so the console can't read the Linode
  API token or the SSH key, or run other programs. A command that normally asks for confirmation
  needs `--yes`. The long-running services (`poll`, `serve-api`), `restore`, SSH key backup and
  creating API tokens aren't available there. The console is for dashboard logins only (never API
  tokens), and every command is recorded on the Activity page and in the console log.

### 8.10 `backup` — scheduling your own whole-system backups

**Where backups go, and whether they work: `backup-config`.** `install.sh` asks for Object Storage
settings and sets up a scheduled `backup` (hourly by default). Without Object Storage, backups stay
on the same host -- lost with it -- so set it up if you skipped it, and check on it from time to
time:

```
python instance_manager.py backup-config                       # status and the last backup's result
python instance_manager.py backup-config --bucket my-bucket \
    --endpoint https://in-maa-1.linodeobjects.com               # prompts for the access/secret key
python instance_manager.py backup-config --test                 # write/read/delete a test object
python instance_manager.py backup-config --disable              # remove the Object Storage settings
```

New settings are tested (a small test object is written, read back and deleted) and saved to
`.env` only if the test passes; the secret key is never shown again. A running scheduler and API
pick up the change within seconds, no restart needed. The dashboard's **System backup** page does
the same, plus **Back up now** and the last backup's result.

Every stop/onboard already backs that one node up automatically (see the disaster recovery
section of the Definitive Guide) — `backup` is a separate, explicit command for taking a backup
of *everything at once*, on your own schedule, rather than waiting for individual nodes to be
touched:

```
python instance_manager.py backup --backup-dir /var/backups/instance-scheduler
```

This does two things every time it runs: if Object Storage is configured (see the Operations
Guide), it re-syncs every currently-onboarded node's own Object Storage record in one pass, and
uploads a single, consistent snapshot of your entire local database there too — covering
schedules and groups, which the per-node records don't. If `--backup-dir` is given, it also
writes that same snapshot to a local file. Give it one, the other, or both; it refuses with a
clear error if neither is available, since there'd be nothing to actually do.

Run it on whatever schedule matches your own risk tolerance — a daily cron entry is a reasonable
default:

```
# /etc/cron.d/instance-scheduler-backup
0 3 * * * root cd /opt/instance-scheduler && .venv/bin/python instance_manager.py backup --backup-dir /var/backups/instance-scheduler >> /var/log/instance-scheduler-backup.log 2>&1
```

Exits non-zero if anything didn't complete (a per-node re-sync failure, either snapshot
destination failing) — check the exit code if you're wiring this into your own monitoring rather
than just reading the log. Each run also saves the trusted SSH host keys of your nodes (the tool's
own `state/known_hosts`), to Object Storage and next to the local snapshot.

**If you install with `install.sh`** (see `DEPLOYMENT.md`), an hourly backup timer is set up for you.

**Backup on every change.** With Object Storage configured, the scheduler also uploads a fresh
database snapshot within about a minute of any change (`full-backups/instances-continuous.db`,
overwritten each time), so a lost host loses at most about a minute of changes. Old timestamped
snapshots are pruned after each hourly backup: everything from the last 48 hours, then one a day
for 30 days (`LIS_BACKUP_KEEP_ALL_HOURS`, `LIS_BACKUP_KEEP_DAILY_DAYS`). The newest snapshot and the
continuous one are never deleted.

**The deployment SSH key.** Every managed node trusts exactly one key, the deployment's own, so it's
kept recoverable automatically: encrypted in the database (and therefore every snapshot) and in
Object Storage, with `LIS_SSH_KEY_PASSPHRASE` from `.env` (generated at install, shown once); its
public half under your Linode profile's SSH keys; and the passphrase in a private StackScript in
your Linode account (`lis-recovery-…` — don't run or delete it). A replacement host fetches the
passphrase with the API token it needs anyway, so nothing extra has to be remembered. Anyone with a
full-access Linode token could already get root on every instance (rescue mode, root password
reset, Lish), and the passphrase opens nothing without the encrypted key, which needs your Object
Storage keys or a snapshot. Check it any time:

```
python instance_manager.py ssh-key-protect
```

**Your own encrypted copy of the settings.** First create an age key on your own computer (not the
server): install age (`brew install age` on macOS, `sudo apt install age` on Ubuntu/Debian,
`sudo dnf install age` on Fedora/RHEL, `winget install --id FiloSottile.age` on Windows), then run
`age-keygen -o lis-age-key.txt`. It prints `Public key: age1…`; keep `lis-age-key.txt` (the secret
part) in your password manager. Set that public key and the scheduler keeps the whole `.env` and the deployment SSH
key in your bucket, encrypted so only your `key.txt` opens them — this host can write them but not
read them back. They're re-uploaded whenever either changes:

```
python instance_manager.py env-backup --set-recipients age1...      # once; also on the System backup page
python instance_manager.py env-restore --identity-file key.txt --ssh-key keys/deploy_key   # on a new host
python instance_manager.py env-restore --identity-file key.txt --print   # just show them (e.g. for a Kubernetes Secret)
```

On a new host, `sudo ./install.sh recover --env-from-backup --age-identity key.txt` asks only for
the bucket settings.

**Moving to a new host.** If the machine running this tool is lost, `sudo ./install.sh recover` on a
fresh one restores everything before starting anything — see `DEPLOYMENT.md` §8.1. The pieces it
uses are also available directly:

```
python instance_manager.py ssh-key-restore --save-passphrase   # the deployment SSH key (passphrase from your Linode account)
python instance_manager.py restore --list     # the snapshots in your bucket
python instance_manager.py restore            # newest snapshot + trusted host keys (or --snapshot / --from-file)
python instance_manager.py rebuild            # adds anything newer than the snapshot, from tags and Object Storage
python instance_manager.py heal               # adopts instances the snapshot doesn't know about (§8.15)
python instance_manager.py schedule-catch-up  # nodes left off their schedule by the outage (§8.15)
```

`restore` refuses to overwrite a database that already has instances or groups unless you pass
`--force`, which moves it aside rather than deleting it. A snapshot from an older version is
upgraded automatically. `rebuild` also finds a node whose tags were removed, through its Object
Storage record.

### 8.11 High availability — why this isn't an always-on active-active or active-passive setup

Run exactly **one** `poll` process at a time, on one machine — never two running simultaneously,
and never a hot standby waiting in the wings. That's not a corner cut; it's the right fit for
what `poll` actually does.

`poll` doesn't serve live requests — it checks every schedule on a short, fixed interval, and a due
action stays due for up to an hour (§8.5). If that process is briefly down (a crash, a host
reboot, a deploy), the worst case is a due action firing a little late once your process
supervisor brings it back up — nothing waits on it
in real time, so nothing downstream notices. Manual `start`/`stop` keeps working the entire time
regardless, since it never depends on `poll` being up.

An **active-active** setup (two schedulers running at once) would trade that harmless delay for
a real correctness risk instead: two processes with no genuine coordination between them could
race to act on the same node at the same moment, or have one fire a start while the other is
mid-firing a stop for it — a new failure mode, introduced to solve a problem (a few seconds of
downtime) a plain restart already solves for free. An **active-passive** setup avoids the double-
fire risk, but only by adding its own always-on standby plus real failure-detection machinery
(health checks, a way for the standby to agree it should take over, a shared view of state) —
infrastructure that has to be running and paid for continuously, to protect against an outage a
single supervised process already recovers from in seconds.

That's also, directly, why it saves you money rather than costs you any: this whole tool exists
to stop you paying for compute that isn't earning its keep. Running a second, always-on
scheduler instance as insurance against a multi-second restart would mean paying for exactly
that kind of idle, always-on capacity — just moved onto this tool's own infrastructure instead
of your managed fleet. The approach actually used costs nothing extra: one process, under
whatever process supervisor your OS already has built in, on the one machine you're already
running this from. If the underlying data is ever lost too, `rebuild` and the Object Storage
backup (§8.10, above) recover it without needing a second live system standing by either — see
the Definitive Guide's Deployment Model chapter for the fuller comparison.

---

### 8.12 Hooks — run your own commands before a stop and after a start

Some services want a step of their own around a stop/start cycle. A database might need a clean,
checked shutdown before its instance is deleted, and you may want to confirm it's actually
accepting connections again after the instance comes back. Hooks let you attach your own commands
to both moments:

- **Pre-stop hook** — runs once on the node, right before it's shut down and deleted.
- **Post-start check** — runs on the node after it's reachable again, as a readiness check.

Both run as `root` over the same SSH connection this tool already uses, and apply to every stop
and start: scheduled, manual, through the API or dashboard, and the automatic stop at the end of
a manual-override window.

**Every stop already shuts the operating system down gracefully** before deleting the instance,
so services managed by systemd (PostgreSQL, Redis, and so on on a normal install) are stopped
cleanly even without a hook. A pre-stop hook adds an explicit step whose result is checked, and
the option to keep the node running if it fails.

```
python instance_manager.py hooks-set --name pg-1 \
    --pre-stop "pg_ctlcluster 16 main stop -m fast" \
    --post-start "pg_isready -q"
```

A hook can be given two ways:

- **A command** — run as-is on the node. That's either an inline command (`pg_isready -q`) or the
  path of a script you already ship on the node, for example with your own image or configuration
  management (`/opt/app/hooks/before-stop.sh`). The script itself stays on the node's OS volume,
  which survives every stop/start.
- **An uploaded script** — the script text itself, stored by this tool. Each time the hook runs,
  it's copied to a temporary file on the node, run (its own `#!` line picks the interpreter), and
  deleted afterward. Up to 1 MB.

```
python instance_manager.py hooks-set --name pg-1 --pre-stop-script ./before-stop.sh
```

Options (only the ones you pass change; anything already set is kept):

| Option | Meaning |
|---|---|
| `--pre-stop COMMAND` | The pre-stop hook, as a command or a path on the node. |
| `--pre-stop-script FILE` | The pre-stop hook, as an uploaded script (read from a local file). |
| `--pre-stop-timeout SECONDS` | How long it may run (default 300, max 3600). |
| `--pre-stop-on-failure abort\|continue` | What happens if it fails — see below (default `abort`). |
| `--post-start COMMAND` | The post-start check, as a command or a path on the node. |
| `--post-start-script FILE` | The post-start check, as an uploaded script. |
| `--post-start-timeout SECONDS` | Total time allowed for the check to pass (default 600, max 3600). |
| `--clear-pre-stop` / `--clear-post-start` | Remove just that one hook. |

Hooks are configured per node or per group. `--group-name dbs` instead of `--name pg-1` sets them
for every member of a group. A node's own hook overrides its group's **for that hook type only**,
so a node can set its own post-start check and still use the group's pre-stop hook.
`hooks-show --name pg-1` shows both its own hooks and the ones that actually apply, including
which group each inherited one comes from.

A command can be up to 4096 characters; for anything longer, upload it as a script or ship it on
the node and give its path. A timed-out hook's command is not killed on the node — if you need a
hard limit there, wrap your command in `timeout`.

**What happens if a hook fails.** A hook fails when its command exits with a non-zero code, runs
past its timeout, or the node can't be reached over SSH to run it at all.

- **Pre-stop hook fails, policy `abort` (the default):** nothing is shut down or deleted. The node
  keeps running (and billing), the stop is recorded as failed in `history`, the command exits
  non-zero, and the end of the hook's own output is printed so you can see why. It's not retried
  on its own: fix the cause and run `stop` again, or use `stop --skip-hooks` to stop without the
  hook. If your hook does several things, note that it may have done some of them before
  failing (for example, stopped the database but then exited non-zero) — this tool doesn't undo
  that, so make your script restore what it changed if a partial run matters.
- **Pre-stop hook fails, policy `continue`:** a warning is recorded and the stop goes ahead.
- **Post-start check:** it's retried every 15 seconds until it succeeds or its timeout runs out,
  so "not ready yet, 20 seconds after boot" is normal and simply retried. If it never succeeds,
  the node is **left running** — it's never stopped or deleted automatically for this — and
  the start is reported as failed so someone can look at it. `status` shows when the last check
  failed. Once you've fixed the cause, re-run the check without restarting:

```
python instance_manager.py hooks-run --name pg-1 --post-start
```

`start --skip-hooks` starts a node without running its post-start check, and
`stop --skip-precapture` (for a node that can't be reached over SSH) skips the pre-stop hook too,
since there's no way to run it. Both are recorded in the node's hook history.

`hooks-run --name pg-1 --pre-stop` runs the pre-stop hook on demand. It asks for confirmation
first (`--yes` skips the prompt), because the node is **not** stopped afterward, so whatever the
hook stops stays stopped until you restart it.

**History.** Every hook run (result, exit code, the end of its output) and every hook change (who
changed it, when, and what it was changed to) is recorded. The dashboard's Hooks card on each node
shows this, and the API exposes it at `GET /instances/{name}/hook-events`.

**Recovering hooks after losing the local database.** Hooks are kept in the local database
alongside schedules and groups (so `backup`, §8.10, covers them), and are also recorded where
`rebuild` can find them with no local database at all:

- **With Object Storage configured (recommended):** every hook — command, script path, or uploaded
  script, for a node or a group — is stored in your bucket, and the node's OS volume gets a short
  tag pointing at it (`hk-…` for its own hooks, `hkg-…` for its group's). `rebuild` reads the tags,
  fetches each hook, checks that its content matches the fingerprint in the tag, and restores it
  on the right node and group. A hook is stored in Object Storage *before* it takes effect: if that
  write fails, the change is refused and nothing changes, so a configured hook is always
  recoverable.
- **Without Object Storage:** a short command (up to about 40 characters, such as a script path)
  is written directly into the node's tags and restored the same way. A longer command or an
  uploaded script can't fit in a tag, so it's kept locally only — `hooks-set` warns when that
  happens, and only a `backup` snapshot can bring it back.

Hooks are never stored on the node by this tool (an uploaded script only exists there while it
runs), and the fingerprint check means a hook can't be swapped for different content in your bucket
or by editing a tag without `rebuild` refusing to restore it.

### 8.13 Manual-only nodes and scripting

Some nodes shouldn't follow any schedule at all — you start and stop them yourself, or from your
own scripts and pipelines. This section covers making a node manual-only, and everything a script
needs: a credential, predictable results, and acting on a whole group in one call.

#### Manual-only nodes

```
python instance_manager.py set-mode --name build-runner-1 --manual
```

A manual-only node is never started or stopped by the scheduler, and a manual start never arms an
auto-stop timer. Start and stop it with `start`/`stop`, the dashboard, or the API. It shows as
`[manual-only]` in `list` and has a "Manual-only" card on its dashboard page.

A manual-only node can't have a schedule, and can't be in a group that has a schedule:

- `set-mode --manual` refuses if the node has its own schedule (`schedule-clear` it first) or is in
  a group with a schedule (`group-remove --keep-manual` it first).
- `schedule-set`, `group-add` into a scheduled group, and `group-schedule-set` on a group with a
  manual-only member are refused the same way.
- It *can* be in a group without a schedule — useful for sharing hooks (§8.12) or a start order
  (§8.6) with other nodes.

Switch it back with `set-mode --name build-runner-1 --auto`. The setting is saved in the node's
disaster-recovery tags, so `rebuild` restores it.

#### Starting or stopping a whole group

```
python instance_manager.py start --group-name dev
python instance_manager.py stop  --group-name dev --yes
```

Every member is acted on at once (up to `--max-parallel`, default 10), and each succeeds or fails
on its own — the output lists every member and its outcome. If the group has a start order
(§8.6), add `--with-dependencies` to bring up the whole chain in order — `start` starts the groups
it depends on first and waits for them to be ready (post-start checks included) before starting
this one; `stop` stops the groups that depend on it first. A stage that doesn't fully succeed stops
the chain, and the later groups are reported as `skipped`.

#### Start order for one-off actions

By default a manual `start`/`stop` goes ahead even when the node's start order isn't satisfied,
with a warning. Add `--respect-dependencies` to refuse instead — the right choice for unattended
scripts:

```
python instance_manager.py start --name web-1 --respect-dependencies
```

#### Exit codes and JSON output

`start` and `stop` (single node or group) exit with:

| Code | Meaning |
|---|---|
| 0 | Done, or already in that state |
| 1 | Failed (see the message) |
| 2 | Command-line usage error |
| 3 | Busy — another operation is running on this node; retry later |
| 4 | Security warning — the node's SSH host key changed; don't retry blindly (see `reset-host-key`) |
| 5 | Refused by `--respect-dependencies` — the start order isn't satisfied yet |

Add `--json` to `start`, `stop` or `list` to get one JSON object on stdout (progress messages go
to stderr). `stop --json` needs `--yes`. `status` already prints JSON.

#### API tokens

Scripts calling the REST API (§8.8) use an API token rather than a browser login:

```
python instance_manager.py api-token-create --name ci-deploy --scopes read,operate --groups dev \
  --expires-days 90
```

The token is printed once — store it in your secret manager. Send it as
`Authorization: Bearer <token>`. You can also create, list and revoke tokens on the dashboard's
**API tokens** page. Only a hash of each token is kept, so a lost token can't be shown again;
revoke it and create a new one. With Object Storage configured, each token's record (hash, scopes,
limits, expiry, revocation; never the token) is also written there on every create and revoke,
so tokens survive a lost database and a restore from an older snapshot never brings a revoked
token back. If that write fails you'll see a warning; run `backup`, or revoke the token again, to
retry.

Scopes come in two sizes. The four **bundles** cover common roles:

| Bundle | Allows |
|---|---|
| `read` | List, status, history, savings, activity, schedules, groups, hooks (view only) |
| `operate` | Start, stop, extend, run a hook now, group start/stop |
| `configure` | Schedules, groups, group membership, start order, manual-only mode, VPC address |
| `admin` | Everything, including changing hooks (they run as root), onboard/offboard, migration, logs, and managing tokens |

For a token that should do exactly one thing, give it **single operations** instead (or as
well) — for example a nightly job that may only stop nodes in one group:

```
python instance_manager.py api-token-create --name nightly-stop --scopes instances:stop \
  --groups dev
```

| Operation | Allows |
|---|---|
| `instances:list` | List nodes |
| `instances:status` | One node's status |
| `instances:history` | One node's history |
| `savings:read` | Savings figures (node or group) |
| `activity:read` | The activity log |
| `logs:read` | Service log files |
| `instances:start` / `instances:stop` | Start / stop a node |
| `instances:extend` | Extend a manual-override timer |
| `instances:clear-lock` | Clear a stuck in-progress mark (refused while a start/stop really runs) |
| `schedules:catch-up` | Start/stop nodes that missed their schedule, after review |
| `groups:start` / `groups:stop` | Start / stop a whole group |
| `schedules:read` / `schedules:write` | Read / set or clear a node's schedule |
| `groups:read` | List and view groups |
| `groups:write` | Create or delete groups, set a group's schedule |
| `groups:membership` | Add a node to, or remove it from, a group |
| `dependencies:write` | Set a group's start order |
| `mode:write` | Make a node manual-only or schedulable |
| `hooks:read` / `hooks:write` / `hooks:run` | Read hooks / set or clear them (they run as root) / run one now |
| `instances:onboard` | List account instances, reserve an IP, onboard |
| `instances:migrate` | Migrate a node off local disk |
| `instances:offboard` | Offboard a node or remove it from tracking |
| `instances:vpc-address` | Move a stopped node to another VPC address |
| `tokens:manage` | Create, list and revoke tokens — never one wider than itself |
| `system:read` | Backup, key-protection, restore-test, lease, watchdog and alert status |
| `system:backup` | Run a backup, a restore test, the settings backup, key protection |
| `system:configure` | Heal, take over the scheduler lease, Shutdown Watchdog, the age key for the settings backup |

Settings that hold credentials -- Object Storage keys, the SMTP password -- can only be changed with
an `admin` token or a dashboard login.

`python instance_manager.py api-token-scopes` prints this list. A request without the scope it
needs is refused with `403` naming the missing scope; anything not on this list needs `admin`.

`--instances` and `--groups` limit a token to particular nodes and/or groups (a group covers its
current members). A limited token only sees those nodes and groups in lists, and can only read
operations it started itself. Every action taken with a token is recorded in `history` under
`token:<name>`. `api-token-list` shows each token's scopes, limits, expiry and when it was last
used; `api-token-revoke --name ci-deploy` stops it working immediately.

#### Waiting for the result over the API

`POST /instances/{name}/start` and `/stop` normally return at once with an operation id to poll
(`GET /operations/{id}`). Add `?wait=true` to get the finished result in the same response —
one call per action from a script:

```
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"respect_dependencies": true}' \
  "https://<your-host>/instances/web-1/start?wait=true"
```

The body is the same as a finished `GET /operations/{id}`: `status` (`done` or `error`), `result`
(the outcome, e.g. `started`), and any `warnings`. An HTTP 409 means it was refused — busy, or the
start order isn't satisfied with `respect_dependencies`. A wait longer than an hour returns 202
with the still-running operation to keep polling. Group actions are
`POST /groups/{name}/start|stop` with `{"with_dependencies": true}` (optional), and also accept
`?wait=true`. Switch a node to manual-only over the API with `PATCH /instances/{name}` and
`{"schedule_mode": "manual"}`.

### 8.14 Holidays — keep everything down on a date it would normally run

Tomorrow is a holiday and nothing should start:

```
python instance_manager.py holiday-add --date 2026-10-12 --note "Dussehra"
```

On that date the scheduler skips every node's scheduled starts. Scheduled stops still happen, so
anything left running the evening before is shut down as usual, and the next normal day starts on
schedule -- nothing to remember to undo.

- **A range:** `holiday-add --date 2026-12-24 --to 2026-12-31`.
- **Only some nodes:** `--group-name dev` (that group's members) or `--name web-1` (one node).
- **The date is each schedule's own local date** -- a group on `Asia/Kolkata` uses the Indian date.
- **Manual and API starts are never blocked** -- you can still start a node by hand on a holiday.
- **Changed your mind?** `holiday-remove --date 2026-10-12` (same `--group-name`/`--name` as when
  added). A start skipped earlier that day catches up within the usual hour.
- `holiday-list` shows what's coming up (`--all` includes past dates).

**Groups or nodes that must keep running on account-wide holidays.** A holiday added without
`--group-name`/`--name` is account-wide and applies to every node by default. A group can opt out:

```
python instance_manager.py holiday-settings --group-name prod --ignore-account-wide
```

Its members then start as usual on account-wide holidays. Holidays added for that group (or for
one of its nodes) still apply. A node's own setting always overrides its group's, the same way its
own schedule does:

```
python instance_manager.py holiday-settings --name web-1 --follow-account-wide   # skips them even if its group runs
python instance_manager.py holiday-settings --name db-1 --ignore-account-wide    # runs on them, with or without a group
python instance_manager.py holiday-settings --name web-1 --inherit               # back to the group's choice
```

`holiday-settings --group-name prod` or `--name web-1` without a choice shows the current setting;
`status` shows the setting in effect for a node and where it comes from.

In the dashboard: the **Holidays** page; each group's page has a **Holidays** card (its upcoming
holidays, adding a date or range for that group, **Run on account-wide holidays**, and **Skip
tomorrow**); each node's page has an **Account-wide holidays** choice. A node whose day is a
holiday says so on its page. Holidays and these settings are included in backups and come back
with `restore`/`rebuild` (the settings are also kept in tags on each node's OS volume and in the
group's Object Storage record).

### 8.15 Recovering safely: self-healing, missed schedules and alerts

**Self-healing (`heal`).** The scheduler compares every node with your account every few minutes
and fixes its own records where the account proves them wrong — for example after a database was
restored from a snapshot taken before a node was started:

- the node's OS volume is attached to an instance its record doesn't point at → that instance is
  the node; it's recorded (never a second one created, and a `stop` stops that one);
- the record points at an instance that's gone and nothing holds the OS volume → stopped;
- an instance with the node's label, no disks and none of your volumes, at least 15 minutes old →
  left behind by a start that never finished; it's deleted (nothing that can hold data ever is);
- a node marked "in progress" while no start/stop is running for it → cleared.

`start`/`stop` make the same check for the node they act on. Run it yourself any time:

```
python instance_manager.py heal                 # every node; --name to limit
```

Volumes and reserved IPs are never deleted by this.

**Missed schedules (`schedule-catch-up`).** The scheduler acts on a start or stop for an hour after
it was due. After a longer outage (for example while the scheduler host was being replaced), nodes
are left as they were until their next scheduled time. These are listed — on the dashboard's
Instances page and here — but nothing is started or stopped until you approve it:

```
python instance_manager.py schedule-catch-up                     # list
python instance_manager.py schedule-catch-up --apply             # act on them, after confirming
python instance_manager.py schedule-catch-up --apply --include-changed-by-hand --yes
```

A node someone started or stopped by hand after the scheduled time is marked as such and left
alone unless you include it. Stops run before starts, following each group's start order.

**Alerts.** Set a webhook (`LIS_ALERT_WEBHOOK_URL` — Slack and Teams incoming webhooks work as is)
and/or email (`LIS_ALERT_EMAIL_TO` plus the `LIS_ALERT_SMTP_*` settings; see `DEPLOYMENT.md`
§8.2) to hear about: the scheduler not checking in for 10 minutes, backups failing, the deployment
SSH key not stored in every place, or nodes staying off their schedule. Each is sent when it
starts, every 6 hours while it lasts, and once when resolved. Linode blocks outbound SMTP ports on
new accounts until support opens them.

```
python instance_manager.py alerts-test        # send a test alert
python instance_manager.py alerts-check       # what needs attention now (exit 1 if anything)
```

Alerts also cover a failed daily restore test of the backups, the Linode API token being rejected
or expiring within 14 days, the disk holding the database nearly full, and Shutdown Watchdog being
off on the scheduler's host.

**Setting up email alerts.** On the dashboard's System backup page, fill in the Alerts form: who
to send to, your SMTP server, port, security, user and password, then **Save** and **Send test
alert**. Or from the command line:

```
python instance_manager.py alerts-config --email-to ops@example.com --smtp-host smtp.gmail.com \
    --smtp-port 587 --smtp-user you@example.com --smtp-password     # prompts for the password
python instance_manager.py alerts-test
```

Use the SMTP server of a mail account you already have (Google Workspace: `smtp.gmail.com`, port
587, with an app password; Microsoft 365: `smtp.office365.com`, 587) or a sending service
(SendGrid, Mailgun, Amazon SES, Brevo). Linode blocks outbound SMTP ports (25, 465, 587) on new
accounts until you ask support to open them; a service that accepts port 2525 works without that.
For a webhook, `--webhook-url` (Slack: an incoming-webhook URL; Teams: a workflow webhook URL).
The running services pick the change up without a restart.

**When the whole host is gone,** nothing on it can alert you. Linode's Shutdown Watchdog (turned on
by `install.sh`; `host-watchdog --enable`) boots the host again after a crash, and
`LIS_HEARTBEAT_URL` sends a ping every minute to an uptime monitor of your choice (Healthchecks.io,
Better Stack, Uptime Kuma), which tells you when the pings stop.

**One active scheduler.** With Object Storage, the active scheduler holds a lease in the bucket. A
second scheduler (an old host coming back after you recovered onto a new one) finds the lease and
stands by -- it starts, stops and uploads nothing, and alerts. `scheduler-lease` shows who's
active; `scheduler-lease --take-over` (or the System backup page) moves it, only once you're sure
the other host is gone.

**Restore testing.** Every day the newest snapshot is downloaded and checked, and the stored key
copies decrypted, without touching the live database (`backup-verify` on demand). A damaged
database is never uploaded. Upgrading with `install.sh install` takes a snapshot first, in
`backups/pre-upgrade-<time>/`.

## 9. Costs

While a node shows `stopped`:

- **No compute charge at all** — this is the whole point.
- **A Block Storage charge, billed per GB/month, for every volume attached to that node** (OS
  volume + any data volumes). This is a flat **$0.10/GB/month** in most regions — but note it
  **scales with the size of the disk being migrated**, not with the instance's plan cost. A
  small instance's ~25GB disk costs about $2.50/month to keep as a stopped volume; a large
  dedicated instance's disk costs proportionally more, since Path B's destination volume has to
  be at least as large as the source disk (see the worked example below).
- **A small idle charge for the reserved IP**, billed whether or not it's currently attached to
  anything (Linode's published rate is around $2/month — worth confirming against Linode's
  current pricing directly, since this specific figure wasn't independently re-verified for this
  guide the way the Block Storage rate was).

Both of these are ongoing while the node exists in any state, running or stopped — they're the
price of guaranteeing the node comes back identical.

**Worked example, to make this concrete for a large instance**: a **G7 Dedicated 32x16**
(640GB local disk, $346/month running) migrated via Path B needs a destination volume of about
645GB. At $0.10/GB/month, that's **~$64.50/month** while stopped (plus the same small reserved-IP
fee any node pays) — **roughly an 81% reduction** versus paying for it running around the clock,
not free, but a real, substantial saving. Smaller instances save a much higher *percentage*,
since their disks — and therefore their stopped-volume cost — are proportionally tiny next to
their running cost.

**One current limitation worth knowing**: Linode caps a single Block Storage volume at 16TB.
This tool doesn't support splitting a migration across multiple volumes, so Path B migration
isn't currently supported for a source disk that large (it fails cleanly with a clear error
before creating anything, rather than partway through) — not a concern for any instance type
available today, but worth knowing if you're planning around very large custom storage
configurations.

---

## 10. Realistic walkthroughs

### Redis failover

You maintain two standby nodes, onboarded as `redis-standby-1` and `redis-standby-2`, both
`stopped` day to day.

Your primary Redis starts misbehaving. Rather than debugging it live:

```
python instance_manager.py start --name redis-standby-1
```

A couple of minutes later, it's up at its known, stable IP. Repoint your application at it (DNS,
config, however you normally do failover). Investigate the original primary at your leisure —
no pressure, no live production system to poke at while it's still serving traffic.

Once you've resolved the original issue and are ready to switch back:

```
python instance_manager.py stop --name redis-standby-1
```

`redis-standby-1` goes back to costing almost nothing until you need it again.

### TiDB replica pool scaling

You keep three replica nodes onboarded — `tidb-replica-1`, `-2`, `-3` — normally all `stopped`.
A traffic spike means you need more read capacity:

```
python instance_manager.py start --name tidb-replica-1
python instance_manager.py start --name tidb-replica-2
```

Both come up at their stable, known IPs and rejoin your cluster the way any TiDB replica would.
When the spike passes:

```
python instance_manager.py stop --name tidb-replica-1
python instance_manager.py stop --name tidb-replica-2
```

---

## 11. Troubleshooting

**`onboard` refuses with "is not a reserved IP."** The node's public IP hasn't been reserved
yet. If you went through §6, this shouldn't happen (it's handled automatically in
`migrate-resume`). If you skipped §6 because the node was already on Block Storage, reserve the
IP yourself first (Cloud Manager, or the API), then re-run `onboard`.

**`start` fails partway through.** The tool retries transient failures (capacity issues,
momentary API errors) automatically with backoff before giving up. If it still fails, any
partially-created instance is automatically cleaned up rather than left behind as an orphaned,
billing resource — check `status --name <name>` and try again.

**A node looks "locked" but nothing is actually running.** See `clear-lock` in §8 — but confirm
independently in Cloud Manager first that nothing is genuinely in flight before using it.

**I changed something in Cloud Manager (renamed the node, added a firewall, re-tagged it) — will
that be lost?** No — every `stop` re-captures the node's current state fresh before deleting it,
specifically so an out-of-band change like this isn't silently reverted on the next `start`.

**A node was deleted manually in Cloud Manager (or the instance was otherwise removed outside
this tool) and the registry still thinks it's `running` against a now-`404` `linode_id`, and
your local `state/` directory is otherwise intact.** Just run `stop --name <name>` (or `start`) —
this tool already detects the confirmed-404 case automatically: it resets the record straight to
a clean `stopped` state, preserving every other already-known field (network config, authorized
keys, label, tags, plan, data volumes, reserved IP), with zero manual Cloud Manager steps needed.
Reach for `rebuild --force` below only if this doesn't apply — it's a strictly worse outcome
when nothing new happens to be running against the old resources yet (see the "partial record"
case just below) than the one-step `stop`/`start` resolution above.

**Your local `state/` directory itself was lost (not just one node's registry entry) — a
disk failure, a machine rebuild, anything that wipes `instances.json` entirely — and you need
to reconstruct it from scratch.** Recover with:

```
python instance_manager.py rebuild --force
```

This rebuilds the registry entry from the disaster-recovery tags already on the surviving
OS/data volumes and reserved IP (see §8's `rebuild`). `--force` is required to overwrite a
stale existing entry, if any. Two outcomes:

- If a live instance is currently running against this node's volumes/IP (the normal case for
  a node that was simply `running` when local state was lost), the record comes back fully
  recovered and pointing at that instance — `stop`/`start` work normally right away.
- If nothing is currently running against those resources, `rebuild` can only recover a partial
  record (network config and authorized keys aren't knowable from tags alone with nothing live to
  read them from) and flags it `needs_manual_recovery` — `start`/`stop` both refuse in this state.
  Get some instance running against those volumes/IP (Cloud Manager, or by re-running whatever
  process created the node originally), then run
  `onboard --name <name> --instance-id <live-instance-id> --force` to fully re-capture the
  remaining fields and clear `needs_manual_recovery`.

**`status`/`list` shows a node as `unreachable`.** Two distinct situations land here — both
resolve the same way, just run `start` again:
- `start` created a real instance (it's real and billing) but couldn't confirm it was actually
  reachable — a slow boot, a network blip, or (see the `SECURITY WARNING` case just below) a
  host-key mismatch.
- A node that was already `running` per this tool's own records was found powered off when
  `start` last checked it — e.g. someone used Cloud Manager's "Power Off" button directly,
  bypassing this tool entirely (that's a different action from this tool's own `stop`, which
  always deletes the instance rather than just powering it down).

Either way, `start` retries against that same existing instance — rebooting it first if it's not
already running/booting — rather than creating a second one. If the instance has since been
confirmed gone entirely (a 404 — e.g. you deleted it by hand in Cloud Manager while
troubleshooting), `start`'s retry (or `stop`) resets the node to `stopped` automatically, since a
genuinely-gone instance isn't ambiguous the way an unreachable one is; run `start` again after
that to recreate it.

**`start` refuses with a `SECURITY WARNING` about a changed SSH host key, right after upgrading
this tool.** This is the one-time migration case in §8 — the node was onboarded before this
tool started verifying host keys, so nothing was on record to compare against yet. Run
`reset-host-key --name <name> --yes` once for that node (and any other pre-existing node showing
the same thing), then `start` again. If you see this on a node that's been running fine on a
current version of this tool for a while, don't treat it as routine — confirm independently
(Cloud Manager's Lish console) that nothing's actually wrong before using `reset-host-key`.

---

## 12. Support

Questions or issues with this tool: contact **Sandip Gangdhar (sgangdhar@akamai.com)**.
