import { describe, expect, it } from 'vitest'
import { blockSendWhileHistoryBusy, HISTORY_LOAD_ERROR_TEXT } from './historyGate'

describe('历史恢复门禁', () => {
  it('恢复进行中锁定发送', () => {
    expect(blockSendWhileHistoryBusy(true, '')).toBe(true)
  })

  it('读取失败锁定发送（防止界面空对话、模型旧上下文的分歧）', () => {
    expect(blockSendWhileHistoryBusy(false, HISTORY_LOAD_ERROR_TEXT)).toBe(true)
  })

  it('恢复成功且无错误时放行', () => {
    expect(blockSendWhileHistoryBusy(false, '')).toBe(false)
  })

  it('失败提示包含两个出口：重试恢复 / 新对话显式清空', () => {
    expect(HISTORY_LOAD_ERROR_TEXT).toContain('重试')
    expect(HISTORY_LOAD_ERROR_TEXT).toContain('新对话')
    expect(HISTORY_LOAD_ERROR_TEXT).toContain('服务端可能仍保留旧对话')
  })
})
