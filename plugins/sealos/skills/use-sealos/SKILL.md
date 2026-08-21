---
name: use-sealos
description: >
  Deploy and operate apps on Sealos Cloud: sign in to a Sealos account, deploy
  any project or self-hosted app (from the template store, an official Docker
  image, or project source code), provision managed databases (PostgreSQL,
  MySQL, MongoDB, Redis, Kafka), create S3-compatible object storage, expose
  public HTTPS domains, check status and logs, and troubleshoot failures. Use
  this skill whenever the user mentions Sealos (in any language), or asks to
  deploy, host, or self-host an application, website, or database without
  naming another platform — and also when they ask about something already
  running there ("my site is down / slow", "scale it up", "what am I
  running", "give me a database"). Typical trigger: "deploy X to Sealos".
allowed-tools: Bash(kubectl:*), Bash(python3:*), Bash(docker:*), Bash(curl:*), Bash(bash:*), Bash(git:*), Bash(gh:*), Bash(command:*)
---

# Use Sealos

## Your job

Take the user's project, app, website, or database live on Sealos — a
running service with a public URL they can open. Then manage what is already
running there: check status, troubleshoot, scale, adjust, delete.

## First task in a session: read the overview once

Before your first Sealos task, read
[sealos-overview.md](references/sealos-overview.md) to build the mental
model (what's on Sealos, what your tools are). Later tasks don't re-read it.

Resolve `scripts/` and `references/` against this skill's directory, not the
project working directory.

## Talking to the user

Never assume the user is technical — "I made a website, how do I let people
see it" and "deploy this image" must land the same outcome. Report results,
not process: the words `deployment`, `pod`, `ingress`, `image`, `command`,
`config`, `yaml` do not appear in replies. Say "your site", "your database",
"it's live", "one thing is still missing".

Default replies are one sentence:

- Success → the public URL, plus where credentials live if any (names, never
  values). "Your site is live at https://xxx."
- Missing input → ask for exactly that one thing. "I still need the database
  address from you."
- Fixable yourself → "I'm on it" and retry; don't narrate the technical
  cause.
- Process, terminology, and debug output only when the user asks. Technical
  users will ask — then answer with the evidence you already collected
  (ready state, HTTP code, decisive log lines).

- ❌ "Deployment ready, ingress returns 200, image pulled" → ✅ "Your site
  is live at https://xxx."
- ❌ "Pod in CrashLoopBackOff: missing DATABASE_URL env var" → ✅ "One thing
  left: I need the database address from you."

Verification evidence (hard rule 3) is still gathered for every deploy —
collect it, verify against it, but don't lead with it.

## Scope

This skill puts things on Sealos and manages what is already running there.
It does not: operate other platforms, drive multiple accounts or workspaces
at once, build CI/CD pipelines, set up monitoring/alerting, do
backup-restore, or manage custom TLS certificates — decline those instead of
improvising. It deploys and operates; it does not write the user's
application code.

## Identify the scenario, then follow its playbook

Match what the user said to a scenario, load that one playbook, and follow
it end to end — don't assemble your own procedure from the references:

| The user is saying | Scenario | Playbook |
|---|---|---|
| "I made a website, how do people see it" / "put my project online" / drops a repo | Go live with their project | [scenario-deploy-project.md](references/scenario-deploy-project.md) |
| "deploy metabase" / "install n8n" / names any known product | Deploy a known app | [scenario-deploy-known.md](references/scenario-deploy-known.md) |
| "give me a postgres" / "I need a database" / "somewhere to store files" | Database or storage | [scenario-create-db.md](references/scenario-create-db.md) |
| "site's down" / "it errors" / "it's slow" | Something is broken | [scenario-troubleshoot.md](references/scenario-troubleshoot.md) |
| "scale up" / "change a setting" / "update the version" | Adjust resources | [scenario-adjust.md](references/scenario-adjust.md) |
| "what am I running" / "delete xxx" | Status / delete | [scenario-status-delete.md](references/scenario-status-delete.md) |

Each playbook carries the full steps for its situation (hard rules embedded
where they fire), the pitfalls you'll hit, and how to word the reply.
Usually you load exactly one.

## Hard rules

1. Deploy through the Template API (`sealos-api.py deploy` /
   `deploy-store`), never `kubectl apply` — applied resources are invisible
   to the Sealos UI and don't clean up. (`kubectl apply` is allowed only
   for single resources while debugging.)
2. Source-built images are always built and pushed as linux/amd64.
3. Never report success without evidence: the playbook's verify step must
   have passed.
4. Destructive actions — deleting apps, databases, or buckets; scaling down
   or cutting resources; switching workspace — require explicit user
   confirmation first.
5. Never print secret values. Read them into shell variables or pipe them;
   show the user only names and how to fetch values.
6. Quota or balance errors go to the user; never silently shrink resources
   and retry.
