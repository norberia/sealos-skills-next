#!/usr/bin/env bash
# Cloud Agent environment install: system tools + repo dependencies.
# Idempotent and non-interactive; safe to re-run.
set -euo pipefail

# The ShellCheck CLI — required by the CI shell-lint job (.github/workflows/ci.yml).
# It is a system tool, not a repo dependency, so it lives in the environment layer.
if ! command -v shellcheck >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y shellcheck
fi

# Codex CLI (global). The package name MUST be scoped: the unscoped `codex`
# on npm is an unrelated project.
if ! command -v codex >/dev/null 2>&1; then
  npm install -g @openai/codex
fi

# Repo dependencies (only runtime dep is `yaml`).
npm install

# Print versions so environment build logs prove the tools are on PATH.
echo "verify: codex $(codex --version)"
echo "verify: shellcheck $(shellcheck --version | sed -n '2p')"
