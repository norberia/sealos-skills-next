# Scenario: create a database or object storage

## What the user sounds like

- "Give me a postgres" / "I need a MySQL" / "set up a redis" / "a MongoDB"
- "I need a database" / "give me a kafka"
- "Somewhere to store files" / "an S3" / "object storage"

## Your goal

Provision a managed database cluster or an S3-compatible bucket and hand
over the connection info — without ever printing a secret value.

## Database steps

1. **Sign in** if not yet.
2. **Write the template** — one KubeBlocks `Cluster` plus the four
   same-named RBAC resources (ServiceAccount / Role / RoleBinding / Cluster)
   per [databases.md](databases.md) (full template + per-engine
   differences).
3. **App wants its own database name?** Add the idempotent init Job
   (`pg_isready` wait + `CREATE DATABASE`) from [databases.md](databases.md);
   the same pattern covers extensions, users, and seed SQL.
4. **Validate + deploy**:
   ```bash
   python3 scripts/sealos-api.py deploy <template.yaml> --dry-run
   python3 scripts/sealos-api.py deploy <template.yaml>
   ```
5. **Verify** — a Cluster counts as ready only at `phase: Running`:
   ```bash
   bash scripts/wait-app.sh cluster/<name>
   ```

## Object storage steps

1. **Write the template** — an `ObjectStorageBucket` CR
   (`policy: private | publicRead | publicReadwrite`) per
   [platform.md](platform.md), which also shows how to wire
   `S3_ACCESS_KEY` / `S3_SECRET_KEY` / endpoint / bucket into an app via
   `secretKeyRef`.
2. **Validate + deploy** as above.

## Pitfalls

- KubeBlocks clusters take 1-3 minutes to reach `Running`; apps that can't
  connect during that window are normal — don't redeploy in a panic.
- Redis/MongoDB credential secrets appear only after the component pod
  starts — wait for the Cluster phase before judging.
- Available versions differ per region: check
  `kubectl get clusterversions.apps.kubeblocks.io` before pinning.

## How to reply

- Database ready → connection info in plain terms: host, port, user, and
  the fetch command for the password — never the password value. "Your
  database is up — host xxx, port xxx, user xxx; get the password with
  `kubectl get secret xxx -o jsonpath=...`."
- Storage ready → "Your storage is up; the credentials are in secret xxx."
