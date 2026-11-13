import { describe, expect, it } from 'vitest'
import { collapsedScrollAdjustment } from './collapseAnchor'

/** 桌面常见几何量：页头 53px + 8px 间距，视口 800px，底部余量 24px */
const DESKTOP = { stickyTop: 61, viewportHeight: 800, bottomReserve: 24 }

describe('collapsedScrollAdjustment（思考区折叠的滚动校正）', () => {
  it('思考块整体可见时折叠不移动（无跳变）', () => {
    // 顶边 300 在视口内，折叠后块高 40 → 期望位置仍为 300
    expect(
      collapsedScrollAdjustment({ ...DESKTOP, topBefore: 300, topAfter: 300, blockHeightAfter: 40 }),
    ).toBe(0)
  })

  it('从思考区中下部折叠：块顶回到吸附位下方，视线留在同一条消息', () => {
    // 顶边已在视口上方 5000px（正在读思考区深处）
    const delta = collapsedScrollAdjustment({
      ...DESKTOP,
      topBefore: -5000,
      topAfter: -5000,
      blockHeightAfter: 40,
    })
    expect(delta).toBe(-5061) // scrollTop 上移 5000+61，块顶落到吸附位 61
  })

  it('折叠后块高超过视口：顶边不低于吸附位', () => {
    // lowestTop = max(61, 800-2000-24) = 61
    const delta = collapsedScrollAdjustment({
      ...DESKTOP,
      topBefore: -300,
      topAfter: -300,
      blockHeightAfter: 2000,
    })
    expect(delta).toBe(-361)
    const after = -300 - delta
    expect(after).toBe(61)
  })

  it('顶边贴近视口底部时按块高留出可见空间', () => {
    // topBefore=790（几乎贴底），块高 40 → lowestTop = 800-40-24 = 736 → 上移 54
    const delta = collapsedScrollAdjustment({
      ...DESKTOP,
      topBefore: 790,
      topAfter: 790,
      blockHeightAfter: 40,
    })
    expect(delta).toBe(54)
  })

  it('用重测的 topAfter 校正并发布局偏差', () => {
    // 折叠期间流式更新等原因使实际位置偏离预期：按重测值校正回期望位置
    const delta = collapsedScrollAdjustment({
      ...DESKTOP,
      topBefore: 300,
      topAfter: 200,
      blockHeightAfter: 40,
    })
    expect(delta).toBe(-100)
  })

  it('吸附近下（页头更高，如手机两行页头）同样成立', () => {
    const mobile = { stickyTop: 100, viewportHeight: 700, bottomReserve: 24 }
    const delta = collapsedScrollAdjustment({
      ...mobile,
      topBefore: -2000,
      topAfter: -2000,
      blockHeightAfter: 40,
    })
    expect(-2000 - delta).toBe(100)
  })
})
