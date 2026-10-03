/**
 * 长回答解析量化测量：parseStreaming 每个分片全量重扫描的成本。
 * 用固定步长（24 字符/片，接近真实 token 节奏）模拟流式，
 * 输出总耗时、平均与单片最大耗时，用于决定是否需要增量扫描。
 */
import { parseStreaming } from '../src/streaming/streamSplit.ts'
import { mockAnswer } from '../src/mock/mockData.ts'

function makeLongAnswer(reps) {
  const parts = []
  for (let k = 0; k < reps; k++) {
    parts.push(mockAnswer.replaceAll('半径为 3', `半径为 ${3 + k}`))
  }
  return parts.join('\n\n')
}

for (const reps of [1, 3, 6, 12, 24]) {
  const text = makeLongAnswer(reps)
  const step = 24
  let total = 0
  let max = 0
  let count = 0
  // 预热 JIT
  parseStreaming(text.slice(0, 4000), false)
  for (let p = step; p < text.length; p += step) {
    const t0 = performance.now()
    parseStreaming(text.slice(0, p), false)
    const dt = performance.now() - t0
    total += dt
    if (dt > max) max = dt
    count++
  }
  const t0 = performance.now()
  parseStreaming(text, true)
  const finalMs = performance.now() - t0
  console.log(
    `${(text.length / 1024).toFixed(0).padStart(4)}KB | 分片 ${String(count).padStart(5)} | ` +
      `累计解析 ${total.toFixed(0).padStart(6)}ms | 平均 ${(total / count).toFixed(2)}ms/片 | ` +
      `最大 ${max.toFixed(2)}ms/片 | 终帧 ${finalMs.toFixed(1)}ms`,
  )
}
