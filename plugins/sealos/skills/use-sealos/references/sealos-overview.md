# Sealos background (read once, the first time you use this skill)

Builds the mental model you operate with. This is your internal reference —
when the user asks what Sealos is, explain in plain words, don't recite the
architecture.

## What Sealos is

A managed Kubernetes you can use right after signing up: deploy apps, run
managed databases and object storage, scale with one click; load balancing
and high availability included.

## What a user owns on Sealos

Every user/workspace is one fully isolated namespace containing only their
own things:

| Thing | What it is | Where the user sees it |
|---|---|---|
| App | a running service with an automatic public URL | console "Apps" |
| Database | managed PostgreSQL / MySQL / MongoDB / Redis / Kafka, HA included | console "Databases" |
| Object storage | S3-compatible store for files (MinIO underneath) | console storage entry |
| Instance | everything one deploy produced (possibly app + database + storage) — one unit to manage and delete as a whole | console instance list |

Under the hood (yours to know, not the user's): apps are ordinary
Deployments/StatefulSets + Service + Ingress labeled
`cloud.sealos.io/app-deploy-manager: <app>` so the App Launchpad UI manages
them; public HTTPS is an auto-assigned `<host>.<app-domain>` with a
wildcard cert — the app domain is region-specific and can differ from the
region's own domain (hzh serves apps under `sealoshzh.site` while its API
is `hzh.sealos.run`), so never guess the URL: read the host from the
instance's Ingress; inside templates `${{ SEALOS_CLOUD_DOMAIN }}` resolves
to the right domain. Databases are KubeBlocks `Cluster` CRs.

## Template store

Ready-made templates for 200+ popular self-hosted apps (Uptime Kuma,
Metabase, n8n, ...): one-click, about 3 minutes. Per-app recipes and known
pitfalls: [recipes.md](recipes.md).

## Regions

Sealos has international and China regions (e.g. `usw-1.sealos.io`,
`bja.sealos.run`). Regions are fully independent — accounts, resources, and
available versions don't carry over. The current region shows in your
credentials; the Template API lives at `https://template.<region-domain>`.

## Your tools

- `scripts/sealos-api.py` — auth (`status`, `login`, `workspaces`,
  `switch`) and the Template API (`deploy`, `store-list`, `store-get`,
  `deploy-store`, `instances`, `delete`). Python stdlib only, nothing to
  install.
- `kubectl` with `export KUBECONFIG=~/.sealos/kubeconfig` — all reads,
  debugging, and deletions. The kubeconfig is namespace-scoped: you cannot
  see or touch other tenants.
- `scripts/wait-app.sh` — post-deploy verification: workload readiness +
  URL probe + failure diagnostics.
- Credentials live in `~/.sealos/` after sign-in.

## Signing in (the first task of a session)

```bash
python3 scripts/sealos-api.py status
```

`authenticated: true` → proceed (note `namespace` and `region_domain`).
Not authenticated → run `sealos-api.py login` **in the background**, watch
its stderr, and relay the sign-in URL to the user the moment it prints (the
code expires; never sit silently until the command ends). Alternative: the
user pastes a kubeconfig from the Sealos web console (top-left avatar →
kubeconfig) into `~/.sealos/kubeconfig`. Wrong region → `login --region
<url>`. Wrong workspace → `workspaces`, then `switch <name>`.

If the agent sandbox blocks writes outside the workspace, escalate file
access for writing `~/.sealos/kubeconfig` and for any command that must
read it.

## Mental model before any playbook

1. Deploys always go through the Template API, never `kubectl apply`.
2. Every deploy is an instance: manageable and deletable as one unit.
3. The public URL is an auto-assigned subdomain — it exists as soon as the
   deploy lands.
4. Databases and storage are managed services: use them, never hand-roll.
5. Everything running bills the account: pods, database clusters, volumes,
   buckets.
