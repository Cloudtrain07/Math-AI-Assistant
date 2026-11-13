/**
 * 思考区折叠的滚动校正（纯计算，不接触 DOM）：
 * 折叠只删除思考区自身的高度，其上方内容不动 → 思考块顶边的文档位置不变，
 * 变化的是它在视口中的位置。几何关系纯化在这里，供 App.vue 在折叠后
 * 重新测量实际位置并校正 scrollTop，让同一条消息留在视野内。
 */

export interface CollapseAnchorInput {
  /** 折叠前思考块顶边相对滚动容器视口顶部的位置（像素，可为负） */
  topBefore: number
  /** 折叠后重新测量的顶边位置（流式等并发更新可能带来偏差） */
  topAfter: number
  /** 吸附位：页头实际高度 + 间距（与 CSS 的 sticky top 一致） */
  stickyTop: number
  /** 滚动容器视口高度 */
  viewportHeight: number
  /** 折叠后思考块高度 */
  blockHeightAfter: number
  /** 视口底部保留余量 */
  bottomReserve: number
}

/**
 * 返回 scrollTop 的增量（正值下移、负值上移），0 表示无需移动。
 * 期望位置 = 把折叠前的顶边位置 clamp 到 [吸附位, 视口底部-块高-余量]：
 * - 思考块原本整体可见 → 位置不变，无跳变；
 * - 从思考区中下部折叠（顶边已在视口上方很远）→ 块顶回到吸附位下方，
 *   已有回答随后自然进入视野。
 */
export function collapsedScrollAdjustment(input: CollapseAnchorInput): number {
  const lowestTop = Math.max(
    input.stickyTop,
    input.viewportHeight - input.blockHeightAfter - input.bottomReserve,
  )
  const desiredTop = Math.min(
    Math.max(input.topBefore, input.stickyTop),
    lowestTop,
  )
  return Math.round(input.topAfter - desiredTop)
}
