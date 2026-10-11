# Security policy

## Supported versions

Security fixes are made on the latest release only. Upgrade to the newest tag before reporting,
and check the [changelog](CHANGELOG.md) in case the issue is already fixed.

| Version | Supported |
|---|---|
| Latest release | Yes |
| Anything older | No — upgrade first |

## Reporting a vulnerability

Please **do not open a public issue** for a security problem.

Report it privately through GitHub: open the repository's **Security** tab and choose
**Report a vulnerability**. Only the maintainers can see the report.

Include what you can of:

- the version you run (the `VERSION` file, or the release tag);
- how it's deployed (VM with `install.sh`, or Kubernetes);
- the steps to reproduce, and what an attacker gains;
- any logs or output, with tokens, keys and passwords removed.

You'll get an acknowledgement within 5 working days. Once the problem is confirmed, a fix is
released as soon as practical and the advisory is published with the fixed version.

## Scope

This tool runs in your own environment with a Linode API token that can create and delete your
instances, so these are especially relevant:

- anything that exposes the Linode API token, the deployment SSH key, Object Storage keys, OAuth
  secrets or API tokens (in logs, API responses, the dashboard, backups or Object Storage);
- getting the API server or dashboard to act without a valid session or token, or beyond a
  token's scopes or node/group limits;
- running commands on the scheduler host or a managed node beyond what a configured hook does;
- anything that makes the tool delete or overwrite resources it doesn't manage.

Out of scope: problems that need an attacker who already controls the scheduler host or holds
a full-access Linode API token for your account, and findings in third-party dependencies that
have no impact on this tool (report those to the dependency).

## Keeping your deployment secure

- Keep `.env` readable only by the service user (`install.sh` sets mode 600).
- Put the dashboard and API behind HTTPS (`install.sh --domain`) and don't expose port 8000
  directly.
- Give API tokens only the scopes and nodes they need, set an expiry, and revoke unused ones.
- Rotate credentials after a suspected leak: `instance_manager.py rotate-credentials`.
