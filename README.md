# sealos-skills-next

Agent plugin for deploying and operating apps on [Sealos Cloud](https://sealos.io),
packaged in the [Agent Plugins](https://agent-plugins.org/) format and as a
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) profile bundle.
The same `use-sealos` skill is also installable through `skills.sh`, OpenClaw /
ClawHub, CodeBuddy, Gemini CLI, Qwen Code, and generic repo importers.

Say "deploy X to Sealos" (or "帮我把 X 部署到 Sealos") in a compatible agent
and the `use-sealos` skill handles the rest: sign-in, picking the fastest
deploy path, databases, storage, public HTTPS, and post-deploy verification.

## What's inside

```
package.json                        # DeepSeek Harness bundle manifest (`dsh.bundle`)
cordis.patch.yml                    # inserts the skill provider into a dsh profile
index.js                            # registers `use-sealos` on `ctx.skills`
CLAUDE.md                           # context-only hosts (Gemini, Qwen, Amp / Kimi)
gemini-extension.json               # Gemini CLI extension
qwen-extension.json                 # Qwen Code extension
openclaw.plugin.json                # unused on OpenClaw's native package.json path; install plugins/sealos instead
.codebuddy-plugin/marketplace.json  # CodeBuddy marketplace
skills/use-sealos                   # skills.sh entry (symlink → plugins/sealos/skills/use-sealos)
plugins/sealos/
├── .claude-plugin/plugin.json      # Claude Code manifest
├── .cursor-plugin/plugin.json      # Cursor manifest
├── .qoder-plugin/plugin.json       # Qoder manifest
└── skills/use-sealos/
    ├── SKILL.md                    # scenario router: role, communication, scope, routing
    ├── references/                 # loaded on demand
    │   ├── sealos-overview.md      # read-once mental model (first task of a session)
    │   ├── scenario-deploy-project.md  # go live with the user's own project
    │   ├── scenario-deploy-known.md    # known app via the template store
    │   ├── scenario-create-db.md       # managed database / object storage
    │   ├── scenario-troubleshoot.md    # broken app: triage and fix
    │   ├── scenario-adjust.md          # scale / env / version changes
    │   ├── scenario-status-delete.md   # inventory and teardown
    │   ├── build.md                # Dockerfile, linux/amd64 build, registry push
    │   ├── databases.md            # KubeBlocks clusters + credentials
    │   ├── platform.md             # manifest contract (labels, Ingress, storage, quota)
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

### DeepSeek Harness

This repository is a dsh profile bundle. After `npx @deepseek-ai/dsh web` works, install the plugin into the same `web` profile (`pnpm` must be on `PATH`):

```sh
npx @deepseek-ai/dsh plugin --profile web add github:norberia/sealos-skills-next
npx @deepseek-ai/dsh web
```

A local checkout:

```sh
npx @deepseek-ai/dsh plugin --profile web add /path/to/sealos-skills-next
```

`use-sealos` appears in the session skill catalog. Say "deploy X to Sealos"; the model loads the skill via the `skill` tool, then runs `scripts/sealos-api.py` / `kubectl` through bash.

The default bash sandbox blocks writes outside the workspace. Login writes `~/.sealos/kubeconfig`, so those commands need `sandbox_permissions: danger-full-access`.

Add the GitHub topic `dsh-plugin` on the public repo so it shows up in the harness plugin index.

### OpenClaw

Install the plugin directory, not the repository root:

```sh
openclaw plugins install /path/to/sealos-skills-next/plugins/sealos
```

OpenClaw loads it as a Cursor bundle and exposes `use-sealos`. Slash-command
exposure is not claimed. Say "deploy X to Sealos".

Do not install the repo root. OpenClaw sees root `package.json` first, treats
the checkout as a native plugin, and fails because that package has no
`openclaw.extensions`. Root `openclaw.plugin.json` is not used on that path.

### CodeBuddy

```text
/plugin marketplace add norberia/sealos-skills-next
```

Command exposure depends on the CodeBuddy runtime.

### Gemini CLI

Context-only extension. Slash commands are not claimed.

```sh
gemini extensions install https://github.com/norberia/sealos-skills-next
```

Ask Gemini to deploy to Sealos Cloud. It loads `CLAUDE.md`, which points at
`use-sealos`.

### Qwen Code

Context-only extension. Slash commands are not claimed.

```sh
qwen extensions install https://github.com/norberia/sealos-skills-next
```

### skills.sh

```sh
npx skills add norberia/sealos-skills-next
```

The pack has one skill, `use-sealos`. Direct `/sealos-deploy` entries are not
claimed. After install, say "deploy X to Sealos".

### Amp / Kimi / generic repo importers

Import `https://github.com/norberia/sealos-skills-next.git`. Behavior is
host-dependent. Context is `CLAUDE.md`; the skill lives at `skills/use-sealos`
and `plugins/sealos/skills/use-sealos`.

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

## Requirements

- `kubectl`, `python3` (stdlib only); `docker` + `gh` only for the source path
- A Sealos account — the skill signs in via OAuth2 device flow, or paste a
  kubeconfig from the Sealos web console into `~/.sealos/kubeconfig`

## Development

Scratch clones, test dumps, and other local-only artifacts go in
`validation-assets/` (gitignored).
