import { describe, expect, it } from 'vitest'
import { formatAvgSpeed, formatLiveSpeed, liveTokS } from './speed'

describe('liveTokS', () => {
  it('按 2 字符/token 估算并圆整到 0.1', () => {
    expect(liveTokS(100, 10)).toBe(5)
    expect(liveTokS(102, 10)).toBe(5.1)
    expect(liveTokS(100, 3)).toBe(16.7)
  })

  it('字符数或秒数不合法时返回 null', () => {
    expect(liveTokS(0, 5)).toBeNull()
    expect(liveTokS(10, 0)).toBeNull()
    expect(liveTokS(10, -1)).toBeNull()
    expect(liveTokS(-4, 5)).toBeNull()
  })
})

describe('formatLiveSpeed', () => {
  it('带"约"前缀', () => {
    expect(formatLiveSpeed(5.1)).toBe('约 5.1 tok/s')
  })

  it('null 返回空串', () => {
    expect(formatLiveSpeed(null)).toBe('')
  })
})

describe('formatAvgSpeed', () => {
  it('真实平均速度', () => {
    expect(
      formatAvgSpeed({
        tok_s: 12.3,
        completion_tokens: 100,
        gen_seconds: 8.12,
        tok_s_estimated: false,
      }),
    ).toBe('平均 12.3 tok/s（输出 100 tokens · 生成 8.1s）')
  })

  it('估算路径带"约"', () => {
    expect(
      formatAvgSpeed({
        tok_s: 9,
        completion_tokens: 40,
        gen_seconds: 4.44,
        tok_s_estimated: true,
      }),
    ).toBe('平均约 9 tok/s（输出 40 tokens · 生成 4.4s）')
  })

  it('无速度数据返回空串', () => {
    expect(formatAvgSpeed({})).toBe('')
    expect(formatAvgSpeed({ tok_s: null })).toBe('')
  })
})