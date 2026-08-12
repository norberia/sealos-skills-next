# sealos-skills-next

Agent plugin for deploying and operating apps on [Sealos Cloud](https://sealos.io),
packaged in the [Agent Plugins](https://agent-plugins.org/) format.

Say "deploy X to Sealos" (or "帮我把 X 部署到 Sealos") in a compatible agent
and the `use-sealos` skill handles the rest: sign-in, picking the fastest
deploy path, databases, storage, public HTTPS, and post-deploy verification.

## What's inside

```
plugins/sealos/
├── .claude-plugin/plugin.json      # Claude Code manifest
├── .cursor-plugin/plugin.json      # Cursor manifest
├── .qoder-plugin/plugin.json       # Qoder manifest
└── skills/use-sealos/
    ├── SKILL.md                    # intent router + execution rules
    ├── references/                 # loaded on demand
    │   ├── deploy.md               # three deploy paths (store / image / source)
    │   ├── build.md                # Dockerfile, linux/amd64 build, registry push
    │   ├── databases.md            # KubeBlocks clusters + credentials
    │   ├── platform.md             # manifest contract (labels, Ingress, storage, quota)
    │   ├── operate.md              # status, logs, debugging, deletion
    │   └── recipes.md              # per-app recipes for popular self-hosted software
    └── scripts/
        ├── sealos-api.py           # auth (OAuth2 device flow) + Template API, stdlib only
        └── wait-app.sh             # post-deploy readiness + URL verification
```

## Installation

### Cursor

From a published GitHub repo: **Settings → Plugins**, paste the repository
URL in **Search or Paste Link**, open the Sealos plugin, click **Add to
Cursor**.

For local development, symlink the skill into your personal skills folder:

```bash
ln -sfn "$(pwd)/plugins/sealos/skills/use-sealos" ~/.cursor/skills/use-sealos
```

### Claude Code

```text
/plugin marketplace add <github-org>/sealos-skills-next   # or a local path
/plugin install sealos@sealos-skills-next
/reload-plugins
```

`/plugin marketplace add` accepts a local directory too, so this works before
the repo is published.

### Codex

Add this repository (URL or local path) as a marketplace source under
**Plugins → More → Add more**. Manifest: `.agents/plugins/marketplace.json`.

### Qoder

In Qoder, open the plugin **Marketplace** (Settings → Plugins → Marketplace),
click **+ Create Plugin**, choose **import from a local folder**, and select
`plugins/sealos` from a clone of this repository.

To distribute, zip the contents of `plugins/sealos` (the zip root must
contain `.qoder-plugin/plugin.json`) as `sealos-0.1.0.zip` and share or
publish it through the Qoder marketplace.

### First run

Say "deploy X to Sealos". The skill checks credentials
(`sealos-api.py status`) and, if needed, signs you in via OAuth2 device flow —
or paste a kubeconfig from the Sealos web console into `~/.sealos/kubeconfig`.

## Deploy strategy

1. **Template store** — 200+ one-click templates, deployed via the Template
   API (~3 min).
2. **Official Docker image** — a generated template (workloads + KubeBlocks
   databases + Ingress) for self-hosted apps not in the store (~5-10 min).
3. **Source build** — Dockerfile → `docker buildx --platform linux/amd64` →
   registry push → image deploy (for the user's own projects).

All three paths go through the Sealos Template API, so every deployment is
tracked as an instance: visible in the Sealos UI and removable as a unit.

## Validation

All 39 reference apps (21 store templates + 18 image recipes, from
Excalidraw to PostHog/Immich/Discourse) were deployed end-to-end on
usw-1.sealos.io with this plugin's own tooling — workloads ready AND public
URL responding — then removed. Times, per-app notes, and the bugs this
process caught are in [validation/results.md](validation/results.md).

## Requirements

- `kubectl`, `python3` (stdlib only); `docker` + `gh` only for the source path
- A Sealos account — the skill signs in via OAuth2 device flow, or paste a
  kubeconfig from the Sealos web console into `~/.sealos/kubeconfig`

## Development

`readme-first.md` holds the original project brief. Temporary reference
clones (`sealos-skills-old/`, `railway-skills-ref/`) are gitignored.
