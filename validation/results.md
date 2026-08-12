# Batch validation — 39 projects on usw-1.sealos.io

Date: 2026-08-12. Method: every app deployed end-to-end with the plugin's own
tooling (`sealos-api.py` + `wait-app.sh`), verified by workload readiness AND
an HTTP probe of the public URL, then deleted. Times are deploy→URL-responding
wall clock, measured on a warm cluster; first-ever pulls of huge images can
add minutes.

## Path A — template store (21/21 pass)

| App | Time | HTTP | Notes |
|---|---|---|---|
| excalidraw | 19s | 200 | |
| changedetection | 31s | 200 | |
| uptime-kuma | 48s | 200 | |
| memos | 11s | 200 | |
| vaultwarden | 21s | 200 | |
| jellyfin | 91s | 200 | |
| gitea | 56s | 200 | |
| umami | 98s | 200 | |
| nocodb | 219s | 200 | args: admin email/password |
| librechat | 42s | 200 | |
| metabase | 152s | 200 | first attempt hit the create-endpoint 409 quirk; resources were actually up (see deploy.md first-aid) |
| authentik | 414s | 200 | slow warmup: URL lags workloads by ~2 min |
| penpot | 381s | 200 | |
| twenty | 195s | 200 | |
| chatwoot | 296s | 200 | |
| plausible | 105s | 200 | args: ClickHouse creds, DISABLE_REGISTRATION, SECRET_KEY_BASE |
| plane | 171s | 200 | multi-service |
| budibase | 214s | 200 | args: admin email/password |
| rocketchat | 207s | 200 | args: admin username/name/email/password |
| immich | 135s | 200 | 4c/17Gi quota |
| posthog | 327s | 200 | 5c/10.8Gi quota; slow first response |

## Path B — generated templates from image recipes (18/18 pass)

| App | Time | HTTP | Notes |
|---|---|---|---|
| mealie | 24s | 200 | |
| actual | 19s | 200 | |
| stirling-pdf | 75s | 401 | `:latest` ships with login enabled (`admin`/`stirling`) |
| metube | 39s | 200 | |
| komga | 51s | 200 | |
| grocy | 24s | 200 | |
| freshrss | 12s | 200 | lowercased PVC name for uppercase path |
| gatus | 15s | 200 | ConfigMap-mounted config.yaml |
| windmill | 145s | 200 | pg + init Job |
| freescout | 67s | 200 | apecloud-mysql + init Job |
| zabbix | 87s | 200 | two workloads; server auto-inits schema |
| guacamole | 104s | 200 | web+guacd pinned 1.6.0; schema Job from versioned GitHub SQL |
| miniflux | 118s | 200 | |
| libredesk | 145s | 200 | initContainer renders config.toml; password complexity rule |
| keila | 97s | 200 | full `MAILER_SMTP_*` set required at boot |
| misskey | 152s | 200 | initContainer renders default.yml; pg + redis |
| discourse | 175s | 200 | `bitnamilegacy/discourse` (Docker Hub `bitnami/*` archived 2025) |
| bagisto | 171s | 200 | initContainer: cache dirs + migrate + seed against external MySQL, chown last |

## Issues found and fixed during validation

1. **`wait-app.sh` false positive** — on curl timeout, `-w '%{http_code}'`
   printed `000` and the `|| echo 000` fallback appended another, producing
   `000000`, which the numeric `-lt 500` check read as 0 (“success”). Slow
   apps (authentik, posthog, bagisto) were passing without a verified URL.
   Fixed: sanitize the code, never break the probe loop on `000`.
2. **Create endpoint 409 quirk** — `POST /api/v2alpha/templates/instances`
   occasionally reports 409 `ALREADY_EXISTS` for a deploy that actually
   landed (internal retry colliding with itself). Documented in deploy.md:
   check cluster state before treating 409 as failure, retry only under a
   fresh name.
3. **Recipe corrections** now baked into recipes.md: keila (SMTP set),
   libredesk (image name, config file, password rule), discourse
   (bitnamilegacy), guacamole (version-pinned pair + schema SQL), bagisto
   (no auto-migrate on external DB, missing cache dirs, root-owned cache
   files from init — chown must be the last init step), stirling-pdf
   (login default on).

## Environment notes

- One validation window (~17:10–17:30 UTC+8) hit a platform/network
  fluctuation: workloads ready but no URL reachable from the test machine,
  across all fresh hostnames. Every affected app re-passed cleanly afterward.
  When URL probes return 000 while workloads are ready, verify your own
  network against a known-good host in the region before debugging the app.
- KubeBlocks cluster deletion holds finalizers for minutes; never reuse a
  just-deleted instance name (deploy.md first-aid).
