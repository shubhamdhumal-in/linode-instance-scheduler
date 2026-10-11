# syntax=docker/dockerfile:1.7
#
# Container image for the Linode Instance Scheduler (roadmap: LKE deployment option).
# This is an additional way to run the same code the VM deployment guide
# (docs/deployment-guide.md) already documents -- nothing about the application
# changes for this path; only how it's packaged and where its persistent state lives.
#
# Two-stage build:
#   1. "web-build"  -- Node, builds the React/Vite dashboard (web/dist/).
#   2. final        -- Python, installs the app's dependencies, then copies its Python files
#                       (individually -- not test files or other dev-only tooling this repo
#                       also carries) FLAT into /app, alongside web/dist copied from the
#                       build stage above. This flat, "web/dist is a direct sibling of the
#                       app's own .py files, and so is state/" layout is REQUIRED, not
#                       arbitrary -- it's exactly what the application's own code expects
#                       (api_server.py's own _WEB_DIST path, instance_manager.py's own
#                       REGISTRY_PATH) -- and it is the one thing this Dockerfile's own two
#                       COPY lines below must always preserve, however those source files are
#                       laid out in whichever copy of this repository you're building from.
#
# No credentials are baked in. LINODE_API_TOKEN and every other secret this tool uses are
# supplied at runtime as environment variables (see k8s/secret.example.yaml) -- never a
# build ARG, never written into an image layer.
#
# An official pre-built image is published automatically on every tagged release to
# ghcr.io/sandipgangdhar/linode-instance-scheduler (:latest and :vX.Y.Z), so building this
# yourself is a convenience, not a requirement -- see docs/deployment-guide-lke.md. Build it
# yourself instead if you want a different CPU architecture, want to build from a modified
# checkout, or would rather not trust a pre-built binary at all.
#
# Build from the repository root -- this Dockerfile's own COPY lines below are written
# relative to it and cannot be built from inside a subdirectory.
#
# ALWAYS pass --platform explicitly. A plain `docker build` with no --platform builds for
# the machine you're running it on, not the machine that will run the image -- on Apple
# Silicon (arm64) that silently produces an arm64 image that a real Linode LKE node (amd64)
# cannot run at all, with no error until the pod crash-loops in production. This is a
# documented, previously-hit mistake in the sibling Linode-LKE-Private-Network project's own
# build history; don't repeat it here.
#
# Single-arch (the common case -- almost every Linode compute plan, and therefore almost
# every LKE node, is amd64):
#
#   docker build --platform linux/amd64 -t <registry>/linode-instance-scheduler:latest .
#
# Multi-arch (only if your cluster genuinely mixes amd64 and arm64 node pools -- needs
# `docker buildx create --use` once first, and a registry to push the manifest list to
# directly; buildx can't load a multi-platform result into the local `docker images` cache):
#
#   docker buildx build --platform linux/amd64,linux/arm64 \
#     -t <registry>/linode-instance-scheduler:latest --push .
#
# The same image serves every long-running role this tool has (the scheduler/poller, the
# REST API + dashboard) -- which one a given container actually runs is decided entirely by
# the command/args Kubernetes passes it (see k8s/deployment.yaml), not by anything baked in
# here. Running the CLI for a one-off command (e.g. `onboard`, `rebuild`) against the same
# state is just `docker run <image> <subcommand> ...`, or `kubectl exec` into a running pod.

FROM node:20-slim AS web-build
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim AS final

# openssh-client provides the real `ssh` binary this tool shells out to for every
# reachability check and remote command against a managed instance (engine.ssh_run()) --
# the one external-binary dependency this project has. Its known_hosts bookkeeping and
# password-auth fallback are pure Python (paramiko, already in requirements.txt) and need
# nothing else from the OS.
RUN apt-get update && apt-get install -y --no-install-recommends \
      openssh-client \
    && rm -rf /var/lib/apt/lists/*

# A dedicated, non-root user. This process only ever needs to read its own code, write to
# its own mounted state volume, and read a mounted SSH key/secret files -- never root.
# Fixed uid/gid (1000) so it matches the `securityContext.fsGroup: 1000` set in
# k8s/deployment.yaml, which is what actually makes a freshly-provisioned, empty
# PersistentVolume writable by this user the first time it's mounted (a build-time `chown`
# on the image's own state/ directory has no effect on a volume mounted over it at runtime
# -- fsGroup is the real mechanism, this uid/gid pairing just has to agree with it).
RUN groupadd --gid 1000 scheduler \
    && useradd --create-home --uid 1000 --gid 1000 --shell /usr/sbin/nologin scheduler

WORKDIR /app

# Exact versions, each checked against its published hash.
COPY requirements.lock requirements.lock
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

# An explicit file list here, not a whole-directory copy -- the source tree this is built
# from also carries test files and dev-only tooling with no reason to be in a production
# image. Keep this list in sync with the real set of production modules if one is ever
# added or removed.
COPY linode_engine.py instance_manager.py api_server.py \
     object_storage_backup.py schema.sql ./
COPY --from=web-build /web/dist web/dist

# state/ is where the SQLite registry, per-instance locks, and the tool's own known_hosts
# file live (instance_manager.py's own REGISTRY_PATH constant -- a direct sibling of the
# .py files copied above, not nested under anything).
# This is the one directory in the image that must be a real PersistentVolume in
# Kubernetes, not ephemeral container storage -- see k8s/deployment.yaml's volumeMounts.
# Pre-created here only so a plain `docker run` with no volume mounted still works out of
# the box for local testing.
RUN mkdir -p /app/state && chown -R scheduler:scheduler /app

USER scheduler

# No CMD default subcommand on purpose -- Kubernetes always sets one explicitly per
# container role (`poll` vs `serve-api`, see k8s/deployment.yaml), and a plain
# `docker run <image>` with nothing else prints argparse's own usage/help instead of doing
# something unexpected by default.
ENTRYPOINT ["python3", "instance_manager.py"]
CMD ["--help"]
