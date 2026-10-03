<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { parseStreaming, type InlineItem, type Unit } from '../streaming/streamSplit'
import { renderInlineHtml, renderMathHtml } from '../streaming/render'

const props = defineProps<{
  /** 累计全文 */
  text: string
  /** 是否已结束（结束时未闭合公式降级为普通文本） */
  done: boolean
  /** 用于生成稳定的调试键（观测器追踪公式节点） */
  scope: string
  /**
   * 故障注入（仅对照验证用，正常播放不传）：
   * - 'replace'：故意让每个公式每次更新都替换内部排版节点；
   * - 'drop'：首个块级公式完成约 1.5 秒后故意从 DOM 中删除它。
   */
  fault?: 'replace' | 'drop'
}>()

const parsed = computed(() => parseStreaming(props.text, props.done))

type InlineUnit = Extract<Unit, { items: InlineItem[] }>
type Child = { key: number; unit: InlineUnit }
type TreeNode =
  | { type: 'block'; gtype: 'p' | 'quote' | 'ul' | 'ol'; key: number; children: Child[] }
  | { type: 'unit'; key: number; unit: Unit }

/**
 * 把行级单元聚合成块级结构（段落/引用/列表）。
 * 只在末尾追加，既有组的 key 与子项 key 不变 → Vue 复用 DOM，公式节点不重建。
 */
const tree = computed<TreeNode[]>(() => {
  const nodes: TreeNode[] = []
  const gtypeOf = (u: Unit): 'p' | 'quote' | 'ul' | 'ol' | null => {
    if (u.type === 'text') return u.flavor === 'quote' ? 'quote' : 'p'
    if (u.type === 'listItem') return u.ordered ? 'ol' : 'ul'
    return null
  }
  for (let i = 0; i < parsed.value.units.length; i++) {
    const unit = parsed.value.units[i]
    const gt = gtypeOf(unit)
    if (gt === null) {
      if (dropArmed.value && unit.type === 'mathBlock' && i === dropKey.value) continue
      nodes.push({ type: 'unit', key: i, unit })
      continue
    }
    const inline = unit as InlineUnit
    const last = nodes[nodes.length - 1]
    if (last && last.type === 'block' && last.gtype === gt) {
      last.children.push({ key: i, unit: inline })
    } else {
      nodes.push({ type: 'block', gtype: gt, key: i, children: [{ key: i, unit: inline }] })
    }
  }
  return nodes
})

let nonce = 0

function mathHtml(tex: string, display: boolean): string {
  const html = renderMathHtml(tex, display)
  if (props.fault !== 'replace') return html
  // 故意让 v-html 字符串每次变化 → 内部 KaTeX 节点被整体替换（注入的故障）
  return `${html}<!--unstable-${nonce++}-->`
}

// 'drop' 故障：首个块级公式出现后计时，到点后把它从渲染树中剔除（DOM 删除）
const dropKey = ref<number | null>(null)
const dropArmed = ref(false)
let dropTimer: ReturnType<typeof setTimeout> | null = null

watch(
  () => props.text,
  (text) => {
    if (props.fault !== 'drop') return
    if (text === '') {
      dropKey.value = null
      dropArmed.value = false
      if (dropTimer !== null) clearTimeout(dropTimer)
      dropTimer = null
      return
    }
    if (dropKey.value !== null) return
    const idx = parsed.value.units.findIndex((u) => u.type === 'mathBlock')
    if (idx >= 0) {
      dropKey.value = idx
      dropTimer = setTimeout(() => {
        dropArmed.value = true
      }, 1500)
    }
  },
)

function fragKey(unitKey: number, itemIndex: number): string {
  return `${props.scope}-${unitKey}-${itemIndex}`
}

function blockMathKey(unitKey: number): string {
  return `${props.scope}-b${unitKey}`
}

function isText(it: InlineItem): it is Extract<InlineItem, { kind: 'text' }> {
  return it.kind === 'text'
}
</script>

<template>
  <div class="stream-body">
    <template v-for="node in tree" :key="node.type === 'block' ? `g${node.key}` : `u${node.key}`">
      <!-- 独立单元：块级公式 / 代码块 -->
      <div
        v-if="node.type === 'unit' && node.unit.type === 'mathBlock'"
        class="math-block"
        :data-stream-math="blockMathKey(node.key)"
        v-html="mathHtml(node.unit.tex, true)"
      ></div>
      <pre v-else-if="node.type === 'unit' && node.unit.type === 'code'" class="code-block"><code>{{ node.unit.text }}</code></pre>

      <!-- 聚合块：段落 / 引用 / 列表 -->
      <p v-else-if="node.type === 'block' && node.gtype === 'p'" class="para">
        <span v-for="child in node.children" :key="child.key" class="line">
          <template v-for="(it, ii) in child.unit.items" :key="ii">
            <span v-if="isText(it)" class="frag" v-html="renderInlineHtml(it.text)"></span>
            <span
              v-else
              class="math-inline"
              :data-stream-math="fragKey(child.key, ii)"
              v-html="mathHtml(it.tex, false)"
            ></span>
          </template>
        </span>
      </p>
      <blockquote v-else-if="node.type === 'block' && node.gtype === 'quote'" class="quote">
        <p class="para">
          <span v-for="child in node.children" :key="child.key" class="line">
            <template v-for="(it, ii) in child.unit.items" :key="ii">
              <span v-if="isText(it)" class="frag" v-html="renderInlineHtml(it.text)"></span>
              <span
                v-else
                class="math-inline"
                :data-stream-math="fragKey(child.key, ii)"
                v-html="mathHtml(it.tex, false)"
              ></span>
            </template>
          </span>
        </p>
      </blockquote>
      <ul v-else-if="node.type === 'block' && node.gtype === 'ul'" class="list">
        <li v-for="child in node.children" :key="child.key">
          <template v-for="(it, ii) in child.unit.items" :key="ii">
            <span v-if="isText(it)" class="frag" v-html="renderInlineHtml(it.text)"></span>
            <span
              v-else
              class="math-inline"
              :data-stream-math="fragKey(child.key, ii)"
              v-html="mathHtml(it.tex, false)"
            ></span>
          </template>
        </li>
      </ul>
      <ol v-else-if="node.type === 'block' && node.gtype === 'ol'" class="list">
        <li v-for="child in node.children" :key="child.key">
          <template v-for="(it, ii) in child.unit.items" :key="ii">
            <span v-if="isText(it)" class="frag" v-html="renderInlineHtml(it.text)"></span>
            <span
              v-else
              class="math-inline"
              :data-stream-math="fragKey(child.key, ii)"
              v-html="mathHtml(it.tex, false)"
            ></span>
          </template>
        </li>
      </ol>

      <!-- 独立单元：标题 -->
      <component
        :is="`h${Math.min(node.unit.level, 6)}`"
        v-else-if="node.type === 'unit' && node.unit.type === 'heading'"
        class="heading"
      >
        <template v-for="(it, ii) in node.unit.items" :key="ii">
          <span v-if="isText(it)" class="frag" v-html="renderInlineHtml(it.text)"></span>
          <span
            v-else
            class="math-inline"
            :data-stream-math="fragKey(node.key, ii)"
            v-html="mathHtml(it.tex, false)"
          ></span>
        </template>
      </component>
    </template>
  </div>
</template>

<style scoped>
.stream-body {
  overflow-wrap: break-word;
}
.line {
  display: block;
}
.frag :deep(p) {
  margin: 0;
}
.math-block {
  overflow-x: auto;
  margin: 0.6em 0;
}
.math-inline {
  white-space: nowrap;
}
.code-block {
  overflow-x: auto;
  background: #f6f8fa;
  border-radius: 6px;
  padding: 0.75em 1em;
  font-size: 0.9em;
  line-height: 1.5;
}
.heading {
  margin: 0.8em 0 0.4em;
}
.para {
  margin: 0.35em 0;
}
.list {
  margin: 0.35em 0;
  padding-left: 1.5em;
}
.quote {
  margin: 0.5em 0;
  padding: 0.1em 0.9em;
  border-left: 4px solid #d0d7de;
  color: #57606a;
}
</style>
