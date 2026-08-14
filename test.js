import { fileURLToPath } from 'node:url'
import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { apply } from './index.js'

const skillDir = fileURLToPath(new URL('./plugins/sealos/skills/use-sealos/', import.meta.url))

describe('dsh-plugin-sealos', () => {
  it('registers use-sealos from the packaged SKILL.md', async () => {
    const providers = []
    apply({
      skills: {
        registerProvider(create) {
          providers.push(create())
        },
      },
    })
    assert.equal(providers.length, 1)
    assert.equal(providers[0].name, 'sealos')

    const listed = await providers[0].list()
    assert.equal(listed.length, 1)
    assert.equal(listed[0].name, 'use-sealos')
    assert.match(listed[0].description, /Sealos Cloud/)
    assert.deepEqual(listed[0].resourceBase, { kind: 'directory', path: skillDir })
    assert.equal(listed[0].source, 'bundled')
    assert.equal(listed[0].rank, 600)
    assert.equal('content' in listed[0], false)

    const loaded = await providers[0].get(listed[0])
    assert.equal(loaded.name, 'use-sealos')
    assert.match(loaded.content, /# Use Sealos/)
    assert.doesNotMatch(loaded.content, /^---/)
    assert.deepEqual(loaded.resourceBase, { kind: 'directory', path: skillDir })
    assert.equal('rank' in loaded, false)

    assert.equal(await providers[0].get({ name: 'other-skill' }), undefined)
  })
})
