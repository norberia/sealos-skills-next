# Sealos

This repository is the Sealos Cloud agent plugin. It is not an application to deploy.

When the user wants to deploy, host, operate, or troubleshoot apps on Sealos
Cloud, follow the `use-sealos` skill:

- `plugins/sealos/skills/use-sealos/SKILL.md`
- `skills/use-sealos/SKILL.md` (same skill; the `skills.sh` entry)

Read that file and its `references/` before acting. Do not invent a parallel
Sealos workflow.

Slash commands such as `/sealos` are host-dependent and are not claimed by this
context file. Natural-language requests ("deploy X to Sealos" / "帮我把 X 部署到
Sealos") are enough.

Auth, Template API, kubectl, and verification live in that skill's `scripts/`
and `references/`.
