# Deployment guide — running this on Linode Kubernetes Engine (LKE)

This is an alternative to [`DEPLOYMENT.md`](./DEPLOYMENT.md) (the single-VM
deployment) — same tool, same CLI, same data model, just packaged as a container and run on
an existing LKE cluster instead of a dedicated host. Read `README.md` §1–§4 first if you
haven't already (what this is, prerequisites, building or migrating your first managed
node) — this guide is only about running the tool itself.

**Pick this path if** you already operate an LKE cluster and would rather run one more
lightweight workload on it than provision and maintain a separate VM. **Pick the VM guide
instead if** you don't already have a cluster, or you'd rather this tool's deployment be
fully independent of anything else running in Kubernetes.

---

## 1. Deployment model — what's actually different here

The application itself is unchanged: one SQLite database as the source of truth, a
scheduler loop (`poll`) that fires `start`/`stop` when a schedule is due, and an optional
REST API + dashboard (`serve-api`). What changes on LKE:

- **One Kubernetes Deployment, pinned to exactly one replica, running two containers in the
  same pod** — `scheduler` (the poller) and `api` (the REST API/dashboard), sharing one
  persistent volume. This mirrors the VM deployment's own two-systemd-services-on-one-host
  design exactly; Kubernetes just supervises the processes instead of systemd. It is
  **not** meant to be scaled — see `k8s/deployment.yaml`'s own header comment for exactly
  why running more than one replica would be unsafe, not just wasteful.
- **A PersistentVolumeClaim** (backed by a real Linode Block Storage volume) replaces the
  VM's local disk for `state/` — the SQLite registry, per-instance lock files, and the
  tool's own `known_hosts` file.
- **Kubernetes Secrets** replace the `.env` file for credentials.

Everything else — how scheduling/groups/manual override work, the CLI commands, the audit
trail, the hybrid tag + Object Storage backup design — is identical to the VM deployment and
documented once, in this project's own main usage guide, not repeated here.

---

## 2. Prerequisites

1. An LKE cluster (Standard or Enterprise) you already have `kubectl` access to.
2. `envsubst` (part of GNU gettext) — `k8s/deploy-lke.sh` uses it. Usually already present
   on Linux; on macOS, `brew install gettext`.
3. Only if you're building your own image instead of using the official one (§3 below):
   Docker (or another OCI-compatible builder), and a container registry you can push to and
   this cluster can pull from (e.g. GitHub Container Registry, Docker Hub, a private
   registry with an `imagePullSecret` configured on the cluster — setting that up is outside
   this guide's scope).

---

## 3. Getting the image

**The fast path — use the official image.** An image is published automatically on every
tagged release to `ghcr.io/sandipgangdhar/linode-instance-scheduler`, both `:latest` and a
specific `:vX.Y.Z` tag matching each release. Nothing to build or push yourself — skip to
§4, using `ghcr.io/sandipgangdhar/linode-instance-scheduler:latest` (or a pinned version tag)
as the `--image` value in §6.

**Build your own instead if** you want a different CPU architecture than the official
image's `linux/amd64`, you've modified the source, or you'd rather not run a pre-built
binary you didn't build yourself. From the repository root (the `Dockerfile` there is
written relative to it and can't be built from inside a subdirectory):

```
docker build --platform linux/amd64 -t <registry>/linode-instance-scheduler:latest .
docker push <registry>/linode-instance-scheduler:latest
```

**Always pass `--platform` explicitly.** A plain `docker build` with none builds for
whatever machine you're running it on, not the machine that will actually run the image —
on an Apple Silicon Mac that silently produces an `arm64` image a real (`amd64`) LKE node
can't run at all, with no error until the pod crash-loops. `linux/amd64` is correct for
almost every Linode compute plan and therefore almost every LKE node pool; only use
`docker buildx build --platform linux/amd64,linux/arm64 ... --push` instead if your cluster
genuinely mixes CPU architectures across node pools.

Rebuilding and pushing a new tag, then re-running `k8s/deploy-lke.sh --image <new-tag>` (or
`kubectl set image deployment/linode-instance-scheduler scheduler=<new-tag>
api=<new-tag> -n <namespace>`), is how you upgrade later — see §9.

---

## 4. Configuring credentials

All in `k8s/`, copied from their own `.example.yaml` templates — these mirror
`.env.example` exactly, credential for credential; see that file's own comments for what
each one is and where to create it in Cloud Manager.

```
cd k8s
cp secret.example.yaml secret.yaml               # LINODE_API_TOKEN, OAuth, Object Storage, key passphrase, alerts
cp ssh-key-secret.example.yaml ssh-key-secret.yaml   # the deployment's own SSH private key
```

Edit both with real values (or, better, create them directly from the command line instead
of ever writing real credentials to a file — see each template's own header comment for the
exact `kubectl create secret` command). Both files are gitignored; never commit them.

Set `LIS_SSH_KEY_PASSPHRASE` to a new random value of 12+ characters (`openssl rand -base64 24`).
It encrypts the stored copies of the deployment SSH key; the scheduler keeps its recovery copy in a
private StackScript in your Linode account, so the API token needs StackScripts read/write. The
optional `LIS_ALERT_*` values turn on webhook and email alerts (see `DEPLOYMENT.md` §8.2).

`configmap.example.yaml` holds only non-secret tunables (poll interval/window, optional CORS
origins) — copy it to `configmap.yaml` if you want to change the defaults, otherwise
`deploy-lke.sh` uses the example file's own defaults automatically.

---

## 5. The "do you already run Linode-LKE-Private-Network" question

If your cluster already runs
[`Linode-LKE-Private-Network`](https://github.com/sandipgangdhar/Linode-LKE-Private-Network)
— the VLAN/VPC networking automation many LKE customers deploy — it installs a Kyverno
policy that automatically mutates **every** new pod on the cluster (outside a short
exclude-list of namespaces) to require scheduling onto a node labeled `vlan-ready=true`.
`k8s/deploy-lke.sh` detects this (read-only `kubectl get` checks against well-known
resource/policy names) and asks, once, whether you want this deployment's own pod to run
under that gate.

**This tool never depends on that project's code, its `etcd`, or its REST API in any way —
the two are, and stay, fully independent.** The only thing your answer changes is whether
this deployment's namespace carries a `kyverno-mutation-exempt=true` label:

- **Exempt (the default, and what most deployments should choose)** — the scheduler's pod
  schedules normally on any node. Nothing about this tool's own job (making calls to the
  public Linode API to manage *other* instances) needs a VLAN/VPC interface for itself.
- **Not exempt** — the pod runs like any other application workload on your cluster,
  requiring a `vlan-ready=true` node. Choose this only if you specifically want the
  scheduler's own pod reachable over your existing private VLAN/NAT path, or your own
  cluster convention requires every workload to be gated the same way.

**One exception to "exempt is the default": managing VPC-only or VLAN-only instances.** The
scheduler reaches every managed instance over SSH (a reachability check after every start, a
key capture before every stop). An instance with no public interface can only be reached at its
private VPC/VLAN address, so the scheduler's pod must run on a node that is on that same VPC or
VLAN — with `Linode-LKE-Private-Network`, that means choosing **not exempt**, so the pod lands on
a `vlan-ready=true` node. If all your managed instances have public interfaces, exempt remains the
right choice.

If nothing is detected, this question is skipped entirely and the pod deploys normally —
there's nothing to opt in or out of.

---

## 6. Deploying

```
cd k8s
./deploy-lke.sh --image ghcr.io/sandipgangdhar/linode-instance-scheduler:latest
```

(Substitute your own `<registry>/linode-instance-scheduler:...` here if you built your own
image in §3 instead of using the official one.)

Add `--yes` to skip the interactive VLAN-gating question (defaults to exempt — see §5),
`--assume-gated`/`--assume-exempt` to answer it non-interactively without skipping the rest
of the prompts, `--namespace <name>` to deploy somewhere other than the default
`linode-instance-scheduler` namespace, and `--expose loadbalancer` to provision a public
Linode NodeBalancer for the API/dashboard instead of the default in-cluster-only exposure
(§7). Run `./deploy-lke.sh --help` for the full option list.

The script applies the namespace, ConfigMap, both Secrets, the PVC, the Deployment, and a
Service, then waits for the rollout to finish. Verify:

```
kubectl get pods -n linode-instance-scheduler
kubectl logs -n linode-instance-scheduler deploy/linode-instance-scheduler -c scheduler -f
kubectl exec -n linode-instance-scheduler deploy/linode-instance-scheduler -c api -- curl -s http://localhost:8000/health
```

---

## 7. Exposing the REST API and dashboard

**In-cluster only by default, deliberately** — same posture as the VM guide's own "no
inbound access required unless you're exposing this to other people." Reach it via
`kubectl port-forward -n linode-instance-scheduler svc/linode-instance-scheduler-api
8000:80`, then open `http://localhost:8000/ui/`, or through your own existing Ingress if
you have one.

To expose it publicly instead, redeploy with `--expose loadbalancer` — Linode's own
cloud-controller-manager provisions a real, billed NodeBalancer automatically. Once it has
an address (`kubectl get service linode-instance-scheduler-api -n
linode-instance-scheduler`), that's what to register as the OAuth callback and put in front
of a real TLS terminator (a NodeBalancer alone is plain HTTP) before enabling "Login with
Linode" — the same OAuth App registration steps as the VM guide's §6 apply unchanged, just
with this hostname/IP instead of a VM's own.

---

## 8. Backups and disaster recovery

Unchanged from the VM deployment's own design (`docs/OPERATIONS.md`'s Backup & recovery
section, or the Definitive Guide's Part V) — every instance's identity, schedule, and group
membership are mirrored onto Linode's own resource tags regardless of how this tool is
deployed, and the optional Object Storage backup layer (§4's credentials) covers everything
tags can't hold. `rebuild` reconstructs the local registry from either, exactly the same way
whether this pod runs on a VM or in this Deployment.

The one LKE-specific addition: the PVC (`k8s/pvc.yaml`) itself is a real Linode Block
Storage volume with a `Retain` reclaim policy, so deleting the PVC object (accidentally or
via `deploy-lke.sh --cleanup --delete-data`) does not delete the underlying volume — it
becomes an unattached volume in your account, recoverable by hand if you ever need it,
rather than gone. That's a safety net on top of, not a replacement for, `rebuild`/Object
Storage — treat total loss of the PVC the same way the VM guide already treats total loss of
`state/instances.db`.

Everything else in the VM guide's §8 applies unchanged: with Object Storage configured, a snapshot
is uploaded within a minute of every change and old ones are pruned; the deployment SSH key is kept
encrypted in the database and Object Storage (passphrase recoverable from your Linode account);
the scheduler compares every node with the account every few minutes (`heal`); nodes left off
their schedule after an outage are listed for approval (`schedule-catch-up`); and alerts reach your
webhook or email. To move to a new cluster, deploy as usual with an empty PVC, then from the pod:
`ssh-key-restore --save-passphrase` (if the key Secret is gone), `restore`, `rebuild`, `heal`.

---

## 9. Upgrading

**Using the official image**, just point at the new version tag — nothing to build:

```
./deploy-lke.sh --image ghcr.io/sandipgangdhar/linode-instance-scheduler:v1.2.3
```

**Using your own image**, rebuild and push first:

```
docker build --platform linux/amd64 -t <registry>/linode-instance-scheduler:v1.2.3 .
docker push <registry>/linode-instance-scheduler:v1.2.3
./deploy-lke.sh --image <registry>/linode-instance-scheduler:v1.2.3
```

Re-running `deploy-lke.sh` with a new `--image` re-applies every manifest and updates the
Deployment's image. The Deployment's `Recreate` strategy (not `RollingUpdate`) means the old
pod fully terminates before the new one starts — a short gap where the scheduler/API are
both down, by design, matching this tool's own single-writer safety guarantee (see
`k8s/deployment.yaml`'s header comment). A brief pause is expected and safe; a missed
schedule window during it self-corrects on the next poll tick once the new pod is up.

---

## 10. Security checklist

- `k8s/secret.yaml`/`k8s/ssh-key-secret.yaml` are never committed (already gitignored) and
  only readable by whoever has RBAC access to `get`/`list` Secrets in this namespace —
  scope that access the same way you would for any other sensitive Secret on the cluster.
- The SSH keypair mounted into the pod is dedicated to this tool only — never a personal or
  shared key — so it can be rotated independently by replacing `ssh-key-secret.yaml` and
  restarting the Deployment.
- `LINODE_API_TOKEN` is scoped to exactly what's needed, not a token with broader account
  access than this tool actually uses.
- The API/dashboard stays in-cluster-only (§7) unless you deliberately choose
  `--expose loadbalancer`, and sits behind real TLS before "Login with Linode" is enabled.
- Both containers run as a fixed non-root user (uid 1000) with `allowPrivilegeEscalation:
  false` and every Linux capability dropped (`k8s/deployment.yaml`) — don't loosen this
  without a specific reason.
- If a credential is ever pasted somewhere it shouldn't have been, rotate it in Cloud
  Manager and update `k8s/secret.yaml`/`ssh-key-secret.yaml`, then `kubectl apply` again and
  `kubectl rollout restart deployment/linode-instance-scheduler -n <namespace>` — no code
  changes needed.

---

## 11. Uninstalling

```
cd k8s
./deploy-lke.sh --cleanup                  # Deployment, Service, ConfigMap, Secrets
./deploy-lke.sh --cleanup --delete-data    # ...and the PVC (see §8 for what that does and doesn't delete)
./deploy-lke.sh --cleanup --delete-namespace   # ...and the namespace itself
```

This only removes what this tool deployed to Kubernetes — it never touches the Linode
instances/volumes/reserved IPs this tool manages. Use `offboard`/`deregister` (see
`README.md`) for that, the same as on the VM deployment.
