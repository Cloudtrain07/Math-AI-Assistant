import { describe, expect, it } from 'vitest'
import { SseParser } from './sse'

const encoder = new TextEncoder()

function feed(parser: SseParser, text: string) {
  return parser.push(encoder.encode(text))
}

function names(events: { event: string }[]) {
  return events.map((e) => e.event)
}

describe('SseParser', () => {
  it('解析单条完整事件', () => {
    const p = new SseParser()
    const events = feed(p, 'event: delta\ndata: {"answer":"你好"}\n\n')
    expect(events).toEqual([
      { event: 'delta', data: '{"answer":"你好"}' },
    ])
  })

  it('多条事件合并在一个分片', () => {
    const p = new SseParser()
    const events = feed(
      p,
      'event: start\ndata: {}\n\nevent: delta\ndata: {"answer":"a"}\n\n: ping\n\n',
    )
    expect(names(events)).toEqual(['start', 'delta', 'ping'])
  })

  it('事件被拆进多个分片（边界在 \\n\\n 中间）', () => {
    const p = new SseParser()
    expect(feed(p, 'event: del')).toEqual([])
    expect(feed(p, 'ta\ndata: {"ans')).toEqual([])
    const events = feed(p, 'wer":"x"}\n\n')
    expect(events).toEqual([{ event: 'delta', data: '{"answer":"x"}' }])
  })

  it('UTF-8 中文字符被字节边界截断时不乱码', () => {
    const p = new SseParser()
    const bytes = encoder.encode('data: {"answer":"数学公式"}\n\n')
    // 在多字节字符中间切开
    const cut = 12
    const e1 = p.push(bytes.slice(0, cut))
    expect(e1).toEqual([])
    const e2 = p.push(bytes.slice(cut))
    expect(e2[0].data).toBe('{"answer":"数学公式"}')
  })

  it('心跳注释行映射为 ping 事件', () => {
    const p = new SseParser()
    const events = feed(p, ': ping\n\n')
    expect(events).toEqual([{ event: 'ping', data: '' }])
  })

  it('flush 冲出未完结尾', () => {
    const p = new SseParser()
    feed(p, 'event: done\ndata: {"answer":"末尾无空行"}')
    expect(p.flush()).toEqual([
      { event: 'done', data: '{"answer":"末尾无空行"}' },
    ])
  })

  it('data 内的换行（多行 data）合并还原', () => {
    const p = new SseParser()
    const events = feed(p, 'data: 第一行\ndata: 第二行\n\n')
    expect(events).toEqual([{ event: 'message', data: '第一行\n第二行' }])
  })
})
