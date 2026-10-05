/**
 * 公式 DOM 稳定性观测 v2（可复用）：
 * 追踪每个公式容器（data-stream-math）及其内部的 KaTeX 排版节点：
 * - 容器多次创建 → 重建异常；
 * - 容器被移除 → 删除异常（流式中任何已登记容器被移除都算异常）；
 * - 容器内部子节点被替换/增删 → 内部替换异常。
 *
 * 正常清空/重播不由 DOM 变化推断：只有 reset() 这个显式操作会同步清空记录，
 * 且它总是发生在引发 DOM 清空的状态变更之前，因此流式中出现的任何删除都无法
 * 被「当作清空」漏报。
 *
 * 该模块不依赖任何框架，可被 #demo 原型与验收脚本（浏览器内注入）共用。
 */

export interface FormulaProbeRecord {
  creations: number
  removals: number
  internalReplacements: number
  signature: string
  samples: string[]
}

export type FormulaAnomalyType = 'recreated' | 'deleted' | 'internal-replaced'

export interface FormulaProbeAnomaly {
  key: string
  type: FormulaAnomalyType
  at: number
  detail: string
}

export interface FormulaProbeStats {
  containers: number
  creations: number
  removals: number
  internalReplacements: number
  anomalies: FormulaProbeAnomaly[]
}

export interface FormulaProbeOptions {
  /** 读取当前「分片/帧」计数，用于给异常打时间标签（缺省恒为 0）。 */
  getTick?: () => number
  /** 每次 DOM 变更后回调一份统计快照（供 Vue 响应式更新或外部读取）。 */
  onChange?: (stats: FormulaProbeStats) => void
}

export interface FormulaProbeHandle {
  /** 显式清空观测记录（唯一允许清空的入口，须发生在引发 DOM 清空的状态变更之前）。 */
  reset(): void
  /** 当前统计快照（深拷贝，可安全保存）。 */
  snapshot(): FormulaProbeStats
  disconnect(): void
}

function describeNode(n: Node): string {
  if (!(n instanceof Element)) return n.nodeName
  const cls = n.getAttribute('class') ?? ''
  return `${n.tagName.toLowerCase()}${cls ? '.' + cls.split(' ')[0] : ''}`
}

function keysIn(root: Element): string[] {
  if (root.hasAttribute('data-stream-math')) return [root.getAttribute('data-stream-math')!]
  return Array.from(root.querySelectorAll('[data-stream-math]')).map(
    (el) => el.getAttribute('data-stream-math')!,
  )
}

export function installFormulaProbe(
  root: Element,
  options: FormulaProbeOptions = {},
): FormulaProbeHandle {
  const getTick = options.getTick ?? (() => 0)
  const records = new Map<string, FormulaProbeRecord>()
  const anomalies: FormulaProbeAnomaly[] = []

  function recordAnomaly(key: string, type: FormulaAnomalyType, detail: string) {
    anomalies.push({ key, type, at: getTick(), detail })
  }

  function statsSnapshot(): FormulaProbeStats {
    let creations = 0
    let removals = 0
    let internalReplacements = 0
    for (const rec of records.values()) {
      creations += rec.creations
      removals += rec.removals
      internalReplacements += rec.internalReplacements
    }
    return {
      containers: records.size,
      creations,
      removals,
      internalReplacements,
      anomalies: anomalies.map((a) => ({ ...a })),
    }
  }

  function emit() {
    options.onChange?.(statsSnapshot())
  }

  function onMutations(mutations: MutationRecord[]) {
    const addedKeys = new Set<string>()
    const removedKeys = new Set<string>()
    const internalHits: { key: string; detail: string }[] = []

    for (const m of mutations) {
      m.addedNodes.forEach((n) => {
        if (!(n instanceof Element)) return
        for (const key of keysIn(n)) {
          addedKeys.add(key)
          const rec =
            records.get(key) ??
            {
              creations: 0,
              removals: 0,
              internalReplacements: 0,
              signature: '',
              samples: [],
            }
          rec.creations += 1
          if (!rec.signature) {
            rec.signature = `${n.children.length}个子节点/${n.outerHTML.length}字符`
          }
          records.set(key, rec)
          if (rec.creations > 1) recordAnomaly(key, 'recreated', `容器第 ${rec.creations} 次创建`)
        }
      })
      m.removedNodes.forEach((n) => {
        if (n instanceof Element) for (const key of keysIn(n)) removedKeys.add(key)
      })
      // 内部替换：变更目标位于某个已有公式容器内部（容器自身不是本批新增）
      const target = m.target instanceof Element ? m.target : m.target.parentElement
      if (target && (m.addedNodes.length > 0 || m.removedNodes.length > 0)) {
        const container = target.closest('[data-stream-math]')
        const key = container?.getAttribute('data-stream-math')
        if (container && key && !addedKeys.has(key)) {
          const rec = records.get(key)
          if (rec) {
            rec.internalReplacements += 1
            const detail =
              [
                ...Array.from(m.removedNodes).map(describeNode),
                ...Array.from(m.addedNodes).map(describeNode),
              ]
                .filter((s) => s !== '')
                .slice(0, 4)
                .join(' / ') || '(子节点变更)'
            if (rec.samples.length < 3) rec.samples.push(detail)
            internalHits.push({ key, detail })
          }
        }
      }
    }

    // 说明：这里刻意没有「批量移除=正常清空」的推断——那种规则会在页面只有一个
    // 公式时把流式中的错误删除漏报成清空。清空只能由 reset() 触发。
    for (const key of removedKeys) {
      const rec = records.get(key)
      if (rec) {
        rec.removals += 1
        recordAnomaly(
          key,
          'deleted',
          `容器被移除（此前创建 ${rec.creations} 次，签名 ${rec.signature}）`,
        )
      }
    }
    for (const hit of internalHits) {
      recordAnomaly(hit.key, 'internal-replaced', `内部排版节点被替换：${hit.detail}`)
    }
    emit()
  }

  const observer = new MutationObserver(onMutations)
  observer.observe(root, { childList: true, subtree: true })

  return {
    reset() {
      records.clear()
      anomalies.length = 0
      emit()
    },
    snapshot: statsSnapshot,
    disconnect() {
      observer.disconnect()
    },
  }
}
