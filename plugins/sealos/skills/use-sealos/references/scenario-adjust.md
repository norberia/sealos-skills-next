# Scenario: adjust a running app

## What the user sounds like

- "Scale up / add capacity" / "it can't handle the load"
- "Add memory" / "bump the resources"
- "Change a setting / an env var" / "update to the new version"
- "Restart it"

## Your goal

Change what is already running, then verify the change landed.

## Steps

1. **What changes**: resources (scale), configuration (env), or version
   (image).
2. **Apply it**:
   ```bash
   kubectl scale deploy/<name> --replicas=<n>
   kubectl set env deploy/<name> KEY=value
   kubectl set image deploy/<name> <container>=<image>
   kubectl rollout restart deploy/<name>
   ```
   (Scale to 0 and back = pause/resume without deleting — what the UI
   pause button does.)
3. **Verify**: `kubectl rollout status deploy/<name>`; for user-facing
   changes re-run `bash scripts/wait-app.sh -u https://<host>
   deployment/<name>`.

## Rules (hard, not pitfalls)

- Resource moves follow the ladder ([platform.md](platform.md)): one step
  at a time, never skipping tiers.
- Scaling DOWN or cutting resources is destructive → confirm with the user
  first.
- Fold changes into the template file — live-only changes are lost on the
  next deploy.

## How to reply

- Done → "Done — it's running bigger now" / "the setting is live".
- Needs confirmation → "Scaling down to X will affect Y — sure?"
