# Scenario: go live with the user's project

## What the user sounds like

- "I made a website — how do I let people see it?"
- "Put this project online" / "deploy my project"
- "Here's my code, get it running" / (drops a project folder or GitHub repo)
- "Give this a public address" / "publish this app"

## Your goal

Turn their project into a running Sealos service with a public URL. You
handle everything end to end; they just receive the URL.

## Steps

1. **Read the project**: framework, listen port, existing Dockerfile or
   compose file, and whether it needs a database or storage.
2. **Sign in** if not yet ([sealos-overview.md](sealos-overview.md)).
3. **Prepare the image** (write a Dockerfile if missing — [build.md](build.md)):
   - Build **linux/amd64 only** — Sealos nodes are amd64, and an Apple
     Silicon default build deploys fine then dies with `exec format error`:
     ```bash
     docker buildx build --platform linux/amd64 -t <registry>/<app>:<tag> --push .
     ```
   - The server must listen on `0.0.0.0` (not 127.0.0.1) with a
     deterministic port; private images need a pull secret ([build.md](build.md)).
4. **Write the template** into `.sealos/template.yaml` (inside the
   project) or a temp dir, following the contract in [platform.md](platform.md)
   exactly: Template envelope + one KubeBlocks block per database
   ([databases.md](databases.md)) + workload (StatefulSet if it writes to
   disk) + Service + Ingress + App CR.
5. **Validate — free, with quota preview**:
   ```bash
   python3 scripts/sealos-api.py deploy <template.yaml> --dry-run
   ```
6. **Deploy** (same command without `--dry-run`). The response lists every
   created resource; its `name` is the instance name.
7. **Verify — nothing is "live" until this passes**:
   ```bash
   export KUBECONFIG=~/.sealos/kubeconfig
   HOST=$(kubectl get ingress -l "cloud.sealos.io/deploy-on-sealos=<instance>" \
     -o jsonpath='{.items[0].spec.rules[0].host}')
   bash scripts/wait-app.sh -t 600 -u "https://$HOST" \
     -l "cloud.sealos.io/deploy-on-sealos=<instance>"
   ```
   `wait-app.sh` waits for the Deployments/StatefulSets/KubeBlocks Clusters
   matching the selector, fails fast on non-recovering pods with
   diagnostics, then probes the URL. Success = exit 0 AND the URL returns
   a real page (200/30x; 401/403 is fine for login-walled apps; for SSR
   apps also `curl -sL` the page and reject bodies containing "Application
   error" / "Internal Server Error").
   Heavy apps (Rails/PHP/JVM) can need 1-3 more minutes after pods turn
   ready — wait-app keeps probing until the timeout, don't shortcut it. If
   the URL fails with code 000 while workloads are ready, check your own
   network before blaming the app: probe a known-good host in the same
   region.

## Pitfalls

- App listens on 127.0.0.1 → unreachable from outside; fix it to 0.0.0.0.
- Needs a database that doesn't exist yet → the app crash-loops; create it
  first ([scenario-create-db.md](scenario-create-db.md)).
- Deploy returns 409 `ALREADY_EXISTS` → the create endpoint is not atomic
  and sometimes reports 409 for a deploy that actually landed. Check
  reality before retrying: `kubectl get all,cluster -l
  "cloud.sealos.io/deploy-on-sealos=<name>"` — if resources exist, continue
  to verification as if it succeeded. If not, the name is taken or its
  resources are still finalizing from an earlier delete (KubeBlocks
  clusters take minutes) → redeploy under a **fresh** name, never the
  just-failed one.
- Keep the random suffix in `app_name`/`app_host`, and run one deploy per
  app name: stripping the suffix collides with earlier deploys in the
  shared namespace and breaks selectors.
- App runs but misbehaves (won't open / errors / slow) → switch to the
  troubleshoot scenario ([scenario-troubleshoot.md](scenario-troubleshoot.md)).
- Iterating a failed deploy: fix the template, `sealos-api.py delete
  <instance>`, redeploy. `kubectl set env/image` directly is fine for quick
  experiments, but fold every fix back into the template so the final
  artifact reproduces.

## How to reply

- Success → "Your site is live at https://xxx" (+ where the initial
  credentials live, if any — the credential secret's name, or the template
  README for store apps). Keep the instance name at hand: it's the handle
  for every later change or removal (`sealos-api.py delete <instance>`).
- Missing something → ask for exactly that one thing ("I still need the
  database password from you").
- Fixable yourself → "I'm on it", then retry. No technical post-mortem.
