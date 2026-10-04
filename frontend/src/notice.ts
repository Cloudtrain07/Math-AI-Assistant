/**
 * 收尾提示文案组装：把 done 载荷按语义拼成界面提示（纯函数，可单测）。
 *
 * 分支与后端 save_status 契约对齐：
 * - saved / null：不追加保存提示；
 * - failed：⚠️ 历史保存失败；
 * - stale：ℹ️ 另一个操作已更新历史，本次回答未保存。
 */

import type { DonePayload } from './api'

export function composeNotice(d: DonePayload): string {
  const notes: string[] = []
  if (d.image_status) notes.push(d.image_status)
  if (d.error) {
    notes.push(d.error)
    if (d.answer) {
      notes.push(
        d.save_status === 'saved'
          ? '（已保留生成的部分内容）'
          : '（部分回答仅在当前页面显示，未写入历史）',
      )
    }
  } else if (d.empty_reply) {
    const hint = d.truncated
      ? '模型的思考过程已用完本次输出额度，没能生成正式回答'
      : '请求已完成，但模型没有生成正式回答'
    const cost =
      d.turn_total !== null ? `（本次约消耗 ${d.turn_total} tokens）` : ''
    notes.push(`⚠️ ${hint}${cost}。`)
  } else {
    if (d.usage_note) notes.push(d.usage_note)
    if (d.truncated) notes.push('⚠️ 回答因长度限制被截断，可以回复「继续」让我接着写。')
  }
  if (d.save_note) notes.push(d.save_note)
  if (d.daily_note) notes.push(d.daily_note)
  return notes.join('\n\n')
}

export function composeDaily(d: DonePayload): string {
  if (d.daily_used === null) return ''
  const mine = `🛡️ 今日已用 ${d.daily_used.toLocaleString()} / ${d.daily_limit.toLocaleString()}（本账号）`
  if (d.global_used !== null) {
    return `${mine}，全局 ${d.global_used.toLocaleString()} / ${d.global_daily_limit.toLocaleString()}`
  }
  return mine
}
