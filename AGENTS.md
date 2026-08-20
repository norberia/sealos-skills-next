# AGENTS.md

## Cursor Cloud specific instructions

This repo is the **Sealos agent plugin** (`use-sealos` skill), not a deployable
application. There is no server and no build step. See `README.md` for the
package layout and `CLAUDE.md` for skill-routing rules. The "application" is
`index.js`, which registers the `use-sealos` skill on a host's `ctx.skills`
(DeepSeek Harness / Cordis and other hosts).

### Environment (repo-managed)

The Cloud Agent environment is defined by `.cursor/environment.json`, which runs
`.cursor/install.sh` on the default Cursor image. That script installs the two
system tools this repo needs but that aren't npm deps — `shellcheck` (CI shell
lint) and the `codex` CLI (`@openai/codex`, global) — then runs `npm install`.
It is idempotent (skips tools already on PATH). Verified via a draft build:
`codex --version` → `codex-cli 0.148.0`, `shellcheck` 0.9.0, both on PATH.
For the tools to persist across new sessions, this file must be merged to the
default branch (repo-managed config wins over dashboard environments); enabling
Environment Builds bakes the install into a snapshot so new pods start instantly.

### Services / components

- **Plugin (Node ESM)** — `index.js` loads `plugins/sealos/skills/use-sealos/SKILL.md`
  and exposes it via `apply(ctx)`. Only runtime dep is `yaml`.
- **Skill tooling (Python, stdlib-only)** — `plugins/sealos/skills/use-sealos/scripts/sealos-api.py`
  (Sealos auth + Template API CLI) and `wait-app.sh`. No pip installs needed;
  `python3` is enough. `sealos-api.py status` runs without credentials and
  reports `{"authenticated": false}` until a kubeconfig exists at `~/.sealos/`.

### Lint / test / run

Commands mirror `.github/workflows/ci.yml` — use it as the source of truth:

- Lint (shell): `shellcheck` every `*.sh`. `shellcheck` is a system tool (not an
  npm dep); it is installed by `.cursor/install.sh`, not `npm install`.
- Lint (manifests): every `*.json` must parse with `jq`.
- Test: `node --test test.js test-host-coverage.js` and
  `python3 -m py_compile` every `*.py`. `node --check index.js` for syntax.
- Run (hello-world): load the plugin as a host would —
  `node -e 'import("./index.js").then(({apply})=>{const p=[];apply({skills:{registerProvider:c=>p.push(c())}});p[0].list().then(l=>console.log(l[0].name))})'`
  should print `use-sealos`.

### Gotchas

- `skills/use-sealos` is a **symlink** to `plugins/sealos/skills/use-sealos`, and
  `test-host-coverage.js` asserts it. Never replace it with a copied directory.
- Host manifest versions must all agree (CI enforces one version per plugin).
- Local-only artifacts (scratch clones, dumps, screenshots) go in gitignored
  `validation-assets/` per `.cursor/rules/validation-assets.mdc` — never commit them.
