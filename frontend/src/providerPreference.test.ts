import { describe, expect, it } from 'vitest'
import { selectedProvider, saveProvider } from './providerPreference'

describe('browser provider preference', () => {
  it('isolates accounts and validates availability', () => {
    const data = new Map<string, string>()
    const storage = { getItem: (key: string) => data.get(key) ?? null, setItem: (key: string, value: string) => { data.set(key, value) } } as Storage
    saveProvider(storage, 'alice', 'beta')
    expect(selectedProvider(storage, 'alice', [{id: 'alpha'}, {id: 'beta'}], 'alpha')).toBe('beta')
    expect(selectedProvider(storage, 'bob', [{id: 'alpha'}, {id: 'beta'}], 'alpha')).toBe('alpha')
    expect(selectedProvider(storage, 'alice', [{id: 'alpha'}], 'alpha')).toBe('alpha')
  })
  it('falls back if storage is blocked', () => {
    const storage = { getItem: () => { throw new Error('blocked') }, setItem: () => { throw new Error('blocked') } } as unknown as Storage
    expect(selectedProvider(storage, 'alice', [{id: 'alpha'}], 'alpha')).toBe('alpha')
    expect(() => saveProvider(storage, 'alice', 'alpha')).not.toThrow()
  })
})
