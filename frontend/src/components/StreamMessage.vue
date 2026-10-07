<script setup lang="ts">
import { computed } from 'vue'
import { parseStreaming, type InlineItem, type Unit } from '../streaming/streamSplit'
import { renderInlineHtml, renderMathHtml } from '../streaming/render'

const props = defineProps<{
  /** 累计全文 */
  text: string
  /** 是否已结束（结束时未闭合公式降级为普通文本） */
  done: boolean
  /** 用于生成稳定的调试键（观测器追踪公式节点） */
  scope: string
}>()

const parsed = computed(() => parseStreaming(props.text, props.done))

type InlineUnit = Extract<Unit, { items: InlineItem[] }>
type Child = { key: number; unit: InlineUnit }
type TreeNode =
  | { type: 'block'; gtype: 'p' | 'quote' | 'ul' | 'ol'; key: number; start?: number; children: Child[] }
  | { type: 'unit'; key: number; unit: Unit }

/** 该单元前面出现过空行 → 渲染层不得把它并入上一个同类型块（段落边界） */
function startsAfterBlank(u: Unit): boolean {
  return (u.type === 'text' || u.type === 'listItem') && u.blankBefore === true
}

/**
 * 把行级单元聚合成块级结构（段落/引用/列表）。
 * 只在末尾追加，既有组的 key 与子项 key 不变 → Vue 复用 DOM，公式节点不重建。
 * 空行标记的单元不并入上一组，而是新起一个块：段落间距由 .para/.list 的 margin 产生，
 * 有序列表也因此保留自己的起始编号。
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
      nodes.push({ type: 'unit', key: i, unit })
      continue
    }
    const inline = unit as InlineUnit
    const last = nodes[nodes.length - 1]
    if (last && last.type === 'block' && last.gtype === gt && !startsAfterBlank(unit)) {
      last.children.push({ key: i, unit: inline })
    } else {
      nodes.push({
        type: 'block',
        gtype: gt,
        key: i,
        start: gt === 'ol' && unit.type === 'listItem' ? unit.start : undefined,
        children: [{ key: i, unit: inline }],
      })
    }
  }
  return nodes
})

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
        v-html="renderMathHtml(node.unit.tex, true)"
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
              v-html="renderMathHtml(it.tex, false)"
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
                v-html="renderMathHtml(it.tex, false)"
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
              v-html="renderMathHtml(it.tex, false)"
            ></span>
          </template>
        </li>
      </ul>
      <ol v-else-if="node.type === 'block' && node.gtype === 'ol'" class="list" :start="node.start">
        <li v-for="child in node.children" :key="child.key">
          <template v-for="(it, ii) in child.unit.items" :key="ii">
            <span v-if="isText(it)" class="frag" v-html="renderInlineHtml(it.text)"></span>
            <span
              v-else
              class="math-inline"
              :data-stream-math="fragKey(child.key, ii)"
              v-html="renderMathHtml(it.tex, false)"
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
            v-html="renderMathHtml(it.tex, false)"
          ></span>
        </template>
      </component>
    </template>
  </div>
</template>

<style scoped>
.stream-body {
  overflow-wrap: break-word;
  min-width: 0;
}
.line {
  display: block;
}
.frag :deep(p) {
  margin: 0;
}
.math-block {
  overflow-x: auto;
  margin: 1em 0;
}
.math-inline {
  white-space: nowrap;
}
.code-block {
  overflow-x: auto;
  background: var(--surface, #1d1f26);
  border: 1px solid var(--line, #343843);
  border-radius: 10px;
  padding: 0.8em 1.1em;
  font-size: 0.9em;
  line-height: 1.5;
}
.heading {
  font-size: 1.06em;
  font-weight: 650;
  margin: 1.3em 0 0.45em;
  color: var(--ink, #e4e6eb);
}
.heading:first-child {
  margin-top: 0.15em;
}
.para {
  margin: 0.5em 0;
}
.list {
  margin: 0.5em 0;
  padding-left: 1.4em;
}
.list li {
  margin: 0.3em 0;
}
.quote {
  margin: 0.8em 0;
  padding: 0.1em 0.95em;
  border-left: 2px solid var(--accent-line, #46538a);
  color: var(--ink-2, #a9afbb);
  background: transparent;
  border-radius: 0;
}
</style>
