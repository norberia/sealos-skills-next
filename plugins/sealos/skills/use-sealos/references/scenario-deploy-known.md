# Scenario: deploy a known self-hosted app

## What the user sounds like

- "Deploy metabase" / "install n8n" / "set up an Uptime Kuma"
- "I want bitwarden — install one for me" / "set up an RSS reader"
- (names any specific self-hosted product)

## Your goal

Find the app in the Sealos template store and one-click it — the fastest
path (~3 min). When a template exists, never build the product yourself.

## Steps

1. **Sign in** if not yet.
2. **Search the store**:
   ```bash
   python3 scripts/sealos-api.py store-list --search <name>
   ```
3. **Inspect the hit** — which args are required:
   ```bash
   python3 scripts/sealos-api.py store-get <template>   # inputs + quota
   ```
   Entries with `"required": true` and no default must be supplied. Ask the
   user only for values only they know (their email, external API keys);
   everything else has sane defaults.
4. **Check the recipe** for known pitfalls: [recipes.md](recipes.md).
5. **Deploy**:
   ```bash
   python3 scripts/sealos-api.py deploy-store <template> [--name <instance>] \
     [--args-json '{"KEY":"value"}'] [--args-file <file>]
   ```
   Use `--args-file` for secret values. The response lists every created
   resource; the instance name is `name` in the response (defaults to
   `<template>-<random8>`).
6. **Verify**:
   ```bash
   bash scripts/wait-app.sh -u https://<host>.<region-domain> [deployment/<name>]
   ```

## Pitfalls

- No store hit → check [recipes.md](recipes.md) for a ready image recipe,
  else use the official Docker image. **Research before writing the
  template**: official image + tag, listen port, required env, persistence
  paths, database/cache dependencies — the product's own
  `docker-compose.yml` is the source of truth. Then follow steps 4-7 of
  the project scenario
  ([scenario-deploy-project.md](scenario-deploy-project.md) — no
  building). **Never build a product from source when an official image
  exists.**
- 400 `INVALID_PARAMETER` → a required arg is missing; `store-get` again
  and supply `--args-json`.
- Heavy apps (authentik, posthog) answer slowly on first load — give them
  2-3 minutes after the workloads are ready.
- The store keeps growing: re-check `store-list --search` even for apps
  already listed in recipes.

## How to reply

- Success → "It's up: https://xxx" (+ where the initial account credentials
  live, if any).
- Missing arg → "I need one thing from you: the admin email."
