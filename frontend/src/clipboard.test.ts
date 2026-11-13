import { afterEach, describe, expect, it, vi } from 'vitest'
import { copyText } from './clipboard'

afterEach(() => {
  vi.unstubAllGlobals()
})

function stubDocument(execCommand: (command: string) => boolean) {
  const textarea = {
    value: '',
    style: {} as Record<string, string>,
    setAttribute: vi.fn(),
    select: vi.fn(),
  }
  const body = { appendChild: vi.fn(), removeChild: vi.fn() }
  vi.stubGlobal('document', {
    createElement: vi.fn(() => textarea),
    execCommand: vi.fn(execCommand),
    body,
  })
  return { textarea, body }
}

describe('copyText', () => {
  it('安全上下文用 navigator.clipboard 写入', async () => {
    const writeText = vi.fn(async () => {})
    vi.stubGlobal('window', { isSecureContext: true })
    vi.stubGlobal('navigator', { clipboard: { writeText } })
    expect(await copyText('1+1=2')).toBe(true)
    expect(writeText).toHaveBeenCalledWith('1+1=2')
  })

  it('clipboard 写入被拒时走 textarea 兜底，用完移除节点', async () => {
    vi.stubGlobal('window', { isSecureContext: true })
    vi.stubGlobal('navigator', {
      clipboard: {
        writeText: vi.fn(async () => {
          throw new Error('denied')
        }),
      },
    })
    const { textarea, body } = stubDocument(() => true)
    expect(await copyText('abc')).toBe(true)
    expect(textarea.value).toBe('abc')
    expect(textarea.select).toHaveBeenCalled()
    expect(body.appendChild).toHaveBeenCalledWith(textarea)
    expect(body.removeChild).toHaveBeenCalledWith(textarea)
  })

  it('非安全上下文（局域网 HTTP）直接用 textarea 兜底', async () => {
    const writeText = vi.fn()
    vi.stubGlobal('window', { isSecureContext: false })
    vi.stubGlobal('navigator', { clipboard: { writeText } })
    const { body } = stubDocument(() => true)
    expect(await copyText('abc')).toBe(true)
    expect(writeText).not.toHaveBeenCalled()
    expect(body.removeChild).toHaveBeenCalled()
  })

  it('兜底 execCommand 返回 false 时整体返回 false', async () => {
    vi.stubGlobal('window', { isSecureContext: false })
    vi.stubGlobal('navigator', {})
    stubDocument(() => false)
    expect(await copyText('abc')).toBe(false)
  })

  it('execCommand 抛异常时返回 false 且仍移除节点', async () => {
    vi.stubGlobal('window', { isSecureContext: false })
    vi.stubGlobal('navigator', {})
    const { body } = stubDocument(() => {
      throw new Error('unsupported')
    })
    expect(await copyText('abc')).toBe(false)
    expect(body.removeChild).toHaveBeenCalled()
  })
})