import { describe, expect, it } from 'vitest'
import { composeDaily, composeNotice } from './notice'
import type { DonePayload } from './api'

function base(overrides: Partial<DonePayload> = {}): DonePayload {
  return {
    answer: '回答',
    reasoning_text: '',
    truncated: false,
    error: '',
    empty_reply: false,
    turn_total: 10,
    usage_note: '⚡ 本次 输入 4 + 输出 6 ≈ 共 10 tokens',
    cumulative_usage: '📊 本会话累计 10 tokens（共 1 轮）',
    save_note: '',
    save_status: 'saved',
    daily_note: '',
    daily_used: 10,
    global_used: 10,
    daily_limit: 1000000,
    global_daily_limit: 3000000,
    image_status: '',
    ...overrides,
  }
}

describe('composeNotice（save_status 契约对齐）', () => {
  it('保存成功：只显示用量，不追加保存提示', () => {
    const text = composeNotice(base())
    expect(text).toContain('⚡ 本次')
    expect(text).not.toContain('保存')
    expect(text).not.toContain('未写入历史')
  })

  it('保存失败：追加保存失败提示', () => {
    const text = composeNotice(base({ save_status: 'failed', save_note: '⚠️ 历史保存失败，刷新后可能丢失。' }))
    expect(text).toContain('历史保存失败')
  })

  it('stale：追加未保存提示', () => {
    const text = composeNotice(base({ save_status: 'stale', save_note: 'ℹ️ 另一个操作已更新历史，本次回答未保存。' }))
    expect(text).toContain('本次回答未保存')
  })

  it('异常但有部分回答且保存成功：提示已保留', () => {
    const text = composeNotice(
      base({ error: '⚠️ 请求失败（RuntimeError: x）', save_status: 'saved', save_note: '' }),
    )
    expect(text).toContain('请求失败')
    expect(text).toContain('已保留生成的部分内容')
  })

  it('异常但有部分回答且保存失败：提示仅页面显示', () => {
    const text = composeNotice(
      base({
        error: '⚠️ 请求失败（RuntimeError: x）',
        save_status: 'failed',
        save_note: '⚠️ 历史保存失败，刷新后可能丢失。',
      }),
    )
    expect(text).toContain('部分回答仅在当前页面显示，未写入历史')
  })

  it('空回答：按截断与否给出对应提示', () => {
    const empty = composeNotice(base({ answer: '', empty_reply: true, usage_note: '' }))
    expect(empty).toContain('没有生成正式回答')
    const truncated = composeNotice(
      base({ answer: '', empty_reply: true, truncated: true, usage_note: '' }),
    )
    expect(truncated).toContain('思考过程已用完本次输出额度')
  })

  it('截断：提示可以回复继续', () => {
    const text = composeNotice(base({ truncated: true }))
    expect(text).toContain('被截断')
  })
})

describe('composeDaily', () => {
  it('无每日用量时不显示', () => {
    expect(composeDaily(base({ daily_used: null }))).toBe('')
  })

  it('有全局数据时显示双额度', () => {
    const text = composeDaily(base())
    expect(text).toContain('本账号')
    expect(text).toContain('全局')
  })

  it('缺全局统计时只显示本账号', () => {
    const text = composeDaily(base({ global_used: null }))
    expect(text).toContain('本账号')
    expect(text).not.toContain('全局')
  })
})
