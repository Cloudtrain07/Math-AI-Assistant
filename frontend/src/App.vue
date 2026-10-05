<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import StreamMessage from './components/StreamMessage.vue'
import ImageEditorDialog from './components/ImageEditorDialog.vue'
import AdminPanel from './components/AdminPanel.vue'
import {
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
import {
  isOverSize,
  isSupportedImageFile,
  type EditParams,
  type EditorResult,
  type EditorSource,
} from './imageEdit'
import { composeDaily, composeNotice } from './notice'
import { providerView } from './settings'

interface ChatEntry {
  role: 'user' | 'assistant'
  content: string
  reasoning: string
  done: boolean
  /** 本会话内随消息发送的图片（仅当前页面内存，恢复历史没有此项） */
  imageUrl?: string
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
const usageText = ref('')
const elapsedSeconds = ref(0)
const restoreNotice = ref('')
const mode = ref('互动模式')
const clearArmed = ref(false)
let clearTimer: ReturnType<typeof setTimeout> | null = null

// ---- 待发送图片：选择/粘贴 → 编辑确认 → 缩略图 → 点击发送才上传 ----
const pendingImage = ref<PendingImage | null>(null)
const editorOpen = ref(false)
const editorSource = ref<EditorSource | null>(null)
const editorParams = ref<EditParams | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)

function discardPendingImage() {
  if (pendingImage.value) {
    URL.revokeObjectURL(pendingImage.value.editedUrl)
    pendingImage.value = null
  }
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
    statusText.value = '⚠️ 不支持的图片格式，仅支持 jpg / png / webp / bmp / gif。'
    return
  }
  if (isOverSize(file)) {
    statusText.value = '⚠️ 图片超过 10MB，请压缩后再试。'
    return
  }
  statusText.value = ''
  try {
    await decodeImage(file)
  } catch {
    statusText.value =
      '⚠️ 浏览器无法解码该图片（如 HEIC 格式），请先转换为 jpg 或 png。'
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
  statusText.value = result.note ? `ℹ️ ${result.note}` : statusText.value
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
  if (result && !result.ok && result.message) statusText.value = result.message
}

// ---- 界面设置（来自 /api/settings，均可在折叠区修改） ----
const settings = ref<SettingsInfo | null>(null)
const level = ref('高中')
const providerId = ref<string | null>(null)
const model = ref('')
const thinking = ref(false)
const effort = ref('high')
const answerStyle = ref('完整步骤+答案')

const currentProvider = computed(
  () => settings.value?.providers.find((p) => p.id === providerId.value) ?? null,
)
// 深度思考开关与思考强度只在服务商支持时显示（与旧界面一致）
const supportsThinking = computed(() => currentProvider.value?.supports_thinking ?? false)
// 解答风格只在解答模式下显示（与旧界面一致）
const showAnswerStyle = computed(() => mode.value === '解答模式')
const modes = computed(() => settings.value?.modes ?? ['互动模式', '解答模式'])

// 切换服务商：模型列表与选中值同步为新服务商配置，避免残留无效模型
watch(providerId, () => {
  model.value = providerView(settings.value?.providers ?? [], providerId.value).model
})

const chatScroll = ref<HTMLElement | null>(null)
let stickToBottom = true

let controller: AbortController | null = null
let elapsedTimer: ReturnType<typeof setInterval> | null = null

onMounted(async () => {
  window.addEventListener('paste', onPaste)
  me.value = await fetchMe()
  authChecked.value = true
  if (me.value) {
    await Promise.all([loadSettings(), restoreHistory()])
  }
})

onBeforeUnmount(() => {
  if (elapsedTimer !== null) clearInterval(elapsedTimer)
  window.removeEventListener('paste', onPaste)
})

async function loadSettings() {
  settings.value = await fetchSettings()
  const s = settings.value
  if (!s) return
  level.value = s.default_level
  mode.value = s.default_mode
  providerId.value = s.default_provider
  model.value = providerView(s.providers, s.default_provider).model
  thinking.value = s.default_thinking
  effort.value = s.default_reasoning_effort
  answerStyle.value = s.default_answer_style
}

async function restoreHistory() {
  const data = await fetchHistory()
  messages.value = data.history.map((m) => ({
    role: m.role,
    content: m.content,
    reasoning: '',
    done: true,
  }))
  restoreNotice.value = data.notice
  stickToBottom = true
  await nextTick()
  scrollToBottom()
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
  stopStreaming()
  await apiLogout()
  me.value = null
  messages.value = []
  statusText.value = ''
  usageText.value = ''
  input.value = ''
  discardPendingImage()
}

// ---- 滚动跟随：用户向上阅读时不强制拉回底部 ----
function onScroll() {
  const el = chatScroll.value
  if (!el) return
  stickToBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80
}

async function scrollToBottom() {
  const el = chatScroll.value
  if (!el || !stickToBottom) return
  el.scrollTop = el.scrollHeight
}

// ---- 发送与流式接收：普通发送与继续生成共享设置、busy 与收尾处理 ----

function startElapsedTimer() {
  elapsedSeconds.value = 0
  elapsedTimer = setInterval(() => {
    elapsedSeconds.value += 1
  }, 1000)
}

function stopElapsedTimer() {
  if (elapsedTimer !== null) clearInterval(elapsedTimer)
  elapsedTimer = null
}

function stopStreaming() {
  if (controller !== null) controller.abort()
}

function currentTurnSettings(): TurnSettings {
  return {
    mode: mode.value,
    level: level.value,
    thinking: thinking.value,
    provider_id: providerId.value,
    model: model.value,
    answer_style: answerStyle.value,
    reasoning_effort: effort.value,
  }
}

async function runTurn(kind: 'chat' | 'continue', text = '') {
  if (sending.value) return
  const pendingUnsent = kind === 'chat' && pendingImage.value && !pendingImage.value.sent
  if (kind === 'chat' && !text && !pendingUnsent) return
  const originalText = text
  if (kind === 'chat') input.value = ''
  statusText.value = ''
  const assistant = reactive<ChatEntry>({
    role: 'assistant',
    content: '',
    reasoning: '',
    done: false,
  })
  // 气泡在 start 事件后才显示：被拒（含无可继续题目）时不显示任何气泡
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
    stickToBottom = true
    void nextTick(scrollToBottom)
  }
  sending.value = true
  stickToBottom = true
  startElapsedTimer()
  controller = new AbortController()

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
        statusText.value = '⏹ 已停止发送，未发起模型请求。图片已保留，可重试。'
      } else {
        statusText.value =
          err instanceof Error ? `⚠️ ${err.message}` : '⚠️ 图片上传失败，请重试。'
      }
      return
    }
  }

  const requestSettings = currentTurnSettings()
  const onDelta = (d: { answer: string; reasoning: string }) => {
    assistant.content += d.answer
    assistant.reasoning += d.reasoning
    void nextTick(scrollToBottom)
  }

  try {
    const result =
      kind === 'chat'
        ? await streamChat(text, requestSettings, {
            signal: controller.signal,
            imageId,
            // 纯图片发送时文本为服务端默认题目提示，气泡同时展示所发图片
            onStart: (payload) => showBubbles(text || payload.message, imageId ? pendingImage.value?.editedUrl : undefined),
            onDelta,
          })
        : await continueChat(requestSettings, {
            signal: controller.signal,
            // 继续指令由服务端构造，start 事件带回原文仅供展示
            onStart: (payload) => showBubbles(payload.message),
            onDelta,
          })
    if (result.kind === 'rejected') {
      // 被拒：移除占位的助手气泡（如有），保留提示文字；待发送图片保留供重试
      if (assistantShown) {
        messages.value.splice(messages.value.indexOf(assistant), 1)
      }
      statusText.value = result.payload.message
    } else {
      const d = result.payload
      assistant.content = d.answer
      assistant.reasoning = d.reasoning_text
      assistant.done = true
      statusText.value = composeNotice(d)
      usageText.value = [d.cumulative_usage, composeDaily(d)]
        .filter(Boolean)
        .join('\n')
      // 普通发送后按核心的图片状态同步缩略图：保留（标记已发送）或被消费/丢弃
      if (kind === 'chat' && pendingImage.value) {
        if (d.image_kept) pendingImage.value.sent = true
        else discardPendingImage()
      }
      await nextTick(scrollToBottom)
    }
  } catch (err) {
    assistant.done = true
    if (controller?.signal.aborted) {
      statusText.value = '⏹ 已停止接收。未完成的回答不会写入历史。'
    } else if (err instanceof Error) {
      statusText.value = `⚠️ ${err.message}`
    } else {
      statusText.value = '⚠️ 连接中断，请稍后重试。'
    }
  } finally {
    stopElapsedTimer()
    sending.value = false
    controller = null
  }
}

function send() {
  const hasPending = pendingImage.value && !pendingImage.value.sent
  if (!input.value.trim() && !hasPending) return
  runTurn('chat', input.value.trim())
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
  const result = await clearConversation()
  if (result.ok) {
    messages.value = []
    statusText.value = result.message
    usageText.value = ''
    restoreNotice.value = ''
    discardPendingImage()
  } else {
    statusText.value = result.message
  }
}
</script>

<template>
  <div class="page">
    <template v-if="authChecked && !me">
      <form class="login" @submit.prevent="submitLogin">
        <h1>数学学习 AI 助手</h1>
        <p class="hint">请输入访问用户名与密码</p>
        <input
          v-model="loginName"
          autocomplete="username"
          placeholder="用户名"
        />
        <input
          v-model="loginPassword"
          type="password"
          autocomplete="current-password"
          placeholder="密码"
        />
        <button type="submit" :disabled="loggingIn || !loginName.trim() || !loginPassword">
          {{ loggingIn ? '登录中…' : '登录' }}
        </button>
        <p v-if="loginError" class="error">{{ loginError }}</p>
      </form>
    </template>

    <template v-else-if="me">
      <header class="header">
        <h1>数学学习 AI 助手</h1>
        <div class="user-bar">
          <span>{{ me.display_name }}</span>
          <select v-model="mode" :disabled="sending" class="mode-select">
            <option v-for="m in modes" :key="m" :value="m">{{ m }}</option>
          </select>
          <button class="ghost" :disabled="sending" @click="onClearClick">
            {{ clearArmed ? '再点一次确认清空' : '🗑️ 新对话' }}
          </button>
          <button class="ghost" :disabled="sending" @click="doLogout">退出</button>
        </div>
      </header>
      <p v-if="restoreNotice" class="notice">{{ restoreNotice }}</p>

      <details class="settings">
        <summary>⚙️ 设置</summary>
        <div class="settings-grid">
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
            <label>
              思考强度
              <select v-model="effort" :disabled="sending">
                <option value="low">low</option>
                <option value="high">high</option>
                <option value="max">max</option>
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
        </div>
      </details>

      <!-- 管理区域：仅本机直连的登录用户挂载（local_admin 由后端判定），
           默认折叠、首次展开才请求统计，远程用户不加载管理数据 -->
      <AdminPanel v-if="me.local_admin" />

      <main ref="chatScroll" class="chat" @scroll="onScroll">
        <div v-if="messages.length === 0" class="placeholder">
          输入数学问题开始提问。
        </div>
        <template v-for="(msg, i) in messages" :key="i">
          <div v-if="msg.role === 'user'" class="msg user">
            <div class="bubble">
              <img v-if="msg.imageUrl" :src="msg.imageUrl" class="sent-image" alt="发送的题目图片" />
              <span>{{ msg.content }}</span>
            </div>
          </div>
          <div v-else class="msg assistant">
            <details v-if="msg.reasoning" class="thinking" open>
              <summary>思考过程{{ msg.done ? '' : '（进行中）' }}</summary>
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
                ⏳ 正在思考… 已用时 {{ elapsedSeconds }} 秒
              </span>
            </div>
          </div>
        </template>
      </main>

      <section v-if="statusText" class="status">{{ statusText }}</section>
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

      <footer class="input-bar">
        <input
          ref="fileInput"
          type="file"
          accept="image/jpeg,image/png,image/webp,image/bmp,image/gif"
          class="file-picker"
          @change="onFileChange"
        />
        <button
          class="icon-btn img-btn"
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
        <input
          v-model="input"
          :disabled="sending"
          placeholder="输入数学问题，可附图片"
          @keydown.enter="send"
        />
        <button
          v-if="!sending"
          class="ghost continue"
          :disabled="sending"
          @click="continueTurn"
        >
          ▶️ 继续生成
        </button>
        <button v-if="sending" class="send stop" @click="stopStreaming">⏹ 停止</button>
        <button
          v-else
          class="send"
          :disabled="!input.trim() && !(pendingImage && !pendingImage.sent)"
          @click="send"
        >
          发送
        </button>
      </footer>

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
.page {
  max-width: 780px;
  margin: 0 auto;
  height: 100dvh;
  display: flex;
  flex-direction: column;
  padding: 12px 14px 0;
}
.header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
}
.header h1 {
  font-size: 1.1rem;
  margin: 0;
}
.user-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 0.85rem;
  color: #57606a;
}
.mode-select {
  border: 1px solid #d0d7de;
  border-radius: 8px;
  padding: 4px 6px;
  background: #fff;
  font-size: 0.85rem;
}
.user-bar button {
  border: 1px solid #d0d7de;
  background: transparent;
  border-radius: 8px;
  padding: 4px 10px;
  cursor: pointer;
  font-size: 0.85rem;
}
.notice {
  margin: 4px 0 0;
  font-size: 0.85rem;
  color: #9a6700;
}
.settings {
  border: 1px solid #e1e4e8;
  border-radius: 10px;
  margin-top: 8px;
  font-size: 0.88rem;
  background: #fafbfc;
}
.settings summary {
  cursor: pointer;
  user-select: none;
  padding: 6px 10px;
  color: #57606a;
}
.settings-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
  padding: 4px 10px 10px;
}
.settings-grid label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  color: #57606a;
}
.settings-grid label.checkbox {
  flex-direction: row;
  align-items: center;
}
.settings-grid select {
  border: 1px solid #d0d7de;
  border-radius: 8px;
  padding: 4px 6px;
  background: #fff;
  font-size: 0.88rem;
}
.input-bar .ghost.continue {
  border: 1px solid #d0d7de;
  background: #fff;
  color: #1a73e8;
  border-radius: 10px;
  padding: 0 12px;
  font-size: 0.9rem;
  cursor: pointer;
  white-space: nowrap;
}
.input-bar .ghost.continue:disabled {
  opacity: 0.5;
}
.login {
  margin: auto;
  display: flex;
  flex-direction: column;
  gap: 10px;
  width: min(320px, 90%);
  text-align: center;
}
.login h1 {
  font-size: 1.2rem;
  margin: 0;
}
.login .hint {
  margin: 0;
  color: #57606a;
  font-size: 0.9rem;
}
.login input {
  border: 1px solid #d0d7de;
  border-radius: 10px;
  padding: 9px 12px;
  font-size: 0.95rem;
}
.login button {
  border: none;
  background: #1a73e8;
  color: #fff;
  border-radius: 10px;
  padding: 9px 0;
  font-size: 0.95rem;
  cursor: pointer;
}
.login button:disabled {
  opacity: 0.5;
}
.login .error {
  color: #cf222e;
  font-size: 0.88rem;
  margin: 0;
}
.chat {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 12px 2px;
}
.placeholder {
  color: #8b949e;
  text-align: center;
  margin-top: 40px;
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
  white-space: pre-wrap;
  overflow-wrap: break-word;
}
.sent-image {
  display: block;
  max-width: 240px;
  max-height: 180px;
  border-radius: 8px;
  margin-bottom: 6px;
  background: rgba(255, 255, 255, 0.9);
}
/* ---- 待发送图片缩略图条 ---- */
.pending-image {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 6px 0 0;
  padding: 6px 8px;
  border: 1px solid #e1e4e8;
  border-radius: 12px;
  background: #fafbfc;
}
.pending-image .thumb {
  width: 52px;
  height: 52px;
  object-fit: cover;
  border-radius: 8px;
  border: 1px solid #e1e4e8;
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
  color: #8b949e;
  font-variant-numeric: tabular-nums;
}
.pending-badge {
  align-self: flex-start;
  font-size: 0.72rem;
  color: #1a73e8;
  background: #eaf2fe;
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
  border: 1px solid #d0d7de;
  background: #fff;
  color: #43505e;
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
  border-color: #1a73e8;
  color: #1a73e8;
  background: #f2f7ff;
}
.chip.danger:hover:not(:disabled) {
  border-color: #cf222e;
  color: #cf222e;
  background: #fff5f5;
}
.chip:disabled {
  opacity: 0.5;
  cursor: default;
}
.chip:focus-visible,
.img-btn:focus-visible {
  outline: 2px solid #1a73e8;
  outline-offset: 1px;
}
.file-picker {
  display: none;
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
.waiting {
  color: #8b949e;
  font-size: 0.9em;
}
.status {
  border-top: 1px solid #e1e4e8;
  padding: 8px 2px;
  font-size: 0.88rem;
  color: #57606a;
  white-space: pre-wrap;
  overflow-wrap: break-word;
}
.usage {
  padding: 0 2px 6px;
  font-size: 0.85rem;
  color: #57606a;
  white-space: pre-wrap;
}
.input-bar {
  display: flex;
  gap: 8px;
  padding: 10px 0 12px;
  align-items: center;
}
.img-btn {
  border: 1px solid #d0d7de;
  background: #fff;
  color: #57606a;
  width: 40px;
  height: 40px;
  border-radius: 10px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.img-btn svg {
  width: 19px;
  height: 19px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.img-btn:hover:not(:disabled) {
  border-color: #1a73e8;
  color: #1a73e8;
  background: #f2f7ff;
}
.img-btn:disabled {
  opacity: 0.5;
  cursor: default;
}
.input-bar input:not(.file-picker) {
  flex: 1;
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
.input-bar .send.stop {
  background: #cf222e;
}
@media (max-width: 560px) {
  .page {
    padding: 8px 8px 0;
  }
  .msg.user .bubble,
  .msg.assistant .bubble {
    max-width: 94%;
  }
}
</style>
