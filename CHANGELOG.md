# Changelog

All notable changes to this project. Versions follow [semantic versioning](https://semver.org/).
Upgrade by checking out the new tag and re-running `install.sh` (see `DEPLOYMENT.md`), or by
moving to the new container image on Kubernetes (see `DEPLOYMENT-LKE.md`).

## v1.12.0

### Added
- Health alert `key-rotation`: an SSH key rotation that isn't finishing (a node that needs manual
  recovery and so never moves, a node that keeps failing to move, or a rotation open for 7 days)
  now raises an alert, since the old key stays trusted until it completes.
- `rotate-credentials ssh-key` lists nodes that won't move by themselves separately, with how to
  unblock them (recover with `onboard --force`, or offboard/deregister).
- Dashboard: "Restore one item from a snapshot" picks the instance or group from a searchable
  list, and still accepts a name that no longer exists (for example a deleted group).
- `SECURITY.md` and this changelog.

### Fixed
- The release copy in the backup bucket now installs offline on Python 3.10 to 3.14, including
  dependencies published only as source.
- `install.sh recover --env-from-backup`: the age identity file could not be written on hosts
  that protect files in `/tmp` (Ubuntu), and a separate bucket settings file stopped the
  recovery after `.env` had been restored.

## v1.11.1

### Added
- Health alert `node-failing`: a node whose last three or more start/stop attempts failed, with
  no success since. A node stuck as "unreachable" is now reported instead of silently waiting.

### Fixed
- The recovery drill failed right after creating its throwaway VM and could leave that VM
  running. Drill VMs are now always found and deleted by their tag, and their host keys removed.

## v1.11.0

### Added
- **Pending deletion**: offboarding a node or deleting a pre-migration backup marks the resources
  for deletion after a grace period (7 days by default, `LIS_DELETION_GRACE_DAYS`). Pending
  deletions are listed in the dashboard, can be recovered exactly as they were or deleted now, and
  survive a total database loss. `--now` deletes immediately.
- A standby scheduler (another host holds the scheduler lease) refuses changes; a host that never
  held the lease stays on standby while the bucket is unreachable.
- Clock check: an alert when the host's clock is more than 60 seconds off or not synchronized.
- Dependencies installed from a hash-locked `requirements.lock`.
- A release copy (code, built dashboard and dependency wheels) kept in the backup bucket, so a
  replacement host can install without GitHub or PyPI.
- Object Storage versioning with a 30-day rule for old versions, and `bucket-protection` to bring
  back an older version of a backup object.
- `restore-item`: restore one schedule, one node's hooks, or one group from a database snapshot.
- `rotate-credentials` for the Linode API token, the bucket key, the OAuth secret and the
  deployment SSH key (nodes move to the new key without losing access).
- `recovery-drill`: a throwaway VM installs the release copy, restores read-only, checks every
  node and is deleted. Runs weekly with `LIS_RECOVERY_DRILL_DAYS`.
- API token scopes for each new operation; the dashboard token page picks nodes and groups from
  searchable lists.

## v1.10.0

### Added
- Scheduler lease: only one scheduler acts at a time; another host stands by.
- Daily restore test of the latest backup, Shutdown Watchdog on the scheduler host, optional
  heartbeat URL for an uptime monitor, alerts for an expiring or rejected API token and a nearly
  full disk.
- A damaged database snapshot is never uploaded over a good one.
- Alert settings editable from the dashboard and `alerts-config`.

## v1.9.1

### Added
- An operator-only, age-encrypted copy of `.env` and the deployment SSH key in the bucket, and
  `install.sh recover --env-from-backup` to restore it on a new host.

## v1.9.0

### Added
- Recovery after losing the scheduler host: a database behind the account adopts the real
  instance instead of creating a second one; backups on every change; the deployment SSH key kept
  encrypted with its passphrase escrowed in your account; review and apply missed schedules after
  an outage; cancel an unfinished migration; webhook and email alerts.

## v1.8.21
- Migration console session logs can be deleted, and expire after the retention period.

## v1.8.20
### Fixed
- `install.sh --domain` no longer warns when the domain points at the host's VPC 1:1 NAT address.

## v1.8.19
### Fixed
- Pre-migration backups failed for node names longer than 23 characters, and their cleanup.

## v1.8.18
- Migration console sessions are viewable in the dashboard while and after a copy runs.

## v1.8.17
- Groups and nodes can choose to ignore account-wide holidays.

## v1.8.16
- Holidays (account-wide, per group, per node) skip scheduled starts.
- An instance's own extension takes precedence over its group's.
- Assisted migration copy over the Lish console, when a pre-migration backup is kept.

## v1.8.15
- Extend a whole group past today's stop; extending keeps the start order.

## v1.8.14
- Optional pre-migration backup and one-step rollback.
- "Keep running past today's stop" for a scheduled node.
- Backup settings from the dashboard and `backup-config`.

## v1.8.13
- The scheduler checks every 15 seconds and runs starts and stops on a worker pool, so start-order
  chains don't wait a full interval per step.

## v1.8.12
### Fixed
- VLAN-only nodes keep their SSH host keys across stop/start.

## v1.8.11
- VPC 1:1 NAT supported on the older configuration-profile network model; start checks the
  reserved IP is still available first.

## v1.8.10
- API tokens can hold single operations, not only the read/operate/configure/admin bundles.
- `status` prints pure JSON on stdout.

## v1.8.9
- Schedule, group and manual-only recovery tags are rewritten on every start and stop; the
  individual schedule is also kept in Object Storage.

## v1.8.8
- API token records are kept in Object Storage, so a restore never brings back a revoked token.

## v1.8.7
- Dashboard Activity, Logs and Console pages.

## v1.8.6
- VPC 1:1 NAT supported on Linode Interfaces.

## v1.8.5
- A start whose VPC address was taken while the node was stopped moves the node to a free address
  automatically; onboarding warns about unmanaged instances on the same network.

## v1.8.4
- `set-vpc-address` moves a stopped node to a new VPC address; onboarding refuses an address
  another managed node already has.

## v1.8.3
- Migration supports the Linode Interfaces model; the dashboard can restart a stale or
  interrupted migration.

## v1.8.2
### Fixed
- Migration keeps every network interface of the original instance.

## v1.8.1
- VPC-only instances migrate and are checked over their private address.
- `install.sh --domain` sets up HTTPS with Caddy.

## v1.8.0
- A group can depend on several groups, with the full start order recoverable.
- `install.sh` installs on a fresh host or recovers onto a replacement.

## v1.7.0
- Group start order (dependencies), manual-only nodes, scoped API tokens, `wait=true`, start or
  stop a whole group in order, CLI exit codes and `--json`.

## v1.6.2
- Every Linode OS image supported: per-OS network configuration and a safer rescue copy command.

## v1.6.1
- VPC-only networking fixed; overnight schedules.

## v1.6.0
- The scheduler handles large fleets in parallel and paces Linode API requests.

## v1.5.0
- Hooks recoverable from Object Storage and tags.

## v1.4.0
- Pre-stop and post-start hooks.
### Fixed
- Recreated instances came back without DNS servers.

## v1.3.5
### Fixed
- A quick second click on a different dashboard action was ignored.

## v1.3.4
- VPC-only and VLAN-only instances supported.

## v1.3.3
- Recovery tags kept on the OS volume, so instances without a public IP are covered.

## v1.3.0 – v1.3.2
- Kubernetes (LKE) deployment path with an official container image.

## v1.2.4 – v1.2.6
- Optional Object Storage backup alongside tag-based recovery, and the `backup` command.

## v1.0.0 – v1.2.3
- Initial releases: the delete/recreate engine, CLI, scheduling, groups, manual override, savings
  display, REST API with "Login with Linode", and the web dashboard.
