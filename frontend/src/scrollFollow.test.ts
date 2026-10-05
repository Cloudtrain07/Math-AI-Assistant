import { describe, expect, it } from 'vitest'
import {
  distanceFromBottom,
  isFollowingBottom,
  shouldShowJumpToBottom,
} from './scrollFollow'

/** 按「距底部多少像素」构造几何量，避免手写出矛盾的 scrollTop/scrollHeight 组合 */
function atDistance(distance: number, scrollHeight = 2000, clientHeight = 844) {
  return { scrollTop: scrollHeight - clientHeight - distance, scrollHeight, clientHeight }
}

describe('distanceFromBottom', () => {
  it('贴底为 0，内容增高后为正', () => {
    expect(distanceFromBottom(atDistance(0))).toBe(0)
    expect(distanceFromBottom(atDistance(256))).toBe(256)
  })

  it('不足一屏时不为正（不需要回到底部入口）', () => {
    expect(distanceFromBottom({ scrollTop: 0, scrollHeight: 500, clientHeight: 844 })).toBeLessThan(0)
  })
})

describe('isFollowingBottom（流式输出要不要拉回底部）', () => {
  it('贴底与 80px 内都跟随', () => {
    expect(isFollowingBottom(atDistance(0))).toBe(true)
    expect(isFollowingBottom(atDistance(79))).toBe(true)
  })

  it('向上阅读后不再被拉回底部', () => {
    expect(isFollowingBottom(atDistance(80))).toBe(false)
    expect(isFollowingBottom(atDistance(1200, 3000))).toBe(false)
  })
})

describe('shouldShowJumpToBottom（回到底部入口的显示时机）', () => {
  it('明显离开底部才出现', () => {
    expect(shouldShowJumpToBottom(atDistance(241))).toBe(true)
    expect(shouldShowJumpToBottom(atDistance(1200, 3000))).toBe(true)
  })

  it('轻微滚动与贴底都不出现，避免遮挡正文', () => {
    expect(shouldShowJumpToBottom(atDistance(0))).toBe(false)
    expect(shouldShowJumpToBottom(atDistance(240))).toBe(false)
  })
})
