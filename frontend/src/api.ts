/**
 * 新 API 客户端：登录会话走 HttpOnly Cookie（同源自动携带），
 * 聊天走 fetch + SSE 流式解析。浏览器不保存账号密码与任何内部配置。
 */

import { SseParser } from './sse'

export interface UserInfo {
  username: string
  display_name: string
  level: string
  /** 仅本机直连的登录用户为 true；远程用户据此不展示、不请求管理数据 */
  local_admin?: boolean
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
  daily_status: string | null
  daily_note: string
  daily_used: number | null
  global_used: number | null
  daily_limit: number
  global_daily_limit: number
  image_status: string
  /** 本轮结束后核心是否仍保留图片状态（false 表示已被消费或按新题丢弃） */
  image_kept: boolean
  completion_tokens?: number | null
  gen_seconds?: number | null
  first_token_seconds?: number | null
  tok_s?: number | null
  tok_s_estimated?: boolean
}

export interface RejectedPayload {
  status: string
  message: string
}

export interface StartPayload {
  /** 本轮消息原文；继续生成时为服务端构造的继续指令，仅用于展示 */
  message: string
}

/** 单轮界面设置：随聊天/继续生成请求提交，后端验证后选择对应客户端。
 * reasoning_effort 为 null 表示当前模型不提供思考强度（后端按缺省处理）；
 * 两个覆盖值为 null 表示留空、沿用服务器 .env 配置，仅作用于本轮请求 */
export interface TurnSettings {
  mode: string
  level: string
  thinking: boolean
  provider_id: string | null
  model: string
  answer_style: string
  reasoning_effort: string | null
  max_output_tokens: number | null
  max_wait_seconds: number | null
}

/** 单个模型的思考档位信息（来自后端共享能力规则，前端不自行维护） */
export interface ModelThinking {
  /** 可选档位；空数组表示该模型不提供思考强度选择 */
  effort_levels: string[]
  /** 切换到该模型时同步的默认档位；无档位时为 null */
  default_effort: string | null
}

/** /api/settings 返回的界面设置清单：只含展示字段，无任何内部配置 */
export interface ProviderOption {
  id: string
  name: string
  models: string[]
  default_model: string | null
  supports_thinking: boolean
  /** 模型名 → 思考档位；覆盖该服务商的全部模型 */
  model_thinking: Record<string, ModelThinking>
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
  /** 网页覆盖值的展示默认：输入框留空时提示沿用这两个服务器配置值 */
  default_max_output_tokens: number
  default_max_wait_seconds: number
  startup_hint: string
}

export interface StreamHandlers {
  onStart?: (payload: StartPayload) => void
  onDelta?: (delta: { answer: string; reasoning: string }) => void
  onUsage?: (tokens: number) => void
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
    // 后端 400/503 返回的字符串 detail 直接展示（含设置校验的中文提示）；
    // 没有可读 detail（如 422 的结构化明细）时保留通用错误
    const body = await readJson(resp)
    const detail = body && typeof body.detail === 'string' ? body.detail : ''
    throw new Error(detail || `请求失败（HTTP ${resp.status}）`)
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
        } else if (ev.event === 'usage') {
          const d = JSON.parse(ev.data)
          if (Number.isInteger(d.completion_tokens) && d.completion_tokens >= 0) handlers.onUsage?.(d.completion_tokens)
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

/**
 * 上传编辑确认后的待发送图片，返回服务端临时图片 id（仅点击发送时调用）。
 * signal 用于上传阶段停止：中止后不再发起聊天请求。
 */
export async function uploadImage(
  blob: Blob,
  name = 'edited.jpg',
  signal?: AbortSignal,
): Promise<{ image_id: string; width: number; height: number }> {
  const form = new FormData()
  form.append('file', blob, name)
  const resp = await fetch('/api/images', { method: 'POST', body: form, signal })
  const body = await readJson(resp)
  if (!resp.ok) {
    throw new Error((body && body.detail) || '图片上传失败，请重试')
  }
  return body as { image_id: string; width: number; height: number }
}

/** 显式移除图片（含阻止上一条消息的附图在后续轮次重新附带）。 */
export async function removeImage(): Promise<{ ok: boolean; message?: string }> {
  const resp = await fetch('/api/images', { method: 'DELETE' })
  if (!resp.ok) return { ok: false }
  return await resp.json()
}

// ---- 管理员接口（后端仅放行本机直连的已登录用户） ----

export interface AdminAccountUsage {
  username: string
  display_name: string
  enabled: boolean
  /** 今日已用 tokens */
  used: number
}

export interface AdminStats {
  date: string
  global_used: number
  global_daily_limit: number
  logins: number
  questions: number
  accounts: AdminAccountUsage[]
}

/** 读取今日管理统计；未登录 401、非本机 403、读取失败 503，均抛可展示错误 */
export async function fetchAdminStats(): Promise<AdminStats> {
  const resp = await fetch('/api/admin/stats')
  const body = await readJson(resp)
  if (!resp.ok) {
    throw new Error((body && body.detail) || '用量数据读取失败，请稍后重试')
  }
  return body as AdminStats
}

/** 重置指定账号密码；请求与响应、日志都不包含任何密码 */
export async function adminResetPassword(
  username: string,
  newPassword: string,
): Promise<{ ok: boolean }> {
  const resp = await fetch('/api/admin/reset-password', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, new_password: newPassword }),
  })
  const body = await readJson(resp)
  if (!resp.ok) {
    throw new Error((body && body.detail) || '重置失败，请稍后重试')
  }
  return body as { ok: boolean }
}

/** 发送一条消息并消费 SSE 流；imageId 来自发送前的 uploadImage。 */
export async function streamChat(
  message: string,
  settings: TurnSettings,
  handlers: StreamHandlers & { signal?: AbortSignal; imageId?: string | null },
): Promise<
  { kind: 'done'; payload: DonePayload } | { kind: 'rejected'; payload: RejectedPayload }
> {
  const body: Record<string, unknown> = { message, ...settings }
  if (handlers.imageId) body.image_id = handlers.imageId
  const resp = await fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
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
