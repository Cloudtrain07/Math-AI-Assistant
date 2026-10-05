/**
 * 新 API 客户端：登录会话走 HttpOnly Cookie（同源自动携带），
 * 聊天走 fetch + SSE 流式解析。浏览器不保存账号密码与任何内部配置。
 */

import { SseParser } from './sse'

export interface UserInfo {
  username: string
  display_name: string
  level: string
}

export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
}

export interface DonePayload {
  answer: string
  reasoning_text: string
  truncated: boolean
  error: string
  empty_reply: boolean
  turn_total: number | null
  usage_note: string
  cumulative_usage: string
  save_note: string
  save_status: string | null
  daily_note: string
  daily_used: number | null
  global_used: number | null
  daily_limit: number
  global_daily_limit: number
  image_status: string
}

export interface RejectedPayload {
  status: string
  message: string
}

export interface StartPayload {
  /** 本轮消息原文；继续生成时为服务端构造的继续指令，仅用于展示 */
  message: string
}

/** 单轮界面设置：随聊天/继续生成请求提交，后端验证后选择对应客户端 */
export interface TurnSettings {
  mode: string
  level: string
  thinking: boolean
  provider_id: string | null
  model: string
  answer_style: string
  reasoning_effort: string
}

/** /api/settings 返回的界面设置清单：只含展示字段，无任何内部配置 */
export interface ProviderOption {
  id: string
  name: string
  models: string[]
  default_model: string | null
  supports_thinking: boolean
}

export interface SettingsInfo {
  providers: ProviderOption[]
  default_provider: string | null
  levels: string[]
  modes: string[]
  answer_styles: string[]
  default_level: string
  default_mode: string
  default_answer_style: string
  default_thinking: boolean
  default_reasoning_effort: string
  startup_hint: string
}

export interface StreamHandlers {
  onStart?: (payload: StartPayload) => void
  onDelta?: (delta: { answer: string; reasoning: string }) => void
  onPing?: () => void
  /** 返回 done 载荷；被拒（rejected）时返回 null */
  onDone?: (payload: DonePayload | null, rejected?: RejectedPayload) => void
}

async function readJson(resp: Response): Promise<any> {
  try {
    return await resp.json()
  } catch {
    return null
  }
}

export async function login(
  username: string,
  password: string,
): Promise<UserInfo> {
  const resp = await fetch('/api/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  const body = await readJson(resp)
  if (!resp.ok) {
    throw new Error((body && body.detail) || '登录失败，请稍后重试')
  }
  return body as UserInfo
}

export async function logout(): Promise<void> {
  await fetch('/api/logout', { method: 'POST' })
}

/** 会话有效返回用户信息；未登录/过期返回 null。 */
export async function fetchMe(): Promise<UserInfo | null> {
  const resp = await fetch('/api/me')
  if (!resp.ok) return null
  return (await resp.json()) as UserInfo
}

export async function fetchHistory(): Promise<{
  history: ChatMessage[]
  notice: string
}> {
  const resp = await fetch('/api/history')
  if (!resp.ok) return { history: [], notice: '⚠️ 聊天记录读取失败。' }
  return await resp.json()
}

export async function clearConversation(): Promise<{
  ok: boolean
  message: string
}> {
  const resp = await fetch('/api/clear', { method: 'POST' })
  return await resp.json()
}

/** 登录后读取界面设置清单；读取失败返回 null（前端按缺省值展示）。 */
export async function fetchSettings(): Promise<SettingsInfo | null> {
  const resp = await fetch('/api/settings')
  if (!resp.ok) return null
  try {
    return (await resp.json()) as SettingsInfo
  } catch {
    return null
  }
}

/**
 * 消费一轮聊天的 SSE 流（/api/chat 与 /api/continue 共用）。
 * - 网络错误 / HTTP 非 2xx：抛出异常（含可展示信息）；
 * - 被拒：返回 { kind: 'rejected', ... }；
 * - 正常收尾：返回 { kind: 'done', payload }；
 * - 调用方 abort（停止按钮）：fetch 抛 AbortError，原样向上抛。
 */
async function consumeTurnStream(
  resp: Response,
  handlers: StreamHandlers,
): Promise<
  { kind: 'done'; payload: DonePayload } | { kind: 'rejected'; payload: RejectedPayload }
> {
  if (resp.status === 401) {
    throw new Error('登录已过期，请重新登录')
  }
  if (!resp.ok || !resp.body) {
    throw new Error(`请求失败（HTTP ${resp.status}）`)
  }

  const parser = new SseParser()
  const reader = resp.body.getReader()
  let done: DonePayload | null = null
  let rejected: RejectedPayload | null = null

  for (;;) {
    const { value, done: finished } = await reader.read()
    if (value) {
      for (const ev of parser.push(value)) {
        if (ev.event === 'start') handlers.onStart?.(JSON.parse(ev.data))
        else if (ev.event === 'delta') {
          const d = JSON.parse(ev.data)
          handlers.onDelta?.({ answer: d.answer || '', reasoning: d.reasoning || '' })
        } else if (ev.event === 'ping') handlers.onPing?.()
        else if (ev.event === 'rejected') rejected = JSON.parse(ev.data)
        else if (ev.event === 'done') done = JSON.parse(ev.data)
      }
    }
    if (finished) break
  }
  for (const ev of parser.flush()) {
    if (ev.event === 'done') done = JSON.parse(ev.data)
    else if (ev.event === 'rejected') rejected = JSON.parse(ev.data)
  }

  if (rejected) return { kind: 'rejected', payload: rejected }
  if (done) return { kind: 'done', payload: done }
  throw new Error('连接中断：未收到完整回答')
}

/** 发送一条消息并消费 SSE 流。 */
export async function streamChat(
  message: string,
  settings: TurnSettings,
  handlers: StreamHandlers & { signal?: AbortSignal },
): Promise<
  { kind: 'done'; payload: DonePayload } | { kind: 'rejected'; payload: RejectedPayload }
> {
  const resp = await fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, ...settings }),
    signal: handlers.signal,
  })
  return consumeTurnStream(resp, handlers)
}

/**
 * 继续生成：不带消息文本，服务端按服务端历史构造继续指令
 * （复用普通发送的核心流程、busy 保护与用量结算）。
 */
export async function continueChat(
  settings: TurnSettings,
  handlers: StreamHandlers & { signal?: AbortSignal },
): Promise<
  { kind: 'done'; payload: DonePayload } | { kind: 'rejected'; payload: RejectedPayload }
> {
  const resp = await fetch('/api/continue', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...settings }),
    signal: handlers.signal,
  })
  return consumeTurnStream(resp, handlers)
}
