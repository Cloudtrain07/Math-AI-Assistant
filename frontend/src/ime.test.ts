import { describe, expect, it } from 'vitest'
import { isImeComposing, shouldSendOnEnter } from './ime'

describe('isImeComposing', () => {
  it('组合进行中（isComposing）不发送', () => {
    expect(isImeComposing({ isComposing: true, keyCode: 13 })).toBe(true)
  })

  it('keyCode 229（输入法事件）不发送', () => {
    expect(isImeComposing({ isComposing: false, keyCode: 229 })).toBe(true)
  })

  it('普通 Enter 发送', () => {
    expect(isImeComposing({ isComposing: false, keyCode: 13 })).toBe(false)
  })

  it('缺少字段的事件按普通 Enter 处理', () => {
    expect(isImeComposing({})).toBe(false)
  })
})

describe('shouldSendOnEnter（多行输入发送判定）', () => {
  it('Enter 发送', () => {
    expect(shouldSendOnEnter({ key: 'Enter', shiftKey: false, keyCode: 13 })).toBe(true)
  })

  it('Shift+Enter 换行，不发送', () => {
    expect(shouldSendOnEnter({ key: 'Enter', shiftKey: true, keyCode: 13 })).toBe(false)
  })

  it('输入法组合中的 Enter 不发送', () => {
    expect(shouldSendOnEnter({ key: 'Enter', isComposing: true, keyCode: 229 })).toBe(false)
    expect(shouldSendOnEnter({ key: 'Enter', isComposing: false, keyCode: 229 })).toBe(false)
  })

  it('其它按键不发送', () => {
    expect(shouldSendOnEnter({ key: 'a', keyCode: 65 })).toBe(false)
    expect(shouldSendOnEnter({ key: 'Escape', keyCode: 27 })).toBe(false)
  })

  it('key 缺省时按 Enter 处理（旧事件兼容）', () => {
    expect(shouldSendOnEnter({ keyCode: 13 })).toBe(true)
  })
})
