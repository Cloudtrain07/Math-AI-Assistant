/** 历史恢复门禁：恢复期间或读取失败时锁定发送类操作。

刷新后 /api/history 尚未恢复完成（或读取失败）就发送，服务端会把
已保存的旧对话拼进本轮请求——界面上却像开了个空的新对话。
「模型仍记得旧对话」的根源就是这个分歧；门禁强制先恢复成功
（或用「新对话」显式清空服务端历史）再发送，两端状态才可能一致。 */

/** 读取失败时展示给用户的提示：服务端历史并未丢失，可重试或显式清空。 */
export const HISTORY_LOAD_ERROR_TEXT =
  '⚠️ 聊天记录读取失败。服务端可能仍保留旧对话：点「重试」恢复后继续，或用「新对话」明确清空后再发送。'

/** 恢复进行中或处于读取失败态时，锁定发送 / 继续生成类操作。 */
export function blockSendWhileHistoryBusy(
  historyLoading: boolean,
  historyError: string,
): boolean {
  return historyLoading || historyError !== ''
}
