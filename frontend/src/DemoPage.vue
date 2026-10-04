<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import StreamMessage from './components/StreamMessage.vue'
import {
  buildChunks,
  mockAnswer,
  mockBadAnswer,
  mockPriceText,
  mockQuestion,
  mockSingleFormula,
  mockThinking,
} from './mock/mockData'

type Scenario = 'normal' | 'thinking' | 'bad' | 'price' | 'unstable' | 'drop'

const userMsg = ref('')
const answerText = ref('')
const thinkingText = ref('')
const answerDone = ref(true)
const thinkingDone = ref(true)
const streaming = ref(false)
const showThinking = ref(true)
const chunkCount = ref(0)
const input = ref('')
const currentScenario = ref<Scenario>('normal')

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

// ---- 公式 DOM 稳定性观测 v2 ----
// 追踪每个公式容器（data-stream-math）及其内部的 KaTeX 排版节点：
// - 容器多次创建 → 重建异常
// - 容器被移除 → 删除异常（流式中任何已登记容器被移除都算异常）
// - 容器内部子节点被替换/增删 → 内部替换异常
// 正常清空/重播不由 DOM 变化推断：只有 play()/reset() 里的 resetDebug()
// 这个显式操作会同步清空记录，且它总是发生在引发 DOM 清空的状态变更之前，
// 因此流式中出现的任何删除都无法被"当作清空"漏报。
interface FormulaRecord {
  creations: number
  removals: number
  internalReplacements: number
  signature: string
  samples: string[]
}

const chatRoot = ref<HTMLElement | null>(null)
let observer: MutationObserver | null = null
const records = new Map<string, FormulaRecord>()
const stats = reactive({
  containers: 0,
  creations: 0,
  removals: 0,
  internalReplacements: 0,
  anomalies: [] as { key: string; type: string; at: number; detail: string }[],
})

function describeNode(n: Node): string {
  if (!(n instanceof Element)) return n.nodeName
  const cls = n.getAttribute('class') ?? ''
  return `${n.tagName.toLowerCase()}${cls ? '.' + cls.split(' ')[0] : ''}`
}

function keysIn(root: Element): string[] {
  if (root.hasAttribute('data-stream-math')) return [root.getAttribute('data-stream-math')!]
  return Array.from(root.querySelectorAll('[data-stream-math]')).map((el) =>
    el.getAttribute('data-stream-math')!,
  )
}

function recordAnomaly(key: string, type: string, detail: string) {
  stats.anomalies.push({ key, type, at: chunkCount.value, detail })
}

function refreshStats() {
  stats.containers = records.size
  stats.creations = 0
  stats.removals = 0
  stats.internalReplacements = 0
  for (const rec of records.values()) {
    stats.creations += rec.creations
    stats.removals += rec.removals
    stats.internalReplacements += rec.internalReplacements
  }
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
        const rec = records.get(key) ?? {
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

  // 说明：这里刻意没有"批量移除=正常清空"的推断——那种规则会在
  // 页面只有一个公式时把流式中的错误删除漏报成清空。
  // 清空只能由 resetDebug()（play/reset 的显式步骤）触发。
  for (const key of removedKeys) {
    const rec = records.get(key)
    if (rec) {
      rec.removals += 1
      recordAnomaly(key, 'deleted', `容器被移除（此前创建 ${rec.creations} 次，签名 ${rec.signature}）`)
    }
  }
  for (const hit of internalHits) {
    recordAnomaly(hit.key, 'internal-replaced', `内部排版节点被替换：${hit.detail}`)
  }
  refreshStats()
}

onMounted(() => {
  observer = new MutationObserver(onMutations)
  if (chatRoot.value) observer.observe(chatRoot.value, { childList: true, subtree: true })
})
onBeforeUnmount(() => observer?.disconnect())

// ---- 播放控制 ----
let stopFlag = false

function resetDebug() {
  records.clear()
  stats.containers = 0
  stats.creations = 0
  stats.removals = 0
  stats.internalReplacements = 0
  stats.anomalies = []
  chunkCount.value = 0
}

async function play(scenario: Scenario, customQuestion?: string) {
  if (streaming.value) return
  stopFlag = false
  streaming.value = true
  currentScenario.value = scenario
  // 显式重置：清空观测记录（这是唯一允许清空记录的入口）
  resetDebug()
  userMsg.value = customQuestion ?? mockQuestion
  answerText.value = ''
  thinkingText.value = ''
  answerDone.value = false
  thinkingDone.value = false
  await nextTick()

  const feed = async (target: 'answer' | 'thinking', text: string, minMs = 16, maxMs = 38) => {
    const chunks = buildChunks(text)
    for (const snap of chunks) {
      if (stopFlag) return
      if (target === 'answer') answerText.value = snap
      else thinkingText.value = snap
      chunkCount.value += 1
      await sleep(minMs + Math.random() * (maxMs - minMs))
    }
  }

  if (scenario === 'thinking') await feed('thinking', mockThinking)
  if (scenario === 'normal' || scenario === 'thinking' || scenario === 'unstable') {
    await feed('answer', mockAnswer)
  } else if (scenario === 'bad') {
    await feed('answer', mockBadAnswer)
  } else if (scenario === 'drop') {
    // 慢速播放，保证删除发生在流式过程中
    await feed('answer', mockSingleFormula, 80, 140)
  } else {
    await feed('answer', mockPriceText)
  }

  answerDone.value = true
  thinkingDone.value = true
  streaming.value = false
}

function stop() {
  stopFlag = true
}

function reset() {
  stop()
  userMsg.value = ''
  answerText.value = ''
  thinkingText.value = ''
  answerDone.value = true
  thinkingDone.value = true
  resetDebug()
}

async function send() {
  const q = input.value.trim()
  if (!q || streaming.value) return
  input.value = ''
  await play('normal', q)
}
</script>

<template>
  <div class="page">
    <header class="header">
      <h1>数学助手 · 流式公式演示（开发专用入口 #demo）</h1>
      <p class="hint">
        假数据流式输出，不调用任何模型 API。观测器 v2 追踪每个公式容器及其内部 KaTeX
        排版节点的创建、删除与替换：正常播放应<b>无任何异常</b>；两个"对照"按钮分别故意
        逐帧替换排版节点、删除唯一的已完成公式，用于验证观测器确实能检出问题；
        清空与重播只由"新对话"/播放按钮显式触发，不应误报。
      </p>
    </header>

    <div class="controls">
      <button :disabled="streaming" @click="play('thinking')">▶ 播放（思考 + 回答）</button>
      <button :disabled="streaming" @click="play('normal')">▶ 播放（仅回答）</button>
      <button :disabled="streaming" @click="play('bad')">▶ 播放（坏公式降级）</button>
      <button :disabled="streaming" @click="play('price')">▶ 播放（纯文本美元）</button>
      <button :disabled="streaming" class="warn" @click="play('unstable')">▶ 对照（故意替换排版节点）</button>
      <button :disabled="streaming" class="warn" @click="play('drop')">▶ 对照（故意删除已完成公式）</button>
      <button :disabled="!streaming" class="ghost" @click="stop">⏹ 停止</button>
      <button class="ghost" @click="reset">✚ 新对话</button>
    </div>

    <main ref="chatRoot" class="chat">
      <template v-if="userMsg">
        <div class="msg user">
          <div class="bubble">{{ userMsg }}</div>
        </div>
        <div class="msg assistant">
          <details v-if="thinkingText" class="thinking" :open="showThinking || !thinkingDone">
            <summary @click.prevent="showThinking = !showThinking">
              思考过程 {{ thinkingDone ? '' : '（进行中）' }}
            </summary>
            <StreamMessage scope="thinking" :text="thinkingText" :done="thinkingDone" />
          </details>
          <div class="bubble answer">
            <StreamMessage
              scope="answer"
              :text="answerText"
              :done="answerDone"
              :fault="currentScenario === 'unstable' ? 'replace' : currentScenario === 'drop' ? 'drop' : undefined"
            />
          </div>
        </div>
      </template>
      <div v-else class="placeholder">点击上方按钮或发送一条消息开始（原型使用假数据）。</div>
    </main>

    <details class="settings">
      <summary>设置（原型占位，第 3 步接入）</summary>
      <div class="settings-body">
        <label>学段 <select disabled><option>初中</option></select></label>
        <label>模式 <select disabled><option>解答模式</option></select></label>
        <label>服务商 <select disabled><option>DeepSeek</option></select></label>
        <label>模型 <select disabled><option>deepseek-chat</option></select></label>
        <label>回答风格 <select disabled><option>完整步骤</option></select></label>
        <label>深度思考 <input type="checkbox" disabled /></label>
      </div>
    </details>

    <footer class="input-bar">
      <input
        v-model="input"
        :disabled="streaming"
        placeholder="输入数学问题（原型：发送后播放示例回答）"
        @keydown.enter="send"
      />
      <button class="send" :disabled="streaming || !input.trim()" @click="send">发送</button>
    </footer>

    <section class="debug">
      <h2>公式稳定性观测 v2</h2>
      <div class="debug-grid">
        <span>分片次数：{{ chunkCount }}</span>
        <span>公式容器：{{ stats.containers }}</span>
        <span>容器创建总次数：{{ stats.creations }}</span>
        <span>容器被移除：{{ stats.removals }}</span>
        <span>内部排版节点替换：{{ stats.internalReplacements }}</span>
        <span :class="stats.anomalies.length ? 'bad' : 'ok'">
          {{ stats.anomalies.length ? `检测到 ${stats.anomalies.length} 项异常！` : '无异常 ✓' }}
        </span>
      </div>
      <ul v-if="stats.anomalies.length" class="anomalies">
        <li v-for="(a, i) in stats.anomalies.slice(0, 8)" :key="i">
          [{{ a.type }}] {{ a.key }} @分片{{ a.at }} — {{ a.detail }}
        </li>
        <li v-if="stats.anomalies.length > 8">…共 {{ stats.anomalies.length }} 项</li>
      </ul>
    </section>
  </div>
</template>

<style scoped>
.page {
  max-width: 780px;
  margin: 0 auto;
  padding: 16px 14px 90px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.header h1 {
  font-size: 1.15rem;
  margin: 0 0 6px;
}
.hint {
  margin: 0;
  font-size: 0.85rem;
  color: #57606a;
}
.controls {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.controls button {
  border: 1px solid #d0d7de;
  background: #fff;
  border-radius: 8px;
  padding: 6px 12px;
  cursor: pointer;
  font-size: 0.85rem;
}
.controls button:disabled {
  opacity: 0.5;
  cursor: default;
}
.controls .ghost {
  background: transparent;
}
.controls .warn {
  border-color: #f0b72f;
  color: #9a6700;
}
.chat {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: 120px;
}
.msg.user {
  display: flex;
  justify-content: flex-end;
}
.msg.user .bubble {
  background: #1a73e8;
  color: #fff;
  border-radius: 14px 14px 4px 14px;
  padding: 8px 12px;
  max-width: 80%;
}
.msg.assistant .bubble {
  background: #fff;
  border: 1px solid #e1e4e8;
  border-radius: 4px 14px 14px 14px;
  padding: 10px 14px;
}
.thinking {
  border: 1px dashed #d0d7de;
  border-radius: 10px;
  padding: 6px 12px;
  margin-bottom: 8px;
  background: #fafbfc;
  font-size: 0.92em;
  color: #57606a;
}
.thinking summary {
  cursor: pointer;
  user-select: none;
}
.placeholder {
  color: #8b949e;
  text-align: center;
  padding: 30px 0;
}
.settings {
  border: 1px solid #e1e4e8;
  border-radius: 10px;
  background: #fff;
  padding: 6px 12px;
  font-size: 0.88rem;
}
.settings-body {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  padding: 8px 0;
}
.settings-body label {
  display: flex;
  align-items: center;
  gap: 4px;
  color: #57606a;
}
.input-bar {
  position: fixed;
  left: 0;
  right: 0;
  bottom: 0;
  background: #f5f6f8;
  padding: 10px 14px;
  display: flex;
  gap: 8px;
  justify-content: center;
}
.input-bar input {
  width: min(680px, 100%);
  border: 1px solid #d0d7de;
  border-radius: 10px;
  padding: 9px 12px;
  font-size: 0.95rem;
}
.input-bar .send {
  border: none;
  background: #1a73e8;
  color: #fff;
  border-radius: 10px;
  padding: 0 18px;
  font-size: 0.95rem;
  cursor: pointer;
  white-space: nowrap;
}
.input-bar .send:disabled {
  opacity: 0.5;
}
.debug {
  border: 1px solid #e1e4e8;
  border-radius: 10px;
  background: #fff;
  padding: 8px 12px;
  font-size: 0.85rem;
}
.debug h2 {
  font-size: 0.9rem;
  margin: 2px 0 6px;
}
.debug-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
}
.debug .ok {
  color: #1a7f37;
  font-weight: 600;
}
.debug .bad {
  color: #cf222e;
  font-weight: 600;
}
.anomalies {
  margin: 6px 0 0;
  color: #cf222e;
  word-break: break-all;
}
@media (max-width: 560px) {
  .controls button {
    padding: 8px 10px;
  }
  .msg.user .bubble,
  .msg.assistant .bubble {
    max-width: 94%;
  }
}
</style>
