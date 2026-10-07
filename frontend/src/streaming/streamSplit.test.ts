import { describe, expect, it } from 'vitest'
import { parseStreaming, type MathItem, type ParseResult, type Unit } from './streamSplit'

/** 收集一个单元里的公式项（tex+display），用于稳定性断言 */
function mathOf(u: Unit | undefined): Array<[string, boolean]> {
  if (!u) return []
  const items = u.type === 'text' || u.type === 'heading' || u.type === 'listItem' ? u.items : []
  return items.filter((it): it is MathItem => it.kind === 'math').map((it) => [it.tex, it.display])
}

/**
 * 断言 b 在 a 的基础上"只追加不涂改"：
 * - a 的非尾部单元与 b 的对应单元完全一致；
 * - a 的尾部单元中已出现的公式项在 b 的对应单元中原样保留（位置、内容不变）。
 * 防住的核心风险：后续文本到达时已完成公式被销毁重建（即流式闪动的根源）。
 */
function assertStable(a: ParseResult, b: ParseResult) {
  for (let i = 0; i < a.units.length; i++) {
    if (i < a.units.length - 1) {
      expect(b.units[i]).toEqual(a.units[i])
    } else {
      const am = mathOf(a.units[i])
      const bm = mathOf(b.units[i])
      expect(bm.slice(0, am.length)).toEqual(am)
    }
  }
}

function feed(full: string, cuts: number[]): ParseResult[] {
  const points = [0, ...cuts, full.length]
  const results: ParseResult[] = []
  for (const p of points) results.push(parseStreaming(full.slice(0, p), p === full.length))
  return results
}

describe('streamSplit 前缀稳定性', () => {
  it('后续文本到达时，已完成单元与其中的公式项保持不变（含公式中间被切断）', () => {
    const full = [
      '圆的面积公式是 $S = \\pi r^2$，其中 $r$ 是半径。',
      '',
      '$$S = \\pi r \\times r = \\pi r^2$$',
      '',
      '- 已知直径 $d$：先用 $r = \\frac{d}{2}$，再代入 $S = \\pi r^2$。',
      '- 半圆面积 $S_{半} = \\frac{1}{2}\\pi r^2$。',
      '',
      '结束。',
    ].join('\n')
    // 刻意在公式分隔符与 LaTeX 命令中间切分
    const cuts: number[] = []
    for (const pat of ['$', '$$', '\\frac{', '\\pi', '\\times']) {
      let from = 0
      for (;;) {
        const idx = full.indexOf(pat, from)
        if (idx === -1) break
        cuts.push(idx + Math.min(pat.length, 2))
        from = idx + pat.length
      }
    }
    cuts.push(full.indexOf('$S_{半}') + 4)
    cuts.sort((a, b) => a - b)

    const results = feed(full, cuts)
    for (let k = 1; k < results.length; k++) assertStable(results[k - 1], results[k])
    // 最终结果完整：块级公式与行内公式都在
    const final = results[results.length - 1]
    expect(final.pending).toBe('')
    expect(final.units.some((u) => u.type === 'mathBlock' && u.tex.includes('\\pi r^2'))).toBe(true)
    const allMath = final.units.flatMap(mathOf)
    expect(allMath.length).toBe(6)
  })

  it('代码围栏与行内代码里的 $ 不被当作公式', () => {
    const full = '代码里 price = "$5" 与\n\n```python\nprint("$10 和 $20")\nx = a $ b\n```\n\n结束 `x $ y` 行内。'
    const results = feed(full, [10, 25, 40, 55, 70])
    for (let k = 1; k < results.length; k++) assertStable(results[k - 1], results[k])
    const final = results[results.length - 1]
    const codeUnit = final.units.find((u) => u.type === 'code')
    expect(codeUnit).toBeDefined()
    expect(final.units.flatMap(mathOf)).toEqual([])
    // 围栏内的 $ 原样保留
    expect((codeUnit as { text: string }).text).toContain('$10 和 $20')
  })

  it('未闭合公式先暂存不显示，闭合后立即成单元；结束仍未闭合则按普通文本降级', () => {
    // 流式中：块级公式未闭合 → pending，不产生单元
    const mid = parseStreaming('前文。\n\n$$\\frac{1}{2', false)
    expect(mid.pending).toBe('$$\\frac{1}{2')
    expect(mid.units.some((u) => u.type === 'mathBlock')).toBe(false)

    // 闭合 → 立即成块
    const closed = parseStreaming('前文。\n\n$$\\frac{1}{2}$$\n\n后文', false)
    expect(closed.pending).toBe('')
    expect(closed.units.some((u) => u.type === 'mathBlock' && u.tex === '\\frac{1}{2}')).toBe(true)

    // done 时未闭合行内公式降级为普通文本，原文不丢
    const done = parseStreaming('结果是 $x + y =', true)
    expect(done.pending).toBe('')
    const texts = done.units
      .filter((u) => u.type === 'text')
      .flatMap((u) => (u.type === 'text' ? u.items : []))
      .filter((it) => it.kind === 'text')
      .map((it) => (it as { kind: 'text'; text: string }).text)
      .join('')
    expect(texts).toContain('$x + y =')
  })

  it('无分隔符的普通文本与 \$ 转义、价格美元不被转成公式', () => {
    const full = '这本书价格 $5，另一本 $10。也可以写 \\$100。没有任何公式。'
    const results = feed(full, [8, 16, 24, 32, 40])
    for (let k = 1; k < results.length; k++) assertStable(results[k - 1], results[k])
    const final = results[results.length - 1]
    expect(final.units.flatMap(mathOf)).toEqual([])
    const allText = final.units
      .flatMap((u) => (u.type === 'text' ? u.items : []))
      .map((it) => (it.kind === 'text' ? it.text : ''))
      .join('')
    expect(allText).toContain('$5')
    expect(allText).toContain('$10')
  })

  it('未闭合行内代码先暂存，闭合后成为代码文本，其中的 $ 从未被当作公式', () => {
    const full = '代码：`$x$` 与后续 $y$。'
    const cuts = [
      full.indexOf('`') + 1,
      full.indexOf('$') + 1,
      full.indexOf('$`') + 1,
      full.lastIndexOf('`') + 1,
    ].filter((n) => n > 0 && n < full.length)
    const results = feed(full, cuts)
    for (let k = 1; k < results.length; k++) assertStable(results[k - 1], results[k])
    // 任意前缀都不曾把 x 渲染成公式（防住：先当公式、闭合后又撤销的闪动）
    for (const r of results) {
      expect(r.units.flatMap(mathOf).map(([tex]) => tex)).not.toContain('x')
    }
    const final = results[results.length - 1]
    expect(final.units.flatMap(mathOf)).toContainEqual(['y', false])
    const allText = final.units
      .flatMap((u) => (u.type === 'text' ? u.items : []))
      .map((it) => (it.kind === 'text' ? it.text : ''))
      .join('')
    expect(allText).toContain('$x$')
  })

  it('行内代码到结束仍未闭合：按原文保留且不误判其中的公式', () => {
    const r = parseStreaming('代码：`$x$ 与 \\frac{1}{2}', true)
    expect(r.pending).toBe('')
    expect(r.units.flatMap(mathOf)).toEqual([])
    const allText = r.units
      .flatMap((u) => (u.type === 'text' ? u.items : []))
      .map((it) => (it.kind === 'text' ? it.text : ''))
      .join('')
    expect(allText).toContain('`$x$ 与 \\frac{1}{2}')
  })

  it('段落已结束的未闭合行内代码：反引号到段落尾按原文保留，后续公式正常', () => {
    const r = parseStreaming('a `b $x$\n\nc $y$', true)
    const math = r.units.flatMap(mathOf)
    expect(math).toEqual([['y', false]])
    const allText = r.units
      .flatMap((u) => (u.type === 'text' ? u.items : []))
      .map((it) => (it.kind === 'text' ? it.text : ''))
      .join('')
    expect(allText).toContain('`b $x$')
  })

  it('\\( \\) 与 \\[ \\] 分隔符同样支持', () => {
    const r = parseStreaming('面积 \\(S = \\pi r^2\\) 如下：\n\n\\[S = \\pi r^2\\]\n完', false)
    expect(r.units.some((u) => u.type === 'mathBlock' && u.tex === 'S = \\pi r^2')).toBe(true)
    const inline = r.units.flatMap(mathOf)
    expect(inline).toContainEqual(['S = \\pi r^2', false])
  })

  it('列表、标题、引用行的分类与标记剥离', () => {
    const r = parseStreaming('## 标题 $x$\n\n- 项一 $a$\n- 项二 $b$\n\n> 引用 $c$\n\n正文 $d$', true)
    const headings = r.units.filter((u) => u.type === 'heading')
    expect(headings.length).toBe(1)
    const list = r.units.filter((u) => u.type === 'listItem')
    expect(list.length).toBe(2)
    const quote = r.units.find((u) => u.type === 'text' && u.flavor === 'quote')
    expect(quote).toBeDefined()
    // 首个文本片段不再带标记
    const firstItem = (list[0].items[0] as { kind: string; text?: string })
    expect(firstItem.kind === 'text' ? firstItem.text : '').toBe('项一 ')
  })
})

describe('段落边界与列表起始编号', () => {
  it('单换行不产生空行标记，空行的第二个段落带 blankBefore（渲染层据此分段）', () => {
    const single = parseStreaming('第一段\n第二段', true)
    expect(single.units.length).toBe(2)
    for (const u of single.units) {
      expect(u.type === 'text' && u.blankBefore === true).toBe(false)
    }
    const blank = parseStreaming('第一段\n\n第二段', true)
    expect(blank.units.length).toBe(2)
    expect(blank.units[0]).toMatchObject({ type: 'text', flavor: 'para', blankBefore: undefined })
    expect(blank.units[1]).toMatchObject({ type: 'text', flavor: 'para', blankBefore: true })
    // 单换行与空行的解析结构必须可区分
    expect(blank.units[1]).not.toEqual(single.units[1])
  })

  it('空行标记在流式分批到达时保持前缀稳定（含恰好在两个换行之间切分）', () => {
    const full = '第一段\n\n第二段\n\n第三段'
    const results = feed(full, [4, 5, 6, 8, 9, 10])
    for (let k = 1; k < results.length; k++) assertStable(results[k - 1], results[k])
    const final = results[results.length - 1]
    expect(final.units.map((u) => (u.type === 'text' ? u.blankBefore ?? false : null))).toEqual([
      false,
      true,
      true,
    ])
  })

  it('行内公式后的单个换行不被误判为空行', () => {
    const r = parseStreaming('$x$ 是值\n$y$ 也是', true)
    const paras = r.units.filter((u): u is Extract<Unit, { type: 'text' }> => u.type === 'text')
    expect(paras.length).toBe(2)
    expect(paras.every((u) => u.blankBefore !== true)).toBe(true)
    // 中间的空行（跨 token）则正确标记
    const blank = parseStreaming('$x$ 是值\n\n$y$ 也是', true)
    const paras2 = blank.units.filter((u): u is Extract<Unit, { type: 'text' }> => u.type === 'text')
    expect(paras2.map((u) => u.blankBefore === true)).toEqual([false, true])
  })

  it('有序列表起始编号保留在 start 上，无序列表没有 start', () => {
    const r = parseStreaming('5. 第一项\n6. 第二项', true)
    const items = r.units.filter((u): u is Extract<Unit, { type: 'listItem' }> => u.type === 'listItem')
    expect(items.length).toBe(2)
    expect(items.map((u) => u.start)).toEqual([5, 6])
    expect(items.every((u) => u.blankBefore !== true)).toBe(true)

    const ul = parseStreaming('- 甲\n- 乙', true)
    const ulItems = ul.units.filter((u): u is Extract<Unit, { type: 'listItem' }> => u.type === 'listItem')
    expect(ulItems.map((u) => u.start)).toEqual([undefined, undefined])
  })

  it('空行分隔两个独立有序列表：后一列表带 blankBefore 且用自己的起始编号', () => {
    const r = parseStreaming('1. 第一项\n2. 第二项\n\n5. 后续项\n6. 再续项', true)
    const items = r.units.filter((u): u is Extract<Unit, { type: 'listItem' }> => u.type === 'listItem')
    expect(items.length).toBe(4)
    expect(items.map((u) => u.start)).toEqual([1, 2, 5, 6])
    expect(items.map((u) => u.blankBefore === true)).toEqual([false, false, true, false])
  })

  it('含空行、列表与公式的文本分批到达：既有单元（含标记与编号）逐字节稳定', () => {
    const full = [
      '开头段落，行内公式 $a^2 + b^2$。',
      '',
      '5. 第一项 $x_1$',
      '6. 第二项',
      '',
      '$$E = mc^2$$',
      '',
      '- 无序项一',
      '- 无序项二 $y$',
      '',
      '结尾段落。',
    ].join('\n')
    const cuts: number[] = []
    for (const pat of ['\n\n', '5.', '6.', '- ', '$$', '$x_1$', '$y$']) {
      let from = 0
      for (;;) {
        const idx = full.indexOf(pat, from)
        if (idx === -1) break
        cuts.push(idx + Math.min(pat.length, 2))
        from = idx + pat.length
      }
    }
    cuts.sort((a, b) => a - b)
    const results = feed(full, cuts)
    for (let k = 1; k < results.length; k++) assertStable(results[k - 1], results[k])
    const final = results[results.length - 1]
    const items = final.units.filter((u): u is Extract<Unit, { type: 'listItem' }> => u.type === 'listItem')
    expect(items.map((u) => u.start)).toEqual([5, 6, undefined, undefined])
    expect(final.units.some((u) => u.type === 'mathBlock' && u.tex === 'E = mc^2')).toBe(true)
    // 行内公式 3 个（mathOf 不含块级公式单元）
    expect(final.units.flatMap(mathOf).length).toBe(3)
  })
})
