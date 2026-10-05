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

/** 提示种类：决定状态区图标与配色，不只靠文字区分。 */
export type StatusKind = 'info' | 'success' | 'warn' | 'error'

/**
 * 按 done 载荷的**结构化字段**判定收尾状态种类，不搜索中文提示字符串：
 * - error：模型错误 / 历史保存失败（最严重）；
 * - warn：空回答 / 截断 / 图片读取失败 / 每日用量记账失败；
 * - info：保存标记过期（stale，非失败，仅说明本次未落盘）；
 * - success：正常完成。
 * 这是「done ≠ 全部成功」的判定入口。
 */
export function classifyDone(d: DonePayload): StatusKind {
  if (d.error) return 'error'
  if (d.save_status === 'failed') return 'error'
  if (d.empty_reply) return 'warn'
  if (d.truncated) return 'warn'
  if (d.image_status) return 'warn'
  if (d.daily_status === 'record_failed' || d.daily_status === 'stats_failed') {
    return 'warn'
  }
  if (d.save_status === 'stale') return 'info'
  return 'success'
}

export function composeDaily(d: DonePayload): string {
  if (d.daily_used === null) return ''
  const mine = `🛡️ 今日已用 ${d.daily_used.toLocaleString()} / ${d.daily_limit.toLocaleString()}（本账号）`
  if (d.global_used !== null) {
    return `${mine}，全局 ${d.global_used.toLocaleString()} / ${d.global_daily_limit.toLocaleString()}`
  }
  return mine
}
