/**
 * 中文输入法组合输入守卫：组合进行中（选词/确认候选）按下的 Enter
 * 属于输入法事件，不应触发发送。
 * 兼容处理：部分环境在 compositionupdate 期间 keyCode 为 229 且
 * isComposing 不可靠，两者任一命中都视为组合输入。
 */
export function isImeComposing(event: {
  isComposing?: boolean
  keyCode?: number
}): boolean {
  if (event.isComposing) return true
  return event.keyCode === 229
}

/**
 * 多行输入框的发送判定：仅「不带 Shift 的 Enter」且不在输入法组合输入中时发送。
 * - Shift+Enter 交由控件插入换行（不发送）；
 * - 组合输入中的 Enter（选词/确认候选）不发送。
 * key 缺省（部分环境/旧事件）时按 Enter 处理。
 */
export function shouldSendOnEnter(event: {
  key?: string
  shiftKey?: boolean
  isComposing?: boolean
  keyCode?: number
}): boolean {
  if (event.key !== undefined && event.key !== 'Enter') return false
  if (event.shiftKey) return false
  return !isImeComposing(event)
}
