/**
 * SSE 增量解析器：把网络分片（Uint8Array）重组为完整事件。
 *
 * 处理三类传输现实：
 * - 一条事件被拆进多个分片（含 \n\n 边界被拆开）；
 * - 多条事件合并在一个分片里；
 * - UTF-8 多字节字符（中文）被字节边界截断（TextDecoder stream 模式负责）。
 * 事件以空行（\n\n）分隔；": " 开头的行为注释（服务端心跳），
 * 映射为 event='ping'，供调用方做活性判断。
 */

export interface SseEvent {
  event: string
  data: string
}

export class SseParser {
  private buffer = ''
  private decoder = new TextDecoder('utf-8')

  /** 送入一个网络分片，返回其中完整的事件（不含正在接收中的尾巴）。 */
  push(bytes: Uint8Array): SseEvent[] {
    this.buffer += this.decoder.decode(bytes, { stream: true })
    return this.drain()
  }

  /** 流结束时调用：冲出解码器尾部并解析剩余缓冲（含无结尾空行的最后一块）。 */
  flush(): SseEvent[] {
    this.buffer += this.decoder.decode()
    const events = this.drain()
    if (this.buffer.trim() !== '') {
      const parsed = parseBlock(this.buffer)
      if (parsed) events.push(parsed)
    }
    this.buffer = ''
    return events
  }

  private drain(): SseEvent[] {
    const events: SseEvent[] = []
    for (;;) {
      const idx = this.buffer.indexOf('\n\n')
      if (idx === -1) break
      const block = this.buffer.slice(0, idx)
      this.buffer = this.buffer.slice(idx + 2)
      const parsed = parseBlock(block)
      if (parsed) events.push(parsed)
    }
    return events
  }
}

function parseBlock(block: string): SseEvent | null {
  let sawComment = false
  let event = 'message'
  const dataLines: string[] = []
  for (const rawLine of block.split('\n')) {
    const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine
    if (line === '') continue
    if (line.startsWith(':')) {
      sawComment = true
    } else if (line.startsWith('event:')) {
      event = line.slice('event:'.length).trim()
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice('data:'.length).replace(/^ /, ''))
    }
  }
  if (dataLines.length > 0) return { event, data: dataLines.join('\n') }
  if (sawComment) return { event: 'ping', data: '' }
  return null
}
