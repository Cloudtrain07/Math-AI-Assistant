/**
 * 网页覆盖的输出上限解析（纯函数，不接触 DOM）：
 * 输入框文本 → 校验并解析；空白沿用服务器配置（解析为 null）。
 * 与后端 api.py 的 WEB_MIN/MAX_* 常量保持同一范围，前端拦截为主、
 * 后端同样严格校验整数（拒绝布尔、小数与越界值）兜底。
 */

export interface WebLimitBounds {
  min: number
  max: number
  label: string
}

/** 与 api.py WEB_* 常量一致；48000 作为试用更长输出的示例值 */
export const OUTPUT_TOKENS_BOUNDS: WebLimitBounds = {
  min: 100,
  max: 200_000,
  label: '最大输出 token',
}

export const WAIT_SECONDS_BOUNDS: WebLimitBounds = {
  min: 15,
  max: 7200,
  label: '最大等待时间',
}

export type ParsedWebLimit =
  | { ok: true; value: number | null }
  | { ok: false; message: string }

/**
 * raw 兼容 string 与 number：Vue 的 v-model 对 type=number 输入会把
 * 填写的值自动转成 number（留空时仍是空字符串），解析前必须归一化，
 * 不能对数字直接调字符串方法。
 */
export function parseWebLimit(raw: string | number, bounds: WebLimitBounds): ParsedWebLimit {
  const text = typeof raw === 'number' ? String(raw) : raw.trim()
  // 空白 = 留空沿用服务器配置，不发送覆盖值
  if (text === '') return { ok: true, value: null }
  const value = Number(text)
  if (!Number.isInteger(value)) {
    return {
      ok: false,
      message: `⚠️ ${bounds.label}必须是 ${bounds.min}–${bounds.max} 之间的整数，请修改后再发送。`,
    }
  }
  if (value < bounds.min || value > bounds.max) {
    return {
      ok: false,
      message: `⚠️ ${bounds.label}需在 ${bounds.min}–${bounds.max} 之间（当前 ${value}），请修改后再发送。`,
    }
  }
  return { ok: true, value }
}
