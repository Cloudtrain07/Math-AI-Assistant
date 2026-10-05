/**
 * 滚动跟随判定（纯计算，不接触 DOM）：
 * 页面唯一的滚动容器把几何量传进来，这里只回答「是否贴着底部」「是否需要回到底部」。
 */
export interface ScrollGeometry {
  scrollTop: number
  scrollHeight: number
  clientHeight: number
}

/** 距底部小于该值视为正在跟读最新内容：流式输出继续自动贴底 */
export const FOLLOW_RANGE = 80

/** 距底部超过该值才提供「回到底部」：轻微滚动不冒泡，避免遮挡正文 */
export const JUMP_RANGE = 240

export function distanceFromBottom(el: ScrollGeometry): number {
  return el.scrollHeight - el.scrollTop - el.clientHeight
}

/** 用户是否仍贴着底部（决定流式输出要不要拉回底部） */
export function isFollowingBottom(el: ScrollGeometry): boolean {
  return distanceFromBottom(el) < FOLLOW_RANGE
}

/** 是否明显离开底部、需要提供回到底部的入口 */
export function shouldShowJumpToBottom(el: ScrollGeometry): boolean {
  return distanceFromBottom(el) > JUMP_RANGE
}
