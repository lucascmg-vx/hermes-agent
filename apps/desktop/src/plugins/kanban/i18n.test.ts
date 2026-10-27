import { describe, expect, it } from 'vitest'

import { en, KANBAN_LOCALES } from './i18n'

describe('blockKindTip', () => {
  const locales = Object.entries(KANBAN_LOCALES) as [string, typeof en][]

  it.each(locales)('%s gives a resource wait its own tooltip, not the dependency one', (_locale, messages) => {
    const tips = ['dependency', 'needs_input', 'capability', 'transient', 'resource'].map(messages.blockKindTip)

    expect(new Set(tips).size).toBe(tips.length)
  })
})
