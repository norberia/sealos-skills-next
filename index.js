import { readFileSync } from 'node:fs'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { parse as parseYaml } from 'yaml'

/** Same rank as packaged dsh skill providers (`BUNDLED_SKILL_RANK` in `@deepseek-ai/dsh-skill`). */
const BUNDLED_SKILL_RANK = 600
const SKILL_NAME = /^[a-z0-9]+(?:-[a-z0-9]+)*$/
const PROVIDER_NAME = 'sealos'
const SKILL_DIR = fileURLToPath(new URL('./plugins/sealos/skills/use-sealos/', import.meta.url))
const SKILL_FILE = fileURLToPath(new URL('./plugins/sealos/skills/use-sealos/SKILL.md', import.meta.url))
const RESOURCE_BASE = { kind: 'directory', path: SKILL_DIR }
const INVOCATION = { modelInvocable: true, userInvocable: true }

export const name = 'sealos-skill'
export const inject = ['skills']

/**
 * Register the packaged `use-sealos` skill on `ctx.skills`.
 * @param {import('@deepseek-ai/cordis').Context} ctx
 */
export function apply(ctx) {
  parseSkillFile(readFileSync(SKILL_FILE, 'utf8'))
  ctx.skills.registerProvider(() => ({
    name: PROVIDER_NAME,
    async list(options) {
      options?.signal?.throwIfAborted()
      const skill = await loadSkill(options?.signal)
      options?.signal?.throwIfAborted()
      return [toCandidate(skill)]
    },
    async get(candidate, options) {
      options?.signal?.throwIfAborted()
      const skill = await loadSkill(options?.signal)
      if (candidate.name !== skill.name) return undefined
      return toDefinition(skill)
    },
  }))
}

async function loadSkill(signal) {
  return parseSkillFile(await readFile(SKILL_FILE, { encoding: 'utf8', signal }))
}

function parseSkillFile(raw) {
  const parsed = parseFrontmatter(raw)
  if (parsed === undefined) {
    throw new Error(`dsh-plugin-sealos: ${SKILL_FILE} is missing YAML frontmatter`)
  }
  const skillName = stringField(parsed.data, 'name')
  const description = stringField(parsed.data, 'description')
  if (skillName === undefined || description === undefined) {
    throw new Error(`dsh-plugin-sealos: ${SKILL_FILE} frontmatter requires name and description`)
  }
  if (!SKILL_NAME.test(skillName)) {
    throw new Error(`dsh-plugin-sealos: invalid skill name "${skillName}"`)
  }
  return {
    name: skillName,
    description,
    invocation: INVOCATION,
    provider: PROVIDER_NAME,
    source: 'bundled',
    resourceBase: RESOURCE_BASE,
    rank: BUNDLED_SKILL_RANK,
    locator: SKILL_FILE,
    path: SKILL_FILE,
    content: parsed.body.trim(),
  }
}

function toCandidate(skill) {
  return {
    name: skill.name,
    description: skill.description,
    invocation: skill.invocation,
    provider: skill.provider,
    source: skill.source,
    resourceBase: skill.resourceBase,
    rank: skill.rank,
    locator: skill.locator,
    path: skill.path,
  }
}

function toDefinition(skill) {
  return {
    name: skill.name,
    description: skill.description,
    invocation: skill.invocation,
    provider: skill.provider,
    source: skill.source,
    resourceBase: skill.resourceBase,
    path: skill.path,
    content: skill.content,
  }
}

function parseFrontmatter(raw) {
  const firstLineEnd = raw.indexOf('\n')
  if (firstLineEnd < 0) return undefined
  const firstLine = raw.slice(0, firstLineEnd).replace(/\r$/, '')
  if (firstLine !== '---') return undefined
  const start = firstLineEnd + 1
  const closing = findClosingFrontmatter(raw, start)
  if (closing === undefined) return undefined
  const parsed = parseYaml(raw.slice(start, closing.start))
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) return undefined
  return { data: parsed, body: raw.slice(closing.bodyStart) }
}

function findClosingFrontmatter(raw, start) {
  let lineStart = start
  while (lineStart <= raw.length) {
    const nextNewline = raw.indexOf('\n', lineStart)
    const lineEnd = nextNewline < 0 ? raw.length : nextNewline
    const line = raw.slice(lineStart, lineEnd).replace(/\r$/, '')
    if (line === '---') {
      return { start: lineStart, bodyStart: nextNewline < 0 ? raw.length : nextNewline + 1 }
    }
    if (nextNewline < 0) return undefined
    lineStart = nextNewline + 1
  }
}

function stringField(data, key) {
  const value = data[key]
  if (typeof value !== 'string') return undefined
  const trimmed = value.trim()
  return trimmed.length > 0 ? trimmed : undefined
}
