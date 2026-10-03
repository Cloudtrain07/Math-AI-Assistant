import MarkdownIt from 'markdown-it'
import katex from 'katex'

/**
 * markdown-it：html:false 让模型输出的原生 HTML 全部转义（安全边界），
 * linkify 关闭避免普通文本被胡乱转成链接。
 * 片段都是行内内容，用 renderInline（不包 <p>）。
 */
const md = new MarkdownIt({ html: false, linkify: false, breaks: false })

// 缓存有界（FIFO 逐出），避免长会话中无限增长。
// 逐出是安全的：渲染是确定性的，重新渲染产出相同 HTML 字符串，
// v-html 值不变就不会触碰 DOM，不影响公式稳定性。
const MAX_INLINE_CACHE = 2000
const MAX_MATH_CACHE = 1000

function putBounded(cache: Map<string, string>, max: number, key: string, value: string): void {
  if (cache.size >= max && !cache.has(key)) {
    const oldest = cache.keys().next().value
    if (oldest !== undefined) cache.delete(oldest)
  }
  cache.set(key, value)
}

const inlineCache = new Map<string, string>()

export function renderInlineHtml(text: string): string {
  if (text === '') return ''
  let html = inlineCache.get(text)
  if (html === undefined) {
    html = md.renderInline(text)
    putBounded(inlineCache, MAX_INLINE_CACHE, text, html)
  }
  return html
}

const mathCache = new Map<string, string>()

/**
 * KaTeX 同步渲染并按 tex+displayMode 缓存。
 * throwOnError:false → 非法 LaTeX 输出红色错误样式并保留原文，不抛异常不丢内容。
 * trust:false → 禁用 \href 等可能引入外部内容的扩展（安全边界）。
 */
export function renderMathHtml(tex: string, display: boolean): string {
  const key = (display ? 'D:' : 'I:') + tex
  let html = mathCache.get(key)
  if (html === undefined) {
    html = katex.renderToString(tex, {
      throwOnError: false,
      displayMode: display,
      strict: false,
      trust: false,
      output: 'htmlAndMathml',
    })
    putBounded(mathCache, MAX_MATH_CACHE, key, html)
  }
  return html
}
