import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { lstatSync, readFileSync, realpathSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = dirname(fileURLToPath(import.meta.url))
const canonicalSkill = join(root, 'plugins/sealos/skills/use-sealos')
const skillsShEntry = join(root, 'skills/use-sealos')
const VERSION = '0.1.0'

function readJson(relative) {
  return JSON.parse(readFileSync(join(root, relative), 'utf8'))
}

describe('host coverage', () => {
  it('Gemini and Qwen extensions load CLAUDE.md as context-only', () => {
    const gemini = readJson('gemini-extension.json')
    const qwen = readJson('qwen-extension.json')
    for (const [name, payload] of [['gemini', gemini], ['qwen', qwen]]) {
      assert.equal(payload.name, 'sealos', name)
      assert.equal(payload.version, VERSION, name)
      assert.equal(payload.contextFileName, 'CLAUDE.md', name)
      assert.match(payload.description, /not claimed/i, name)
    }
    const claudeMd = readFileSync(join(root, 'CLAUDE.md'), 'utf8')
    assert.match(claudeMd, /use-sealos/)
    assert.match(claudeMd, /plugins\/sealos\/skills\/use-sealos\/SKILL.md/)
    assert.match(claudeMd, /skills\/use-sealos\/SKILL.md/)
    assert.doesNotMatch(claudeMd, /\/sealos-deploy/)
  })

  it('OpenClaw is a Claude bundle pointer without a copied skill tree', () => {
    const openclaw = readJson('openclaw.plugin.json')
    assert.equal(openclaw.id, 'sealos')
    assert.equal(openclaw.version, VERSION)
    assert.equal(openclaw.format, 'claude-plugin')
    assert.equal(openclaw.source, 'plugins/sealos/.claude-plugin/plugin.json')
    assert.ok(openclaw.hostTargets.includes('openclaw'))
    assert.equal('skills' in openclaw, false)
    assert.equal('commands' in openclaw, false)
    const source = JSON.parse(
      readFileSync(join(root, openclaw.source), 'utf8'),
    )
    assert.equal(source.name, 'sealos')
    assert.equal(source.version, VERSION)
  })

  it('CodeBuddy marketplace points at plugins/sealos', () => {
    const marketplace = readJson('.codebuddy-plugin/marketplace.json')
    assert.equal(marketplace.version, VERSION)
    assert.equal(marketplace.plugins.length, 1)
    const plugin = marketplace.plugins[0]
    assert.equal(plugin.name, 'sealos')
    assert.equal(plugin.source, './plugins/sealos')
    assert.equal(plugin.version, VERSION)
    assert.equal('commands' in plugin, false)
    assert.equal('skills' in plugin, false)
    const pluginJson = readJson('plugins/sealos/.claude-plugin/plugin.json')
    assert.equal(pluginJson.version, VERSION)
  })

  it('skills.sh entry is a symlink to the canonical use-sealos skill', () => {
    assert.equal(lstatSync(skillsShEntry).isSymbolicLink(), true)
    assert.equal(realpathSync(skillsShEntry), realpathSync(canonicalSkill))
    const skill = readFileSync(join(skillsShEntry, 'SKILL.md'), 'utf8')
    assert.match(skill, /^---\nname: use-sealos\n/)
  })

  it('README documents the six added hosts', () => {
    const readme = readFileSync(join(root, 'README.md'), 'utf8')
    for (const needle of [
      'npx skills add norberia/sealos-skills-next',
      'gemini extensions install https://github.com/norberia/sealos-skills-next',
      'qwen extensions install https://github.com/norberia/sealos-skills-next',
      'openclaw plugins install /path/to/sealos-skills-next/plugins/sealos',
      '/plugin marketplace add norberia/sealos-skills-next',
      'Amp / Kimi',
    ]) {
      assert.match(readme, new RegExp(needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
    }
  })
})
