import { describe, expect, it } from 'vitest'
import { OUTPUT_TOKENS_BOUNDS, WAIT_SECONDS_BOUNDS, parseWebLimit } from './webLimits'

describe('parseWebLimit（网页覆盖值解析）', () => {
  it('空白沿用服务器配置（null），含纯空格', () => {
    expect(parseWebLimit('', OUTPUT_TOKENS_BOUNDS)).toEqual({ ok: true, value: null })
    expect(parseWebLimit('   ', WAIT_SECONDS_BOUNDS)).toEqual({ ok: true, value: null })
  })

  it('范围内整数通过（含边界与 48000 试用值）', () => {
    expect(parseWebLimit('48000', OUTPUT_TOKENS_BOUNDS)).toEqual({ ok: true, value: 48000 })
    expect(parseWebLimit('100', OUTPUT_TOKENS_BOUNDS)).toEqual({ ok: true, value: 100 })
    expect(parseWebLimit('200000', OUTPUT_TOKENS_BOUNDS)).toEqual({ ok: true, value: 200000 })
    expect(parseWebLimit('600', WAIT_SECONDS_BOUNDS)).toEqual({ ok: true, value: 600 })
    expect(parseWebLimit('15', WAIT_SECONDS_BOUNDS)).toEqual({ ok: true, value: 15 })
    expect(parseWebLimit('7200', WAIT_SECONDS_BOUNDS)).toEqual({ ok: true, value: 7200 })
  })

  it('v-model 自动转成的 number 输入同样处理（修复 raw.trim 类型错误）', () => {
    // type=number 输入的 v-model 会把填写值变成 number，留空仍是字符串
    expect(parseWebLimit(48000, OUTPUT_TOKENS_BOUNDS)).toEqual({ ok: true, value: 48000 })
    expect(parseWebLimit(600, WAIT_SECONDS_BOUNDS)).toEqual({ ok: true, value: 600 })
    expect(parseWebLimit(50, OUTPUT_TOKENS_BOUNDS).ok).toBe(false)
    expect(parseWebLimit(12.5, OUTPUT_TOKENS_BOUNDS).ok).toBe(false)
    expect(parseWebLimit(8000, WAIT_SECONDS_BOUNDS).ok).toBe(false)
  })

  it('小数与非数字拒绝并提示整数', () => {
    const decimal = parseWebLimit('48000.5', OUTPUT_TOKENS_BOUNDS)
    expect(decimal.ok).toBe(false)
    if (!decimal.ok) expect(decimal.message).toContain('整数')
    const nan = parseWebLimit('abc', WAIT_SECONDS_BOUNDS)
    expect(nan.ok).toBe(false)
    if (!nan.ok) expect(nan.message).toContain('整数')
  })

  it('越界拒绝并给出范围', () => {
    const low = parseWebLimit('50', OUTPUT_TOKENS_BOUNDS)
    expect(low.ok).toBe(false)
    if (!low.ok) expect(low.message).toContain('100')
    const high = parseWebLimit('200001', OUTPUT_TOKENS_BOUNDS)
    expect(high.ok).toBe(false)
    if (!high.ok) expect(high.message).toContain('200000')
    const wait = parseWebLimit('8000', WAIT_SECONDS_BOUNDS)
    expect(wait.ok).toBe(false)
    if (!wait.ok) expect(wait.message).toContain('7200')
  })

  it('错误信息带字段名', () => {
    const bad = parseWebLimit('0', OUTPUT_TOKENS_BOUNDS)
    expect(bad.ok).toBe(false)
    if (!bad.ok) expect(bad.message).toContain('最大输出 token')
  })
})
