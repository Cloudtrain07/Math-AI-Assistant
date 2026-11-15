<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import StreamMessage from './components/StreamMessage.vue'
import ImageEditorDialog from './components/ImageEditorDialog.vue'
import AdminPanel from './components/AdminPanel.vue'
import {
  TurnHttpError,
  attachGeneration,
  stopGeneration,
  clearConversation,
  continueChat,
  fetchHistory,
  fetchMe,
  fetchSettings,
  login as apiLogin,
  logout as apiLogout,
  removeImage as apiRemoveImage,
  streamChat,
  uploadImage,
  type SettingsInfo,
  type TurnSettings,
  type UserInfo,
} from './api'
import { blockSendWhileHistoryBusy, HISTORY_LOAD_ERROR_TEXT } from './historyGate'
import {
  isOverSize,
  isSupportedImageFile,
  type EditParams,
  type EditorResult,
  type EditorSource,
} from './imageEdit'
import { shouldSendOnEnter } from './ime'
import { classifyDone, composeDaily, composeNotice } from './notice'
import { collapsedScrollAdjustment } from './collapseAnchor'
import { isFollowingBottom, shouldShowJumpToBottom } from './scrollFollow'
import { OUTPUT_TOKENS_BOUNDS, WAIT_SECONDS_BOUNDS, parseWebLimit } from './webLimits'
import { modelThinking, providerView, syncEffort } from './settings'
import { copyText } from './clipboard'
import { formatAvgSpeed } from './speed'
import { browserStorage, selectedProvider, saveProvider } from './providerPreference'

interface ChatEntry {
  role: 'user' | 'assistant'
  content: string
  reasoning: string
  done: boolean
  /** 本会话内随消息发送的图片（仅当前页面内存，恢复历史没有此项） */
  imageUrl?: string
  copied?: boolean
  speedSummary?: string
}

/** 待发送图片：始终保留原始文件，重新编辑从原图+参数恢复 */
interface PendingImage {
  source: EditorSource
  editedBlob: Blob
  editedUrl: string
  editedWidth: number
  editedHeight: number
  params: EditParams
  /** 已随上一条消息发送（服务端仍保留，供同题追问重新附带） */
  sent: boolean
}

type StatusKind = 'info' | 'success' | 'warn' | 'error' | 'stopped'

const me = ref<UserInfo | null>(null)
const authChecked = ref(false)
const loginName = ref('')
const loginPassword = ref('')
const loginError = ref('')
const loggingIn = ref(false)

const messages = ref<ChatEntry[]>([])
const input = ref('')
const sending = ref(false)
const statusText = ref('')
const statusKind = ref<StatusKind>('info')
const usageText = ref('')
const elapsedSeconds = ref(0)
const genSeconds = ref(0)
const liveTokens = ref<number | null>(null)
const activeAssistant = ref<ChatEntry | null>(null)
let firstDeltaAt: number | null = null
const restoreNotice = ref('')
/** 历史恢复状态门禁：加载中/读取失败时锁定发送，防止界面与模型上下文分叉 */
const historyLoading = ref(false)
const historyError = ref('')
/** 恢复结果的代次：清空/登出后递增，过期恢复结果直接丢弃（不覆盖最新状态） */
let restoreEpoch = 0
const historyBusy = computed(() =>
  blockSendWhileHistoryBusy(historyLoading.value, historyError.value),
)
const mode = ref('互动模式')
const clearArmed = ref(false)
let clearTimer: ReturnType<typeof setTimeout> | null = null

/** 状态提示统一入口：文字 + 种类（图标与颜色由种类决定，不只靠颜色区分） */
function setStatus(text: string, kind: StatusKind = 'info') {
  statusText.value = text
  statusKind.value = kind
}

// ---- 待发送图片：选择/粘贴 → 编辑确认 → 缩略图 → 点击发送才上传 ----
const pendingImage = ref<PendingImage | null>(null)
let imageRestoreSerial = 0
const editorOpen = ref(false)
const editorSource = ref<EditorSource | null>(null)
const editorParams = ref<EditParams | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)
const composerInput = ref<HTMLTextAreaElement | null>(null)

/** 多行输入自动增高：按内容高度撑开，超出 CSS max-height 时局部滚动，不撑破页面 */
function autosizeComposer() {
  const el = composerInput.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = `${el.scrollHeight}px`
}

// 内容变化（含发送后清空、粘贴、换行）后重新计算高度
watch(input, () => void nextTick(autosizeComposer))

function discardPendingImage() {
  imageRestoreSerial++
  if (pendingImage.value) {
    URL.revokeObjectURL(pendingImage.value.editedUrl)
    pendingImage.value = null
  }
}

/**
 * 已发送消息的缩略图地址：为每条消息创建独立 blob URL。
 * 与待发送预览的 editedUrl 不共享，预览的重新编辑/移除不会让历史缩略图失效；
 * 消息 URL 由消息持有，在清空、退出、恢复历史和组件卸载时统一释放。
 */
function createSentImageUrl(): string | undefined {
  const blob = pendingImage.value?.editedBlob
  return blob ? URL.createObjectURL(blob) : undefined
}

/** 释放所有已发送消息持有的图片地址（messages 即将被整体替换或组件销毁时） */
function releaseMessageImages() {
  for (const m of messages.value) {
    if (m.imageUrl?.startsWith('blob:')) URL.revokeObjectURL(m.imageUrl)
  }
}

function previewUrl(id: string): string {
  return `/api/history?preview_id=${encodeURIComponent(id)}`
}

async function restoreActiveImage(id: string, epoch: number) {
  const serial = imageRestoreSerial
  try {
    const response = await fetch(previewUrl(id), { cache: 'no-store' })
    if (!response.ok) return
    const blob = await response.blob()
    const bitmap = await decodeImage(blob)
    const width = bitmap.width, height = bitmap.height
    bitmap.close()
    if (epoch !== restoreEpoch || serial !== imageRestoreSerial || pendingImage.value) return
    pendingImage.value = {
      source: { blob, name: 'restored-image' }, editedBlob: blob,
      editedUrl: URL.createObjectURL(blob), editedWidth: width, editedHeight: height,
      params: { rotate: 0, crop: null }, sent: true,
    }
  } catch { /* A missing preview must not prevent history restoration. */ }
}

function openFilePicker() {
  fileInput.value?.click()
}

async function onFileChange(event: Event) {
  const picker = event.target as HTMLInputElement
  const file = picker.files?.[0]
  picker.value = ''
  if (file) await acceptNewImage(file)
}

function onPaste(event: ClipboardEvent) {
  if (!me.value || sending.value || editorOpen.value) return
  const item = Array.from(event.clipboardData?.items ?? []).find((i) =>
    i.type.startsWith('image/'),
  )
  if (!item) return
  const file = item.getAsFile()
  if (!file) return
  event.preventDefault()
  void acceptNewImage(file)
}

/** 校验并在浏览器中预解码，再进入编辑弹窗；失败给出明确提示 */
async function acceptNewImage(file: File) {
  if (sending.value) return
  if (!isSupportedImageFile(file)) {
    setStatus('不支持的图片格式，仅支持 jpg / png / webp / bmp / gif。', 'error')
    return
  }
  if (isOverSize(file)) {
    setStatus('图片超过 10MB，请压缩后再试。', 'error')
    return
  }
  setStatus('')
  try {
    await decodeImage(file)
  } catch {
    setStatus('浏览器无法解码该图片（如 HEIC 格式），请先转换为 jpg 或 png。', 'error')
    return
  }
  // 新图从零开始编辑；重新编辑则由 reEdit 带入原参数
  editorSource.value = { blob: file, name: file.name }
  editorParams.value = null
  editorOpen.value = true
}

function decodeImage(blob: Blob): Promise<ImageBitmap> {
  return createImageBitmap(blob)
}

function onEditorConfirm(result: EditorResult) {
  editorOpen.value = false
  const source = editorSource.value
  if (!source) return
  discardPendingImage()
  pendingImage.value = {
    source,
    editedBlob: result.blob,
    editedUrl: URL.createObjectURL(result.blob),
    editedWidth: result.width,
    editedHeight: result.height,
    params: result.params,
    sent: false,
  }
  if (result.note) setStatus(result.note, 'info')
}

function reEdit() {
  const pending = pendingImage.value
  if (!pending || sending.value) return
  editorSource.value = pending.source
  editorParams.value = pending.params
  editorOpen.value = true
}

async function removePending() {
  if (sending.value) return
  discardPendingImage()
  // 同时清掉服务端可能保留的上一条附图，避免后续轮次重新附带
  const result = await apiRemoveImage()
  if (result && !result.ok && result.message) setStatus(result.message, 'error')
}

// ---- 界面设置（来自 /api/settings，均可在折叠区修改） ----
const settings = ref<SettingsInfo | null>(null)
const level = ref('高中')
const providerId = ref<string | null>(null)
const model = ref('')
const thinking = ref(false)
/** 当前模型的思考强度；null 表示该模型不提供强度选择（不随请求发送） */
const effort = ref<string | null>('high')
const answerStyle = ref('完整步骤+答案')
/** 网页覆盖的输出上限：v-model 对 type=number 输入会把填写值转成 number、
 * 留空仍是空字符串，因此声明为 string | number；发送前统一解析校验 */
const maxOutputTokensInput = ref<string | number>('')
const maxWaitSecondsInput = ref<string | number>('')

const currentProvider = computed(
  () => settings.value?.providers.find((p) => p.id === providerId.value) ?? null,
)
// 深度思考开关只在服务商支持时显示（与旧界面一致）；
// 思考强度按当前模型的档位元数据显隐（Qwen3.7、MiMo 无强度选择）
const supportsThinking = computed(() => currentProvider.value?.supports_thinking ?? false)
const modelCapability = computed(() => modelThinking(currentProvider.value, model.value))
const effortLevels = computed(() => modelCapability.value.effort_levels)
// 解答风格只在解答模式下显示（与旧界面一致）
const showAnswerStyle = computed(() => mode.value === '解答模式')
const modes = computed(() => settings.value?.modes ?? ['互动模式', '解答模式'])

// 切换服务商：模型列表与选中值同步为新服务商配置，避免残留无效模型
watch(providerId, () => {
  const providers = settings.value?.providers ?? []
  model.value = providerView(providers, providerId.value).model
  if (me.value && providerId.value && providers.some(p => p.id === providerId.value)) {
    saveProvider(browserStorage(), me.value.username, providerId.value)
  }
})

// 切换服务商或模型后同步思考强度：合法值保留，非法值换成新模型的默认档位，
// 无档位的模型清空（不发送强度参数），不残留上一模型的档位
watch([providerId, model], () => {
  effort.value = syncEffort(modelCapability.value, effort.value)
})

// .page 是唯一滚动容器：页头与输入卡为玻璃悬浮层，消息从其下方穿过
const chatScroll = ref<HTMLElement | null>(null)
/** 吸附用的页头实测高度：写入 CSS 变量供 .thinking[open] summary 的 sticky top 使用 */
const headerEl = ref<HTMLElement | null>(null)
let headerResize: ResizeObserver | null = null

/** 把页头当前高度同步到 .page 的 --thinking-top（换行、旋转、键盘压缩都会变） */
function syncStickyTop() {
  const header = headerEl.value
  const pageEl = chatScroll.value
  if (!header || !pageEl) return
  pageEl.style.setProperty('--thinking-top', `${header.offsetHeight}px`)
}

/** 页头在登录后才渲染：登录态变化后挂接观察器，卸载/登出时断开 */
function attachHeaderObserver() {
  headerResize?.disconnect()
  headerResize = null
  syncStickyTop()
  const header = headerEl.value
  if (header && typeof ResizeObserver !== 'undefined') {
    headerResize = new ResizeObserver(syncStickyTop)
    headerResize.observe(header)
  }
}

watch([authChecked, me], () => void nextTick(attachHeaderObserver), { immediate: true })

let stickToBottom = true
/** 用户明显离开底部时的「回到底部」入口（流式输出不会把阅读位置拉回去） */
const showJump = ref(false)

let controller: AbortController | null = null
let generationId: string | null = null
let stopRequested = false
let reconnectRequested = false
function resumeVisible() {
  if (!document.hidden && generationId && controller) {
    reconnectRequested = true
    controller.abort()
  }
}
let elapsedTimer: ReturnType<typeof setInterval> | null = null

onMounted(async () => {
  window.addEventListener('paste', onPaste)
  document.addEventListener('visibilitychange', resumeVisible)
  window.addEventListener('pageshow', resumeVisible)
  window.addEventListener('online', resumeVisible)
  await nextTick(autosizeComposer)
  me.value = await fetchMe()
  authChecked.value = true
  if (me.value) {
    await Promise.all([loadSettings(), restoreHistory()])
  }
})

onBeforeUnmount(() => {
  if (elapsedTimer !== null) clearInterval(elapsedTimer)
  window.removeEventListener('paste', onPaste)
  document.removeEventListener('visibilitychange', resumeVisible)
  window.removeEventListener('pageshow', resumeVisible)
  window.removeEventListener('online', resumeVisible)
  reconnectRequested = false
  controller?.abort()
  headerResize?.disconnect()
  headerResize = null
  releaseMessageImages()
})

async function loadSettings() {
  const username = me.value?.username
  const epoch = restoreEpoch
  const loaded = await fetchSettings()
  if (!username || username !== me.value?.username || epoch !== restoreEpoch) return
  settings.value = loaded
  const s = settings.value
  if (!s) return
  level.value = s.default_level
  mode.value = s.default_mode
  const selected = selectedProvider(browserStorage(), me.value?.username ?? '', s.providers, s.default_provider)
  providerId.value = selected
  model.value = providerView(s.providers, selected).model
  thinking.value = s.default_thinking
  effort.value = s.default_reasoning_effort
  answerStyle.value = s.default_answer_style
}

async function restoreHistory(resumeCurrent = true) {
  const epoch = restoreEpoch
  historyLoading.value = true
  historyError.value = ''
  try {
    releaseMessageImages()
    const data = await fetchHistory()
    // 恢复期间发生清空/登出（restoreEpoch 递增）时，过期结果不得覆盖最新状态
    if (epoch !== restoreEpoch) return
    messages.value = (resumeCurrent && data.generation ? data.history.slice(0, data.generation.history_length) : data.history).map((m) => ({
      role: m.role,
      content: m.content,
      reasoning: '',
      done: true,
    }))
    for (const preview of data.image_previews ?? []) {
      const entry = messages.value[preview.index]
      if (entry?.role === 'user') entry.imageUrl = previewUrl(preview.id)
    }
    if (data.active_preview_id && !pendingImage.value) {
      void restoreActiveImage(data.active_preview_id, epoch)
    }
    restoreNotice.value = data.notice
    if (data.notice) setStatus(data.notice, 'error')
    else if (data.image_preview_missing) setStatus('部分图片预览超过本次对话容量限制，刷新后无法显示。', 'warn')
    followLatest()
    await nextTick()
    scrollToBottom()
    if (data.generation && epoch === restoreEpoch) {
      if (resumeCurrent) {
        historyLoading.value = false
        void runTurn('resume', '', data.generation.id)
      } else {
        setStatus('已有另一条回答正在生成，请刷新页面查看进度。', 'warn')
      }
    }
  } catch {
    // 读取失败绝不能伪装成空的新对话：服务端历史仍在，此时发送会造成
    // 「界面像新对话、模型带着旧上下文」的分歧——锁定发送直到恢复或显式清空
    if (epoch !== restoreEpoch) return
    messages.value = []
    historyError.value = HISTORY_LOAD_ERROR_TEXT
    followLatest()
  } finally {
    if (epoch === restoreEpoch) historyLoading.value = false
  }
}

async function submitLogin() {
  if (loggingIn.value) return
  loginError.value = ''
  loggingIn.value = true
  try {
    me.value = await apiLogin(loginName.value.trim(), loginPassword.value)
    loginPassword.value = ''
    await Promise.all([loadSettings(), restoreHistory()])
  } catch (err) {
    loginError.value = err instanceof Error ? err.message : '登录失败'
  } finally {
    loggingIn.value = false
  }
}

async function doLogout() {
  reconnectRequested = false
  controller?.abort()
  await apiLogout()
  restoreEpoch++
  me.value = null
  releaseMessageImages()
  messages.value = []
  setStatus('')
  usageText.value = ''
  input.value = ''
  historyError.value = ''
  historyLoading.value = false
  discardPendingImage()
  // 会话整体没了：重置跟随与「回到底部」状态，重新登录后不带残留
  followLatest()
}

// ---- 滚动跟随：用户向上阅读时不强制拉回底部 ----

/**
 * 上一次由程序贴底后的 scrollTop。
 * 滚动事件要等到下一帧才派发，用户在这之前向上翻时，光靠事件里的标记会把他的阅读位置拉回去，
 * 所以贴底前先按当前位置核对一次。
 */
let pinnedTop = -1

/** 重新开始跟随最新内容（新回合、恢复历史、清空、退出、点「回到底部」） */
function followLatest() {
  stickToBottom = true
  pinnedTop = -1
  showJump.value = false
}

/** 只刷新按钮显隐：不改动跟随状态，也不移动滚动位置 */
function refreshJumpState() {
  const el = chatScroll.value
  if (el) showJump.value = shouldShowJumpToBottom(el)
}

function onScroll() {
  const el = chatScroll.value
  if (el) followReader(el)
}

async function scrollToBottom() {
  const el = chatScroll.value
  if (!el) return
  // 滚动事件要到下一帧才派发：位置已被用户改动时，先按实时几何量核对要不要继续跟随
  if (stickToBottom && pinnedTop >= 0 && el.scrollTop < pinnedTop - 4) followReader(el)
  if (stickToBottom) {
    el.scrollTop = el.scrollHeight
    pinnedTop = el.scrollTop
  }
  // 内容增高本身不派发滚动事件：布局完成后按实时几何量重算显隐（不把向上阅读的人拉回来）
  refreshJumpState()
}

/** 用户自己滚动了：按实时几何量决定要不要继续跟随 */
function followReader(el: HTMLElement) {
  stickToBottom = isFollowingBottom(el)
  if (stickToBottom) pinnedTop = -1
  refreshJumpState()
}

/** 回到底部：沿用同一个滚动容器，并恢复既有的自动跟随规则 */
function jumpToBottom() {
  followLatest()
  void scrollToBottom()
}

// ---- 思考区折叠：吸附标题在折叠时校正滚动，让同一条消息留在视野内 ----

/**
 * 思考区标题点击（原生 details/summary 交互保持不变）：
 * 仅折叠时先记录折叠前位置，等浏览器切换 open、新布局生效后重新测量并校正。
 * 校正赋值会触发 onScroll：不在底部时停止自动跟随（流式不把视线拉回底部），
 * 在底部时保持跟随——两种情况都符合「继续接收」的期望。
 */
function onThinkingSummaryClick(event: MouseEvent) {
  const summary = event.currentTarget as HTMLElement | null
  const details = summary?.closest('details')
  const pageEl = chatScroll.value
  if (!summary || !details || !pageEl || !details.open) return
  const containerTop = pageEl.getBoundingClientRect().top
  const topBefore = details.getBoundingClientRect().top - containerTop
  // 浏览器在 click 默认动作里切换 open：定时器回调时布局已折叠，读取几何量
  // 会强制同步排版。不用 rAF——部分内嵌/无头环境的帧回调不可靠，
  // setTimeout(0) 在两种环境下都确定执行
  setTimeout(() => {
    if (details.open) return // 期间又被展开：不校正
    // 吸附位与 CSS 的 sticky top 同口径：sticky 以滚动容器内容盒顶边为基准，
    // 用页头的实时位置（含页内边距）+ 高度 + 8px 间距，与实测吸附位一致
    const header = headerEl.value
    const headerTop = header
      ? header.getBoundingClientRect().top - pageEl.getBoundingClientRect().top
      : 60
    const stickyTop = headerTop + (header?.offsetHeight ?? 0) + 8
    const topAfter = details.getBoundingClientRect().top - containerTop
    const delta = collapsedScrollAdjustment({
      topBefore,
      topAfter,
      stickyTop,
      viewportHeight: pageEl.clientHeight,
      blockHeightAfter: details.offsetHeight,
      bottomReserve: 24,
    })
    if (delta !== 0) pageEl.scrollTop += delta
  }, 0)
}

// ---- 发送与流式接收：普通发送与继续生成共享设置、busy 与收尾处理 ----

function startElapsedTimer() {
  elapsedSeconds.value = 0
  genSeconds.value = 0
  liveTokens.value = null
  firstDeltaAt = null
  elapsedTimer = setInterval(() => {
    elapsedSeconds.value += 1
    if (firstDeltaAt !== null) {
      genSeconds.value = (performance.now() - firstDeltaAt) / 1000
    }
  }, 1000)
}

function stopElapsedTimer() {
  if (elapsedTimer !== null) clearInterval(elapsedTimer)
  elapsedTimer = null
}

function stopStreaming() {
  stopRequested = true
  if (generationId) {
    void stopGeneration(generationId).catch(() => setStatus('停止请求失败，请重试。', 'error'))
  } else {
    controller?.abort()
  }
}

async function onCopy(msg: ChatEntry, text: string) {
  if (!(await copyText(text))) {
    setStatus('复制失败，请手动选择文本复制。', 'warn')
    return
  }
  msg.copied = true
  setTimeout(() => {
    msg.copied = false
  }, 1500)
}

function liveSummary(msg: ChatEntry): string {
  const actual = liveTokens.value
  const count = actual ?? Math.floor((msg.content.length + msg.reasoning.length) / 2)
  const label = actual === null ? '约 ' : ''
  const speed = genSeconds.value >= 1 ? ` · 平均${label}${(count / genSeconds.value).toFixed(1)} tok/s` : ''
  return `已输出${label}${count} tokens${speed}${actual === null ? '（按已收到文字估算）' : ''}`
}

interface ResolvedWebLimits {
  maxOutputTokens: number | null
  maxWaitSeconds: number | null
}

/** 发送前解析两个覆盖输入：空白=沿用服务器配置；非法值返回中文错误 */
function resolveWebLimits(): ResolvedWebLimits | { error: string } {
  const output = parseWebLimit(maxOutputTokensInput.value, OUTPUT_TOKENS_BOUNDS)
  if (!output.ok) return { error: output.message }
  const wait = parseWebLimit(maxWaitSecondsInput.value, WAIT_SECONDS_BOUNDS)
  if (!wait.ok) return { error: wait.message }
  return { maxOutputTokens: output.value, maxWaitSeconds: wait.value }
}

function currentTurnSettings(limits: ResolvedWebLimits): TurnSettings {
  const levels = effortLevels.value
  return {
    mode: mode.value,
    level: level.value,
    thinking: thinking.value,
    provider_id: providerId.value,
    model: model.value,
    answer_style: answerStyle.value,
    // 仅当前模型提供档位且选中值合法时发送强度；否则发 null（后端按缺省处理）
    reasoning_effort:
      effort.value !== null && levels.includes(effort.value) ? effort.value : null,
    max_output_tokens: limits.maxOutputTokens,
    max_wait_seconds: limits.maxWaitSeconds,
  }
}

async function runTurn(kind: 'chat' | 'continue' | 'resume', text = '', resumeId = '') {
  // 历史恢复完成前（或读取失败未处理时）禁止发送：此时界面与服务端历史不一致
  if (sending.value || historyBusy.value) return
  const pendingUnsent = kind === 'chat' && pendingImage.value && !pendingImage.value.sent
  if (kind === 'chat' && !text && !pendingUnsent) return
  // 请求开始前验证覆盖值：无效时不进入发送流程、不清空输入草稿
  const limits = kind === 'resume' ? { maxOutputTokens: null, maxWaitSeconds: null } : resolveWebLimits()
  if ('error' in limits) {
    setStatus(limits.error, 'error')
    return
  }
  const originalText = text
  if (kind === 'chat') input.value = ''
  setStatus('')
  const assistant = reactive<ChatEntry>({
    role: 'assistant',
    content: '',
    reasoning: '',
    done: false,
  })
  // 气泡在 start 事件后才显示：被拒（含无可继续题目）时不显示任何气泡
  activeAssistant.value = assistant
  let assistantShown = false
  const showBubbles = (userContent: string, imageUrl?: string) => {
    messages.value.push({
      role: 'user',
      content: userContent,
      reasoning: '',
      done: true,
      imageUrl,
    })
    messages.value.push(assistant)
    assistantShown = true
    // 不在此处重新声明跟随：若用户在请求往返期间已向上阅读，首包到达时不应把他拉回底部
    void nextTick(scrollToBottom)
  }
  sending.value = true
  followLatest()
  startElapsedTimer()
  controller = new AbortController()
  generationId = null
  stopRequested = false
  reconnectRequested = false

  // 图片在点击发送后才上传：选择、编辑、预览阶段不发生任何网络传输；
  // 上传阶段可停止（中止后不发聊天请求），失败保留待发送图片供重试
  let imageId: string | null = null
  if (kind === 'chat' && pendingUnsent && pendingImage.value) {
    const uploadSignal = controller.signal
    try {
      const uploaded = await uploadImage(
        pendingImage.value.editedBlob,
        'edited.jpg',
        uploadSignal,
      )
      imageId = uploaded.image_id
    } catch (err) {
      input.value = originalText
      sending.value = false
      stopElapsedTimer()
      controller = null
      if (uploadSignal.aborted) {
        setStatus('已停止发送，未发起模型请求。图片已保留，可重试。', 'stopped')
      } else {
        setStatus(err instanceof Error ? err.message : '图片上传失败，请重试。', 'error')
      }
      return
    }
  }

  const requestSettings = currentTurnSettings(limits)
  generationId = resumeId || Array.from(crypto.getRandomValues(new Uint8Array(16)), n => n.toString(16).padStart(2, '0')).join('')
  const onDelta = (d: { answer: string; reasoning: string }) => {
    if (firstDeltaAt === null && (d.answer || d.reasoning)) {
      firstDeltaAt = performance.now()
    }
    assistant.content += d.answer
    assistant.reasoning += d.reasoning
    void nextTick(scrollToBottom)
  }

  let reconcileHistory = false
  let previewUnavailable = false
  try {
    const handlers = {
      signal: controller.signal,
      generationId,
      imageId,
      onStart: (payload: { message: string; preview_id?: string; preview_unavailable?: boolean }) => {
        if (!assistantShown) showBubbles(kind === 'chat' ? (text || payload.message) : payload.message, imageId ? createSentImageUrl() : (payload.preview_id ? previewUrl(payload.preview_id) : undefined))
        else { assistant.content = ''; assistant.reasoning = '' }
        if (kind === 'resume' && payload.preview_id && !pendingImage.value) void restoreActiveImage(payload.preview_id, restoreEpoch)
        if (payload.preview_unavailable) {
          previewUnavailable = true
          setStatus('图片预览未能保留，刷新后无法恢复这张图片。', 'warn')
        }
      },
      onDelta,
      onUsage: (tokens: number) => { liveTokens.value = tokens },
    }
    let result
    try {
      result = kind === 'chat' ? await streamChat(text, requestSettings, handlers)
        : kind === 'continue' ? await continueChat(requestSettings, handlers)
        : await attachGeneration(generationId, handlers)
    } catch (err) {
      if (err instanceof TurnHttpError) throw err
      if (controller.signal.aborted) {
        if (!reconnectRequested) throw err
        reconnectRequested = false
        controller = new AbortController()
        handlers.signal = controller.signal
      }
      // A lost response can follow an accepted start. Keep trying the same id.
      for (;;) {
        if (controller.signal.aborted) throw err
        try {
          if (stopRequested) await stopGeneration(generationId)
          result = await attachGeneration(generationId, handlers)
          break
        } catch (retryError) {
          if (controller.signal.aborted) {
            if (!reconnectRequested) throw retryError
            reconnectRequested = false
            controller = new AbortController()
            handlers.signal = controller.signal
          }
          if (retryError instanceof TurnHttpError) throw retryError
          setStatus('连接中断，正在尝试恢复回答…', 'warn')
          await new Promise(resolve => setTimeout(resolve, 2000))
        }
      }
    }
    if (result.kind === 'rejected') {
      // 被拒：移除占位的助手气泡（如有），保留提示文字；待发送图片保留供重试
      if (assistantShown && !stopRequested) {
        messages.value.splice(messages.value.indexOf(assistant), 1)
      } else if (assistantShown) {
        assistant.done = true
        if (assistant.content || assistant.reasoning) assistant.speedSummary = `未完成 · ${liveSummary(assistant)}`
      }
      setStatus(result.payload.message, stopRequested ? 'stopped' : 'info')
    } else {
      const d = result.payload
      assistant.content = d.answer
      assistant.reasoning = d.reasoning_text
      assistant.done = true
      assistant.speedSummary = (d.error ? '未完成 · ' : '') + (formatAvgSpeed(d) || (d.completion_tokens != null ? `输出${d.tok_s_estimated ? '约 ' : ' '}${d.completion_tokens} tokens` : ''))
      // done ≠ 全部成功：状态种类由结构化字段判定（模型错误/空回答/截断/保存失败/stale/用量失败）
      setStatus([composeNotice(d), previewUnavailable ? '图片预览未能保留，刷新后无法恢复这张图片。' : ''].filter(Boolean).join('\n'), previewUnavailable && classifyDone(d) === 'success' ? 'warn' : classifyDone(d))
      usageText.value = [d.cumulative_usage, composeDaily(d)]
        .filter(Boolean)
        .join('\n')
      // 普通发送后按核心的图片状态同步缩略图：保留（标记已发送）或被消费/丢弃
      if (kind === 'chat' || kind === 'resume') {
        if (!d.image_kept) discardPendingImage()
        else if (pendingImage.value) pendingImage.value.sent = true
      }
      await nextTick(scrollToBottom)
    }
  } catch (err) {
    if (err instanceof TurnHttpError && err.status === 404) {
      reconcileHistory = true
    }
    if (assistant.content || assistant.reasoning) assistant.speedSummary = `未完成 · ${liveSummary(assistant)}`
    assistant.done = true
    if (controller?.signal.aborted) {
      setStatus('已停止接收。未完成的回答不会写入历史。', 'stopped')
    } else if (err instanceof Error) {
      setStatus(err.message, 'error')
    } else {
      setStatus('连接中断，请稍后重试。', 'error')
    }
  } finally {
    stopElapsedTimer()
    sending.value = false
    activeAssistant.value = null
    controller = null
    generationId = null
    // 收尾时状态行/用量行会改变内容高度：布局完成后再核对一次按钮显隐
    void nextTick(refreshJumpState)
  }
  if (reconcileHistory && me.value) {
    await restoreHistory(false)
  }
}

function send() {
  const hasPending = pendingImage.value && !pendingImage.value.sent
  if (!input.value.trim() && !hasPending) return
  runTurn('chat', input.value.trim())
}

/** Enter 发送；Shift+Enter 换行；中文输入法组合输入（选词/确认候选）不触发发送 */
function onComposerKey(event: KeyboardEvent) {
  if (!shouldSendOnEnter(event)) return
  // 阻止 Enter 在 textarea 中插入换行后再发送
  event.preventDefault()
  send()
}

function continueTurn() {
  runTurn('continue')
}

// ---- 新对话（二次确认，与现有界面一致） ----

async function onClearClick() {
  if (sending.value) return
  if (!clearArmed.value) {
    clearArmed.value = true
    clearTimer = setTimeout(() => {
      clearArmed.value = false
    }, 5000)
    return
  }
  if (clearTimer !== null) clearTimeout(clearTimer)
  clearArmed.value = false
  // 显式清空是唯一允许覆盖恢复结果的操作：递增代次丢弃在途恢复，
  // 并解除读取失败门禁（服务端历史已清空，界面与上下文重新一致）
  restoreEpoch++
  const result = await clearConversation()
  if (result.ok) {
    releaseMessageImages()
    messages.value = []
    setStatus(result.message, 'success')
    usageText.value = ''
    restoreNotice.value = ''
    historyError.value = ''
    historyLoading.value = false
    discardPendingImage()
    // 内容清空后没有「上方」可读：按钮状态与跟随一起归零
    followLatest()
  } else {
    setStatus(result.message, 'error')
  }
}
</script>

<template>
  <div ref="chatScroll" class="page" @scroll="onScroll">
    <template v-if="authChecked && !me">
      <form class="login" @submit.prevent="submitLogin">
        <span class="mark large" aria-hidden="true">∑</span>
        <h1>数学学习 AI 助手</h1>
        <p class="hint">请输入访问用户名与密码</p>
        <input
          v-model="loginName"
          autocomplete="username"
          placeholder="用户名"
          aria-label="用户名"
        />
        <input
          v-model="loginPassword"
          type="password"
          autocomplete="current-password"
          placeholder="密码"
          aria-label="密码"
        />
        <button type="submit" :disabled="loggingIn || !loginName.trim() || !loginPassword">
          {{ loggingIn ? '登录中…' : '登录' }}
        </button>
        <p v-if="loginError" class="error">{{ loginError }}</p>
        <p class="foot">仅限授权用户访问 · 登录即记录用量</p>
      </form>
    </template>

    <template v-else-if="me">
      <header ref="headerEl" class="header">
        <div class="brand">
          <span class="mark" aria-hidden="true">∑</span>
          <h1>数学学习 AI 助手</h1>
        </div>
        <div class="user-bar">
          <span class="user-chip">
            <svg viewBox="0 0 20 20" aria-hidden="true">
              <circle cx="10" cy="7" r="3" />
              <path d="M4 16.5c1.2-2.8 3.4-4 6-4s4.8 1.2 6 4" />
            </svg>
            <span class="chip-name">{{ me.display_name }}</span>
          </span>
          <span class="divider" aria-hidden="true"></span>
          <select v-model="mode" :disabled="sending" class="mode-select" aria-label="模式">
            <option v-for="m in modes" :key="m" :value="m">{{ m }}</option>
          </select>
          <div class="user-actions">
            <button class="btn ghost" :class="{ armed: clearArmed }" :disabled="sending || historyLoading" @click="onClearClick">
              <svg viewBox="0 0 20 20" aria-hidden="true">
                <circle cx="10" cy="10" r="7.2" />
                <path d="M10 6.6v6.8M6.6 10h6.8" />
              </svg>
              {{ clearArmed ? '再点一次确认清空' : '新对话' }}
            </button>
            <!-- 手机端：继续生成移到页头与「新对话」并排（桌面端此按钮隐藏，用输入区的同名按钮） -->
            <button class="btn ghost continue-top" :disabled="sending || historyBusy" @click="continueTurn">
              <svg viewBox="0 0 20 20" aria-hidden="true">
                <path d="M7.5 5.5l7 4.5-7 4.5z" />
              </svg>
              继续生成
            </button>
            <button class="btn ghost" :disabled="sending" @click="doLogout">
              <svg viewBox="0 0 20 20" aria-hidden="true">
                <path d="M8 4H5.5A1.5 1.5 0 0 0 4 5.5v9A1.5 1.5 0 0 0 5.5 16H8M12.5 6.8 15.7 10l-3.2 3.2M15.5 10H7.8" />
              </svg>
              退出
            </button>
          </div>
        </div>
      </header>

      <p v-if="restoreNotice" class="notice">
        <svg viewBox="0 0 20 20" aria-hidden="true">
          <circle cx="10" cy="10" r="7.2" />
          <path d="M10 9v4.4M10 6.4v.2" />
        </svg>
        {{ restoreNotice }}
      </p>

      <details class="panel">
        <summary>
          <svg viewBox="0 0 20 20" aria-hidden="true">
            <circle cx="10" cy="10" r="2.7" />
            <path
              d="M10 3v2M10 15v2M3 10h2M15 10h2M5 5l1.4 1.4M13.6 13.6 15 15M15 5l-1.4 1.4M6.4 13.6 5 15"
            />
          </svg>
          设置
          <span class="summary-hint">服务商 · 模型 · 学段 · 思考</span>
        </summary>
        <div class="panel-grid">
          <label>
            服务商
            <select v-model="providerId" :disabled="sending || !settings">
              <option v-for="p in settings?.providers ?? []" :key="p.id" :value="p.id">
                {{ p.name }}
              </option>
            </select>
          </label>
          <label>
            模型
            <select v-model="model" :disabled="sending || !currentProvider">
              <option v-for="m in currentProvider?.models ?? []" :key="m" :value="m">
                {{ m }}
              </option>
            </select>
          </label>
          <label>
            学段
            <select v-model="level" :disabled="sending">
              <option v-for="l in settings?.levels ?? ['小学', '初中', '高中', '大学']" :key="l" :value="l">
                {{ l }}
              </option>
            </select>
          </label>
          <template v-if="supportsThinking">
            <label class="checkbox">
              <input v-model="thinking" type="checkbox" :disabled="sending" />
              深度思考模式（更严谨，稍慢）
            </label>
            <label v-if="effortLevels.length > 0">
              思考强度
              <select v-model="effort" :disabled="sending">
                <option v-for="l in effortLevels" :key="l" :value="l">{{ l }}</option>
              </select>
            </label>
          </template>
          <label v-if="showAnswerStyle">
            解答风格
            <select v-model="answerStyle" :disabled="sending">
              <option v-for="s in settings?.answer_styles ?? []" :key="s" :value="s">
                {{ s }}
              </option>
            </select>
          </label>
          <label>
            最大输出 token
            <input
              v-model="maxOutputTokensInput"
              class="num-input"
              type="number"
              inputmode="numeric"
              :min="OUTPUT_TOKENS_BOUNDS.min"
              :max="OUTPUT_TOKENS_BOUNDS.max"
              step="1"
              :placeholder="`留空=${settings?.default_max_output_tokens ?? 30000}`"
              :disabled="sending"
            />
          </label>
          <label>
            最大等待时间（秒）
            <input
              v-model="maxWaitSecondsInput"
              class="num-input"
              type="number"
              inputmode="numeric"
              :min="WAIT_SECONDS_BOUNDS.min"
              :max="WAIT_SECONDS_BOUNDS.max"
              step="1"
              :placeholder="`留空=${settings?.default_max_wait_seconds ?? 600}`"
              :disabled="sending"
            />
          </label>
          <p class="panel-hint">
            以上两项留空沿用服务器配置，只作用于接下来的发送与继续生成，刷新后复位；试用更长输出可填 48000。
          </p>
        </div>
      </details>

      <!-- 管理区域：仅本机直连的登录用户挂载（local_admin 由后端判定），
           默认折叠、首次展开才请求统计，远程用户不加载管理数据 -->
      <AdminPanel v-if="me.local_admin" />

      <main class="chat">
        <div v-if="historyLoading" class="placeholder">
          <span class="mark" aria-hidden="true">∑</span>
          <p>正在加载对话…</p>
        </div>
        <div v-else-if="historyError" class="placeholder">
          <span class="mark" aria-hidden="true">∑</span>
          <p role="alert">{{ historyError }}</p>
          <button class="btn" type="button" :disabled="historyLoading" @click="restoreHistory()">
            重试加载对话
          </button>
        </div>
        <div v-else-if="messages.length === 0" class="placeholder">
          <span class="mark" aria-hidden="true">∑</span>
          <p>输入数学问题开始提问。</p>
        </div>
        <template v-for="(msg, i) in messages" :key="i">
          <div v-if="msg.role === 'user'" class="msg user">
            <button class="chip copy-chip" type="button" @click="onCopy(msg, msg.content)">
              <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M7 7V4h9v9h-3M4 7h9v9H4z" /></svg>
              {{ msg.copied ? '已复制' : '复制' }}
            </button>
            <div class="bubble">
              <img v-if="msg.imageUrl" :src="msg.imageUrl" class="sent-image" alt="发送的题目图片" />
              <span>{{ msg.content }}</span>
            </div>
          </div>
          <div v-else class="msg assistant">
            <details v-if="msg.reasoning" class="thinking" :class="{ streaming: !msg.done }" open>
              <summary @click="onThinkingSummaryClick">
                <svg viewBox="0 0 20 20" aria-hidden="true">
                  <path d="M10 3.2 12.2 8l5.1.5-3.8 3.4 1.1 5-4.6-2.7L5.4 16.9l1.1-5L2.7 8.5 7.8 8z" />
                </svg>
                思考过程{{ msg.done ? '' : '（进行中）' }}
                <span v-if="!msg.done" class="pulse" aria-hidden="true"></span>
              </summary>
              <StreamMessage scope="think" :text="msg.reasoning" :done="msg.done" />
            </details>
            <div class="bubble answer">
              <StreamMessage
                v-if="msg.content || msg.done"
                scope="ans"
                :text="msg.content"
                :done="msg.done"
              />
              <span v-else class="waiting">
                <span class="spinner" aria-hidden="true"></span>
                正在思考… 已用时 {{ elapsedSeconds }} 秒
              </span>
            </div>
            <div v-if="msg.speedSummary" class="gen-speed">{{ msg.speedSummary }}</div>
            <div v-if="msg.content" class="msg-actions">
              <button class="chip copy-chip" type="button" @click="onCopy(msg, msg.content)">
                <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M7 7V4h9v9h-3M4 7h9v9H4z" /></svg>
                {{ msg.copied ? '已复制' : '复制' }}
              </button>
            </div>
          </div>
        </template>
      </main>

      <section v-if="statusText" class="status" :class="statusKind">
        <svg v-if="statusKind === 'success'" viewBox="0 0 20 20" aria-hidden="true">
          <circle cx="10" cy="10" r="7.2" />
          <path d="M6.6 10.2l2.3 2.3 4.5-4.8" />
        </svg>
        <svg v-else-if="statusKind === 'error'" viewBox="0 0 20 20" aria-hidden="true">
          <circle cx="10" cy="10" r="7.2" />
          <path d="M10 6.4v4.6M10 13.4v.2" />
        </svg>
        <svg v-else-if="statusKind === 'warn'" viewBox="0 0 20 20" aria-hidden="true">
          <path d="M10 3.4l7.1 12.4H2.9z" />
          <path d="M10 8.4v3.4M10 13.8v.2" />
        </svg>
        <svg v-else-if="statusKind === 'stopped'" viewBox="0 0 20 20" aria-hidden="true">
          <rect x="4.5" y="4.5" width="11" height="11" rx="2" />
        </svg>
        <svg v-else viewBox="0 0 20 20" aria-hidden="true">
          <circle cx="10" cy="10" r="7.2" />
          <path d="M10 9v4.4M10 6.4v.2" />
        </svg>
        {{ statusText }}
      </section>
      <section v-if="usageText" class="usage">{{ usageText }}</section>

      <!-- 待发送图片：确认后才出现在这里；点击发送时才上传 -->
      <div v-if="pendingImage" class="pending-image">
        <img class="thumb" :src="pendingImage.editedUrl" alt="待发送图片" />
        <div class="pending-meta">
          <span class="pending-size">
            {{ pendingImage.editedWidth }}×{{ pendingImage.editedHeight }}
          </span>
          <span v-if="pendingImage.sent" class="pending-badge">已发送</span>
        </div>
        <div class="pending-actions">
          <button class="chip" type="button" :disabled="sending" @click="reEdit">
            <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M12.8 3.6l3.6 3.6L7 16.6l-4 1 1-4z" /></svg>
            编辑
          </button>
          <button class="chip danger" type="button" :disabled="sending" @click="removePending">
            <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M5 5l10 10M15 5L5 15" /></svg>
            移除
          </button>
        </div>
      </div>

      <div class="composer-dock">
      <div v-if="sending && activeAssistant" class="live-stats" role="status">
        <template v-if="firstDeltaAt === null">等待模型响应 · 已用时 {{ elapsedSeconds }} 秒</template>
        <template v-else>{{ activeAssistant.content ? '正在回答' : '正在思考' }} · {{ liveSummary(activeAssistant) }}</template>
      </div>
      <footer class="composer">
        <input
          ref="fileInput"
          type="file"
          accept="image/jpeg,image/png,image/webp,image/bmp,image/gif"
          class="file-picker"
          @change="onFileChange"
        />
        <button
          class="icon-btn"
          type="button"
          title="添加图片（支持裁剪、旋转）"
          :disabled="sending"
          @click="openFilePicker"
        >
          <svg viewBox="0 0 20 20" aria-hidden="true">
            <rect x="2.5" y="3.5" width="15" height="13" rx="2" />
            <circle cx="7" cy="8" r="1.4" />
            <path d="M2.5 14l4.5-4 3 3 3.5-3.5 4 4" />
          </svg>
        </button>
        <textarea
          ref="composerInput"
          v-model="input"
          class="composer-input"
          rows="1"
          :disabled="sending"
          placeholder="输入数学问题，可附图片"
          aria-label="输入数学问题，可附图片"
          @keydown="onComposerKey"
          @input="autosizeComposer"
        ></textarea>
        <button
          v-if="!sending"
          class="btn ghost continue"
          :disabled="sending || historyBusy"
          @click="continueTurn"
        >
          <svg viewBox="0 0 20 20" aria-hidden="true">
            <path d="M7.5 5.5l7 4.5-7 4.5z" />
          </svg>
          继续生成
        </button>
        <button v-if="sending" class="btn danger-btn" @click="stopStreaming">
          <svg viewBox="0 0 20 20" aria-hidden="true">
            <rect x="5.5" y="5.5" width="9" height="9" rx="1.5" />
          </svg>
          停止
        </button>
        <button
          v-else
          class="btn primary send"
          :disabled="historyBusy || (!input.trim() && !(pendingImage && !pendingImage.sent))"
          @click="send"
        >
          <svg viewBox="0 0 20 20" aria-hidden="true">
            <path d="M3.2 10.2 17 3.6l-3 12.6-4.2-4z" />
            <path d="M9.8 12.2 17 3.6" />
          </svg>
          发送
        </button>
        <!-- 回到底部：只在明显离开底部时出现，浮在输入卡上方，不改用新的滚动容器 -->
        <button v-if="showJump" class="jump-bottom" type="button" @click="jumpToBottom">
          <svg viewBox="0 0 20 20" aria-hidden="true">
            <path d="M10 4.2v9.6M6.2 10.6 10 14.4l3.8-3.8" />
          </svg>
          回到底部
        </button>
      </footer>
      </div>

      <ImageEditorDialog
        :open="editorOpen"
        :source="editorSource"
        :initial-params="editorParams"
        @cancel="editorOpen = false"
        @confirm="onEditorConfirm"
      />
    </template>
  </div>
</template>

<style scoped>
/* ===== 页面框架：唯一滚动容器；单栏内容流，页头不加卡片（与设计预览一致） ===== */
.page {
  width: min(780px, 100%);
  margin: 0 auto;
  height: 100dvh;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  padding: 12px 14px 0;
  scrollbar-gutter: stable;
}

/* ===== 通用控件 ===== */
.btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: 1px solid var(--line);
  border-radius: var(--radius-ctl);
  font-size: 0.88rem;
  cursor: pointer;
  white-space: nowrap;
  transition: background 0.15s, border-color 0.15s, color 0.15s, box-shadow 0.15s;
}
.btn svg {
  width: 15px;
  height: 15px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
  flex-shrink: 0;
}
.btn.ghost {
  background: transparent;
  color: var(--ink-2);
  padding: 6px 12px;
}
.btn.ghost:hover:not(:disabled) {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--accent-weak);
}
.btn.primary {
  border: none;
  background: var(--accent);
  color: var(--accent-contrast);
  padding: 8px 18px;
  font-weight: 550;
}
.btn.primary:hover:not(:disabled) {
  background: var(--accent-deep);
}
.btn.primary:disabled {
  background: #2b3148;
  color: var(--ink-3);
  cursor: default;
}
.btn.danger-btn {
  border: none;
  background: var(--danger);
  color: var(--accent-contrast);
  padding: 8px 18px;
  font-weight: 550;
}
.btn.danger-btn:hover:not(:disabled) {
  background: #e78d81;
}
.btn.ghost:disabled,
.btn.ghost:disabled:hover {
  border-color: var(--line-soft);
  color: var(--ink-3);
  background: transparent;
  cursor: default;
}
.btn:disabled {
  cursor: default;
}
.btn.ghost.armed {
  /* 二次确认态用危险色：清空不可撤销，提示要区别于普通次级按钮 */
  border-color: var(--danger);
  color: var(--danger);
  background: var(--danger-bg);
  font-weight: 600;
}
.icon-btn {
  border: 1px solid var(--line);
  background: var(--surface);
  color: var(--ink-2);
  width: 40px;
  height: 40px;
  border-radius: var(--radius-ctl);
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  transition: border-color 0.15s, color 0.15s, background 0.15s;
}
.icon-btn svg {
  width: 19px;
  height: 19px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.icon-btn:hover:not(:disabled) {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--accent-weak);
}
.icon-btn:disabled,
.icon-btn:disabled:hover {
  border-color: var(--line-soft);
  color: var(--ink-3);
  background: var(--surface);
  cursor: default;
}
select {
  font: inherit;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 5px 8px;
  background: var(--surface);
  font-size: 0.88rem;
  color: var(--ink);
}
.file-picker {
  display: none;
}

/* ===== 品牌标志 ===== */
.mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border-radius: 9px;
  background: var(--accent);
  color: var(--accent-contrast);
  font-size: 1.05rem;
  font-family: Georgia, 'Times New Roman', serif;
  flex-shrink: 0;
}
.mark.large {
  width: 44px;
  height: 44px;
  border-radius: 12px;
  font-size: 1.5rem;
}

/* ===== 页头：与页面同色，仅一条细底线，不加卡片 ===== */
.header {
  position: sticky;
  top: 0;
  z-index: 20;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
  padding: 10px 2px;
  background: var(--bg);
  border-bottom: 1px solid var(--line-soft);
}
.brand {
  display: flex;
  align-items: center;
  gap: 10px;
}
.brand h1 {
  font-size: 1.08rem;
  margin: 0;
  font-weight: 650;
  letter-spacing: 0.01em;
}
.user-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 0.88rem;
  color: var(--ink-2);
}
.user-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 11px;
  border-radius: 999px;
  border: 1px solid var(--line);
  color: var(--ink-2);
  font-size: 0.84rem;
  background: transparent;
}
.user-chip svg {
  width: 14px;
  height: 14px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
  flex-shrink: 0;
}
/* 用户名过长时省略，不推着页头按钮换行或溢出 */
.chip-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* 页头操作按钮成组：桌面端与原样一致，手机端整组换到「新对话 / 继续生成」并排的一行 */
.user-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  min-width: 0;
}
.divider {
  width: 1px;
  height: 18px;
  background: var(--line);
}
.mode-select {
  border-radius: 999px;
  padding: 5px 10px;
  color: var(--ink);
}

/* ===== 恢复提示：弱化警示条 ===== */
.notice {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 12px 0 0;
  padding: 7px 12px;
  font-size: 0.84rem;
  color: var(--warn);
  background: var(--warn-bg);
  border: 1px solid var(--warn-line);
  border-radius: var(--radius-ctl);
}
.notice svg {
  width: 14px;
  height: 14px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
  flex-shrink: 0;
}

/* ===== 状态与用量：弱化紧凑文字行，图标区分种类（不只靠颜色） ===== */
.status {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  margin: 12px 0 0;
  padding: 0 2px;
  font-size: 0.82rem;
  color: var(--ink-2);
  white-space: pre-wrap;
  overflow-wrap: break-word;
}
.status svg {
  width: 13px;
  height: 13px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
  flex-shrink: 0;
  margin-top: 2px;
}
.status.success svg {
  stroke: var(--ok);
}
.status.warn svg {
  stroke: var(--warn);
}
.status.error {
  color: var(--danger);
}
.status.stopped svg {
  stroke: var(--ink-2);
}
.usage {
  margin: 6px 0 0;
  padding: 0 2px;
  font-size: 0.78rem;
  color: var(--ink-3);
  white-space: pre-wrap;
  font-variant-numeric: tabular-nums;
}

/* ===== 折叠面板（设置）：稍亮表面 + 细边框 ===== */
.panel {
  border: 1px solid var(--line);
  border-radius: var(--radius-card);
  margin-top: 12px;
  font-size: 0.88rem;
  background: var(--surface);
  box-shadow: var(--shadow-card);
}
.panel summary {
  cursor: pointer;
  user-select: none;
  padding: 9px 14px;
  color: var(--ink-2);
  display: flex;
  align-items: center;
  gap: 8px;
  list-style: none;
  border-radius: var(--radius-card);
}
.panel summary::-webkit-details-marker {
  display: none;
}
.panel summary::after {
  content: '';
  margin-left: auto;
  width: 8px;
  height: 8px;
  border-right: 1.6px solid var(--ink-3);
  border-bottom: 1.6px solid var(--ink-3);
  transform: rotate(45deg);
  transition: transform 0.15s;
}
.panel[open] summary::after {
  transform: rotate(225deg);
}
.panel summary svg {
  width: 15px;
  height: 15px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.panel summary:hover {
  color: var(--ink);
  background: var(--surface-2);
}
.summary-hint {
  color: var(--ink-3);
  font-size: 0.78rem;
}
.panel-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 18px;
  padding: 6px 14px 14px;
}
.panel-grid label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  color: var(--ink-2);
  font-size: 0.84rem;
}
.panel-grid label.checkbox {
  flex-direction: row;
  align-items: center;
  gap: 7px;
  color: var(--ink);
  padding-top: 18px;
}
.panel-grid input[type='checkbox'] {
  width: 16px;
  height: 16px;
  accent-color: var(--accent);
}
.panel-grid input[type='number'].num-input {
  width: 150px;
  font-variant-numeric: tabular-nums;
}
.panel-hint {
  flex-basis: 100%;
  margin: 2px 0 0;
  color: var(--ink-3);
  font-size: 0.78rem;
}

/* ===== 聊天区：回答直接呈现在页面背景上，无卡片 ===== */
.chat {
  display: flex;
  flex-direction: column;
  gap: 22px;
  padding: 20px 2px;
}
.placeholder {
  margin: 48px auto 0;
  text-align: center;
  color: var(--ink-3);
}
.placeholder .mark {
  opacity: 0.45;
  margin-bottom: 10px;
}
.placeholder p {
  margin: 0;
  font-size: 0.9rem;
}
.msg {
  display: flex;
}
.msg.user {
  justify-content: flex-end;
}
.msg.user .bubble {
  background: var(--user-bubble);
  color: var(--ink);
  border-radius: 16px 16px 4px 16px;
  padding: 9px 14px;
  max-width: 78%;
  white-space: pre-wrap;
  overflow-wrap: break-word;
  line-height: 1.65;
  font-size: 0.95rem;
}
.msg.assistant {
  flex-direction: column;
}
.msg.assistant .bubble.answer {
  max-width: 100%;
  font-size: 0.95rem;
  line-height: 1.8;
  color: var(--ink);
}
.msg.user .copy-chip {
  align-self: center;
  flex: none;
  margin-right: 6px;
}
.msg-actions {
  margin-top: 4px;
}
.gen-speed {
  margin-top: 2px;
  font-size: 0.78rem;
  color: var(--ink-3);
  font-variant-numeric: tabular-nums;
}
.sent-image {
  display: block;
  max-width: min(240px, 100%);
  max-height: 170px;
  border-radius: 10px;
  border: 1px solid var(--line);
  margin-bottom: 8px;
  background: #fff;
}

/* 思考区：左侧细线 + 弱化内容，不抢回答的视觉中心 */
.thinking {
  border: none;
  border-left: 2px solid var(--line);
  border-radius: 0;
  padding: 1px 0 1px 12px;
  margin-bottom: 10px;
  background: transparent;
  font-size: 0.9em;
  color: var(--ink-2);
  max-width: 100%;
}
.thinking[open] {
  padding-bottom: 2px;
}
.thinking.streaming {
  border-left-color: var(--accent);
}
.thinking summary {
  cursor: pointer;
  user-select: none;
  display: flex;
  align-items: center;
  gap: 6px;
  list-style: none;
  color: var(--ink-2);
  font-size: 0.82rem;
}
.thinking summary::after {
  content: '';
  margin-left: auto;
  width: 7px;
  height: 7px;
  border-right: 1.5px solid var(--ink-3);
  border-bottom: 1.5px solid var(--ink-3);
  transform: rotate(45deg);
  transition: transform 0.15s;
}
.thinking[open] summary::after {
  transform: rotate(225deg);
}
.thinking summary::-webkit-details-marker {
  display: none;
}
.thinking summary svg {
  width: 13px;
  height: 13px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.5;
  stroke-linejoin: round;
}
.thinking summary:hover {
  color: var(--ink);
}
/* 展开的思考区：标题吸附在页头下方，只跟随到本思考区末尾（sticky 的包含块
   就是 details 自身，到块底自然随区滚出）。top 用实测页头高度（App 脚本把
   offsetHeight 写入 .page 的 --thinking-top），+8px 与折叠校正的吸附位同口径；
   实底背景遮挡从下方滚过的正文，z-index 低于页头的 20 */
.thinking[open] > summary {
  position: sticky;
  top: calc(var(--thinking-top, 64px) + 8px);
  z-index: 15;
  background: var(--bg);
  padding: 6px 0;
}
.thinking :deep(.stream-body) {
  color: var(--ink-2);
  font-size: 0.92em;
}
.pulse {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--accent);
  margin-left: 2px;
  animation: pulse 1.2s ease-in-out infinite;
}
@keyframes pulse {
  0%,
  100% {
    opacity: 0.25;
  }
  50% {
    opacity: 1;
  }
}
.waiting {
  color: var(--ink-3);
  font-size: 0.9em;
  display: inline-flex;
  align-items: center;
  gap: 8px;
}
.spinner {
  width: 14px;
  height: 14px;
  border-radius: 50%;
  border: 2px solid var(--accent-line);
  border-top-color: var(--accent);
  animation: spin 0.8s linear infinite;
  flex-shrink: 0;
}
@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

/* ===== 待发送图片缩略图条 ===== */
.pending-image {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 12px 0 0;
  padding: 6px 8px;
  border: 1px solid var(--line);
  border-radius: var(--radius-card);
  background: var(--surface);
  box-shadow: var(--shadow-card);
}
.pending-image .thumb {
  width: 52px;
  height: 52px;
  object-fit: cover;
  border-radius: 8px;
  border: 1px solid var(--line-soft);
  background: #fff;
}
.pending-meta {
  display: flex;
  flex-direction: column;
  gap: 3px;
  flex: 1;
  min-width: 0;
}
.pending-size {
  font-size: 0.78rem;
  color: var(--ink-3);
  font-variant-numeric: tabular-nums;
}
.pending-badge {
  align-self: flex-start;
  font-size: 0.72rem;
  color: var(--accent);
  background: var(--accent-weak);
  border: 1px solid var(--accent-line);
  border-radius: 999px;
  padding: 1px 8px;
}
.pending-actions {
  display: flex;
  gap: 6px;
}
.chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  border: 1px solid var(--line);
  background: var(--surface);
  color: var(--ink-2);
  border-radius: 8px;
  padding: 4px 9px;
  font-size: 0.8rem;
  cursor: pointer;
}
.chip svg {
  width: 13px;
  height: 13px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.chip:hover:not(:disabled) {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--accent-weak);
}
.chip.danger:hover:not(:disabled) {
  border-color: var(--danger);
  color: var(--danger);
  background: var(--danger-bg);
}
.chip:disabled {
  border-color: var(--line-soft);
  color: var(--ink-3);
  cursor: default;
}

/* ===== 输入区：实心表面 + 细边框，专注但不厚重 ===== */
.composer-dock { position: sticky; bottom: 0; z-index: 20; }
.live-stats { padding: 7px 12px; background: var(--surface, #fff); color: var(--ink-2); font-size: 0.82rem; border-radius: 8px; }
.composer {
  position: relative;
  bottom: 0;
  z-index: 20;
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 8px 10px;
  margin: 18px 0 14px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius-card);
  box-shadow: var(--shadow-pop);
}
.composer-input {
  flex: 1;
  min-width: 0;
  border: none;
  background: transparent;
  padding: 8px 4px;
  font-family: inherit;
  font-size: 0.95rem;
  line-height: 1.5;
  color: var(--ink);
  /* 多行输入：自动增高到上限后局部滚动，不撑破页面/不遮挡内容 */
  resize: none;
  max-height: 160px;
  overflow-y: auto;
}
.composer-input::placeholder {
  color: var(--ink-3);
}
.composer-input:focus-visible {
  box-shadow: none;
}
.composer:focus-within {
  border-color: var(--accent);
  box-shadow: var(--focus-ring);
}
.send {
  padding: 0 18px;
  height: 38px;
  white-space: nowrap;
}
.send svg {
  width: 15px;
  height: 15px;
}
.continue {
  height: 38px;
  padding: 0 12px;
  white-space: nowrap;
  background: var(--surface);
}
/* 页头「继续生成」仅在手机端替代输入区里的同名按钮，桌面端不显示，避免重复可见 */
.continue-top {
  display: none;
}

/* 回到底部：跟随输入卡这一悬浮层，贴在它上方右侧，不覆盖输入区 */
.jump-bottom {
  position: absolute;
  right: 10px;
  bottom: calc(100% + 6px);
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 5px 11px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--surface-2);
  color: var(--ink-2);
  font-size: 0.78rem;
  cursor: pointer;
  box-shadow: var(--shadow-card);
}
.jump-bottom:hover {
  border-color: var(--accent);
  color: var(--accent);
}
.jump-bottom svg {
  width: 13px;
  height: 13px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.7;
  stroke-linecap: round;
  stroke-linejoin: round;
}

/* ===== 登录页：同配色同控件，简洁无装饰 ===== */
.login {
  margin: auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
  width: min(360px, 92%);
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 16px;
  box-shadow: var(--shadow-pop);
  padding: 34px 30px 26px;
  text-align: center;
}
.login .mark.large {
  align-self: center;
  margin-bottom: 2px;
}
.login h1 {
  font-size: 1.25rem;
  margin: 0;
  font-weight: 650;
}
.login .hint {
  margin: 0 0 10px;
  color: var(--ink-3);
  font-size: 0.86rem;
}
.login input {
  border: 1px solid var(--line);
  border-radius: var(--radius-ctl);
  padding: 10px 12px;
  font-size: 0.95rem;
  background: var(--surface);
  color: var(--ink);
  transition: border-color 0.15s, box-shadow 0.15s;
}
.login input:focus-visible {
  border-color: var(--accent);
}
.login button[type='submit'] {
  border: none;
  background: var(--accent);
  color: var(--accent-contrast);
  border-radius: var(--radius-ctl);
  padding: 11px 0;
  font-size: 0.95rem;
  font-weight: 550;
  letter-spacing: 0.3em;
  text-indent: 0.3em;
  cursor: pointer;
  margin-top: 6px;
}
.login button[type='submit']:hover:not(:disabled) {
  background: var(--accent-deep);
}
.login button:disabled {
  background: #2b3148;
  color: var(--ink-3);
  cursor: default;
}
.login .error {
  color: var(--danger);
  font-size: 0.88rem;
  margin: 0;
}
.login .foot {
  margin: 12px 0 0;
  font-size: 0.76rem;
  color: var(--ink-3);
}

/* ===== 手机视口 ===== */
@media (max-width: 560px) {
  .page {
    /* 顶部避开刘海、底部避开手势条：页头与输入卡都是 sticky，按容器内边距留位 */
    padding: calc(8px + env(safe-area-inset-top, 0px)) 8px
      calc(8px + env(safe-area-inset-bottom, 0px));
  }
  .header {
    padding: 8px 2px;
  }
  .brand h1 {
    font-size: 1rem;
  }
  .user-bar {
    width: 100%;
    justify-content: flex-start;
    /* 窄屏页头换行而非横向溢出 */
    flex-wrap: wrap;
    row-gap: 6px;
  }
  .user-chip {
    min-width: 0;
    max-width: 100%;
  }
  /* 操作按钮整组另起一行：「新对话」与「继续生成」必定并排，组内再按宽度换行 */
  .user-actions {
    width: 100%;
    gap: 10px;
  }
  /* 拇指可点区域：页头次级按钮与模式选择都抬高到 40px */
  .user-actions .btn,
  .mode-select {
    min-height: 40px;
  }
  .msg.user .bubble,
  .msg.assistant .bubble.answer {
    max-width: 94%;
  }
  .thinking {
    max-width: 96%;
  }
  .chat {
    /* 内容不足一屏时也把输入区顶到屏幕底部，避免下方留出大片空白 */
    flex: 1;
    gap: 16px;
    padding: 14px 2px;
  }
  /* 手机端显示页头「继续生成」，隐藏输入区里的同名按钮（避免重复） */
  .continue-top {
    display: inline-flex;
  }
  /* 输入区改为上下两行：第一行 textarea 独占宽度，第二行放图片上传、回到底部与发送/停止 */
  .composer {
    display: grid;
    grid-template-columns: auto 1fr auto;
    gap: 6px 8px;
    align-items: center;
    margin: 10px 0 8px;
  }
  .composer .composer-input {
    grid-column: 1 / -1;
    grid-row: 1;
    max-height: 120px;
  }
  .composer .icon-btn {
    grid-column: 1;
    grid-row: 2;
    width: 44px;
    height: 44px;
  }
  .composer .send,
  .composer .danger-btn {
    grid-column: 3;
    grid-row: 2;
    /* 普通长度即取自然宽度，靠右对齐，不铺满整行 */
    justify-self: end;
    height: 44px;
  }
  .composer .continue {
    display: none;
  }
  /* 手机端把「回到底部」放进第二行的空档：不压住正文，也不用另起一个悬浮层 */
  .composer .jump-bottom {
    position: static;
    grid-column: 2;
    grid-row: 2;
    justify-self: start;
    min-height: 34px;
  }
}

/* 软键盘占用半屏（布局视口随之变矮）：压缩悬浮层留白与输入增高上限，保住正文可读高度 */
@media (max-width: 560px) and (max-height: 560px) {
  .page {
    padding-top: calc(4px + env(safe-area-inset-top, 0px));
    padding-bottom: calc(4px + env(safe-area-inset-bottom, 0px));
  }
  .header {
    padding: 4px 2px;
  }
  .chat {
    padding: 8px 2px;
  }
  .composer {
    padding: 6px 8px;
    margin: 6px 0 4px;
  }
  .composer .composer-input {
    max-height: 88px;
  }
}
</style>
