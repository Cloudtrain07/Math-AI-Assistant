/**
 * 流式公式解析器（纯函数，无 DOM 依赖）。
 *
 * 目标：把"累计全文"切成可增量渲染的单元序列，并保证前缀稳定性——
 * 文本继续到达时，除尾部单元外，既有单元与其中的公式项逐字节不变。
 * 这样渲染层可以用索引作 key 复用 DOM，已完成的公式永远不会被销毁重画。
 *
 * 分隔符规则与 core/math_format.py 对齐：$...$ 行内、$$...$$ 块级，
 * 另外直接支持 \(...\) 与 \[...\]（math_format 会把它们归一化为 $/$$，
 * 这里双保险，原型阶段不依赖后端归一化）。
 * 行内代码 span（`...`）与代码围栏优先级最高，其中的 $ 永远不是公式；
 * 未闭合的行内代码在段落结束前整体暂存，段落结束或流结束时按原文保留，
 * 绝不出现"先当公式、闭合后又撤销"的稳定性违约。
 */

export interface TextItem {
  kind: 'text'
  text: string
}

export interface MathItem {
  kind: 'math'
  tex: string
  display: boolean
}

export type InlineItem = TextItem | MathItem

export type Unit =
  | { type: 'text'; flavor: 'para' | 'quote'; items: InlineItem[] }
  | { type: 'heading'; level: number; items: InlineItem[] }
  | { type: 'listItem'; ordered: boolean; items: InlineItem[] }
  | { type: 'mathBlock'; tex: string }
  | { type: 'code'; lang: string; text: string; closed: boolean }

export interface ParseResult {
  units: Unit[]
  /** 未闭合公式暂存的原文（不渲染）；done 时为空串 */
  pending: string
}

type RawToken =
  | { t: 'text'; s: string }
  | { t: 'math'; tex: string; display: boolean }
  | { t: 'fence'; lang: string; body: string; closed: boolean }

function isLineStart(src: string, i: number): boolean {
  return i === 0 || src[i - 1] === '\n'
}

function matchFenceMarker(src: string, i: number): string | null {
  if (!isLineStart(src, i)) return null
  let j = i
  let spaces = 0
  while (src[j] === ' ' && spaces < 3) {
    j++
    spaces++
  }
  const three = src.slice(j, j + 3)
  return three === '```' || three === '~~~' ? three : null
}

/**
 * 行内公式的闭合 $ 查找：只在当前行内找（不跨换行），
 * 且闭合 $ 前一个字符不能是空白或反斜杠。
 * 找不到返回 -1（调用方把该 $ 当普通文本，或行未结束前先暂存）。
 */
function findInlineClose(src: string, contentStart: number, lineEnd: number): number {
  for (let k = contentStart; k < lineEnd; k++) {
    if (src[k] === '$' && k > contentStart && src[k - 1] !== ' ' && src[k - 1] !== '\\') {
      return k
    }
  }
  return -1
}

function backtickRunAt(src: string, i: number): number {
  let run = 0
  while (src[i + run] === '`') run++
  return run
}

/** 找到与打开反引号串等长的闭合串位置；只在 limit（段落边界）之前找；找不到返回 -1 */
function findCodeSpanClose(src: string, from: number, run: number, limit: number): number {
  let k = from
  for (;;) {
    const idx = src.indexOf('`'.repeat(run), k)
    if (idx === -1 || idx + run > limit) return -1
    if (src[idx + run] === '`') {
      k = idx + 1
      continue
    }
    return idx
  }
}

/**
 * 第一阶段：字符级扫描，产出 text / math / fence 三类原始 token。
 * done=false 时遇到未闭合公式即停止，剩余部分作为 pending 暂存；
 * done=true 时未闭合公式降级为普通文本（不丢原文）。
 * 行内公式只在单行内匹配：行结束仍无闭合符，该 $ 按普通文本释放，
 * 避免"价格 $5 和 $10"这类文本被误判后长期不可见。
 */
function scanTokens(src: string, done: boolean): { tokens: RawToken[]; pending: string } {
  const tokens: RawToken[] = []
  let i = 0
  let textStart = 0
  let pending = ''

  const pushText = (end: number) => {
    if (end > textStart) tokens.push({ t: 'text', s: src.slice(textStart, end) })
  }
  const holdPending = (at: number) => {
    pushText(at)
    pending = src.slice(at)
  }

  while (i < src.length) {
    const fence = matchFenceMarker(src, i)
    if (fence) {
      pushText(i)
      const lineEnd = src.indexOf('\n', i)
      const openLine = lineEnd === -1 ? src.slice(i) : src.slice(i, lineEnd)
      const lang = openLine.replace(/^[`~]+/, '').trim()
      if (lineEnd === -1) {
        tokens.push({ t: 'fence', lang, body: '', closed: false })
        i = src.length
        textStart = i
        break
      }
      const closeRe = new RegExp(`^\\s*${fence === '```' ? '`' : '~'}{3,}\\s*$`)
      let j = lineEnd + 1
      let closed = false
      for (;;) {
        const nl = src.indexOf('\n', j)
        const line = nl === -1 ? src.slice(j) : src.slice(j, nl)
        if (closeRe.test(line)) {
          tokens.push({ t: 'fence', lang, body: src.slice(lineEnd + 1, Math.max(lineEnd + 1, j - 1)), closed: true })
          i = nl === -1 ? src.length : nl + 1
          closed = true
          break
        }
        if (nl === -1) break
        j = nl + 1
      }
      if (!closed) {
        tokens.push({ t: 'fence', lang, body: src.slice(lineEnd + 1), closed: false })
        i = src.length
      }
      textStart = i
      continue
    }

    const ch = src[i]

    // \$ 转义：整体留在文本里，由 markdown-it 输出字面 $
    if (ch === '\\' && src[i + 1] === '$') {
      i += 2
      continue
    }

    if (ch === '$') {
      if (src.startsWith('$$', i)) {
        const end = src.indexOf('$$', i + 2)
        if (end !== -1) {
          pushText(i)
          tokens.push({ t: 'math', tex: src.slice(i + 2, end).trim(), display: true })
          i = end + 2
          textStart = i
          continue
        }
        if (!done) {
          holdPending(i)
          return { tokens, pending }
        }
        i += 2
        continue
      }
      // 行内 $：打开符后不能是空白；只在当前行内找闭合符
      const next = src[i + 1]
      const lineEnd = (() => {
        const nl = src.indexOf('\n', i)
        return nl === -1 ? src.length : nl
      })()
      if (next !== undefined && next !== '$' && !/\s/.test(next) && i + 1 < lineEnd) {
        const close = findInlineClose(src, i + 1, lineEnd)
        if (close !== -1) {
          pushText(i)
          tokens.push({ t: 'math', tex: src.slice(i + 1, close).trim(), display: false })
          i = close + 1
          textStart = i
          continue
        }
      }
      // 行未结束仍可能闭合 → 暂存等待；行已完整（到行尾都没有闭合符）→ 按普通文本释放
      if (lineEnd === src.length && !done) {
        holdPending(i)
        return { tokens, pending }
      }
      i += 1
      continue
    }

    if (ch === '\\' && (src.startsWith('\\[', i) || src.startsWith('\\(', i))) {
      const display = src[i + 1] === '['
      const closeCh = display ? '\\]' : '\\)'
      const end = src.indexOf(closeCh, i + 2)
      if (end !== -1) {
        pushText(i)
        tokens.push({ t: 'math', tex: src.slice(i + 2, end).trim(), display })
        i = end + 2
        textStart = i
        continue
      }
      if (!done) {
        holdPending(i)
        return { tokens, pending }
      }
      i += 2
      continue
    }

    // 行内代码span：其中的 $ 不参与公式匹配。
    // 代码 span 不跨空行（与 CommonMark 一致），闭合符只在当前段落内找。
    // 未闭合时不能继续向后扫描（否则其中的 $ 会被先当公式、闭合后又撤销）：
    // - 段落还没结束（流式进行中）→ 从反引号起整体暂存，等闭合符到达；
    // - 段落已结束（出现空行）或流已结束 → 反引号到段落尾按普通文本原样保留，
    //   其中的 $ 不误判为公式。
    if (ch === '`') {
      const run = backtickRunAt(src, i)
      const blankIdx = src.indexOf('\n\n', i)
      const boundary = blankIdx === -1 ? src.length : blankIdx
      const close = findCodeSpanClose(src, i + run, run, boundary)
      if (close !== -1) {
        i = close + run
        continue
      }
      if (boundary === src.length && !done) {
        holdPending(i)
        return { tokens, pending }
      }
      i = boundary
      continue
    }

    i += 1
  }

  pushText(src.length)
  return { tokens, pending: '' }
}

type LineKind = 'blank' | 'para' | 'quote' | 'heading' | 'list'

interface LineClass {
  kind: LineKind
  level?: number
  ordered?: boolean
  stripLen: number
}

function classifyLine(raw: string): LineClass {
  const trimmed = raw.trim()
  if (trimmed === '') return { kind: 'blank', stripLen: raw.length }
  const indent = raw.length - raw.trimStart().length
  if (indent > 3) return { kind: 'para', stripLen: 0 }
  const rest = raw.trimStart()
  const base = raw.length - rest.length
  if (rest.startsWith('>')) {
    return { kind: 'quote', stripLen: base + (rest[1] === ' ' ? 2 : 1) }
  }
  const h = /^#{1,6}(?=\s|$)/.exec(rest)
  if (h) {
    const after = rest.slice(h[0].length)
    return { kind: 'heading', level: h[0].length, stripLen: base + h[0].length + (after.startsWith(' ') ? 1 : 0) }
  }
  const ul = /^[-*+]\s+/.exec(rest)
  if (ul) return { kind: 'list', ordered: false, stripLen: base + ul[0].length }
  const ol = /^\d{1,9}[.)]\s+/.exec(rest)
  if (ol) return { kind: 'list', ordered: true, stripLen: base + ol[0].length }
  return { kind: 'para', stripLen: 0 }
}

interface LineBuf {
  raw: string
  items: InlineItem[]
}

function closeLine(buf: LineBuf, units: Unit[]): void {
  const cls = classifyLine(buf.raw)
  if (cls.kind === 'blank') return
  let items = buf.items
  if (cls.stripLen > 0 && items.length > 0 && items[0].kind === 'text') {
    const first = items[0] as TextItem
    const cut = Math.min(cls.stripLen, first.text.length)
    const rest = first.text.slice(cut)
    items = rest === '' ? items.slice(1) : [{ kind: 'text', text: rest }, ...items.slice(1)]
  }
  if (items.length === 0) return
  if (cls.kind === 'heading' && cls.level) {
    units.push({ type: 'heading', level: cls.level, items })
  } else if (cls.kind === 'list') {
    units.push({ type: 'listItem', ordered: cls.ordered === true, items })
  } else if (cls.kind === 'quote') {
    units.push({ type: 'text', flavor: 'quote', items })
  } else {
    units.push({ type: 'text', flavor: 'para', items })
  }
}

function buildUnits(tokens: RawToken[]): Unit[] {
  const units: Unit[] = []
  let cur: LineBuf | null = null

  const flush = () => {
    if (cur) {
      closeLine(cur, units)
      cur = null
    }
  }
  const openLine = () => {
    if (!cur) cur = { raw: '', items: [] }
  }
  const appendText = (s: string) => {
    openLine()
    const last = cur!.items[cur!.items.length - 1]
    if (last && last.kind === 'text') last.text += s
    else cur!.items.push({ kind: 'text', text: s })
    cur!.raw += s
  }

  for (const tok of tokens) {
    if (tok.t === 'fence') {
      flush()
      units.push({ type: 'code', lang: tok.lang, text: tok.body, closed: tok.closed })
      continue
    }
    if (tok.t === 'math') {
      if (tok.display) {
        flush()
        units.push({ type: 'mathBlock', tex: tok.tex })
      } else {
        openLine()
        cur!.raw += `$${tok.tex}$`
        cur!.items.push({ kind: 'math', tex: tok.tex, display: false })
      }
      continue
    }
    const parts = tok.s.split('\n')
    parts.forEach((part, idx) => {
      if (idx > 0) flush()
      if (part !== '') appendText(part)
    })
  }
  flush()
  return units
}

/**
 * 解析累计全文。前缀稳定性约定：
 * - 未闭合的行内公式：若所在行尚未写完（还没有换行符），从 $ 起整体进入 pending 暂存；
 *   换行后仍无闭合符则该 $ 降级为普通文本。
 * - 未闭合的块级公式（$$ / \[）：从打开符起整体进入 pending 暂存，闭合后立即成块渲染。
 * - done=true 时所有未闭合内容按普通文本原样输出（pending 为空）。
 */
export function parseStreaming(src: string, done: boolean): ParseResult {
  const { tokens, pending } = scanTokens(src, done)
  return { units: buildUnits(tokens), pending: done ? '' : pending }
}
