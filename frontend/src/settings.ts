/**
 * 服务商切换的纯同步逻辑（与旧界面 on_provider_change 对齐）：
 * 切换服务商时模型列表与选中值同步为新服务商的配置，思考控件按能力显隐，
 * 避免残留上一个服务商的无效模型设置。
 *
 * 思考档位规则由后端 /api/settings 的 model_thinking 下发（共享同一份
 * 能力函数），前端只负责按元数据同步控件与合法选中值。
 */

import type { ModelThinking, ProviderOption } from './api'

export interface ProviderView {
  /** 该服务商的模型选项 */
  models: string[]
  /** 切换后选中的模型：默认模型，缺省时取第一个选项 */
  model: string
  /** 是否显示深度思考开关与思考强度 */
  supportsThinking: boolean
}

export function providerView(
  providers: ProviderOption[],
  providerId: string | null,
): ProviderView {
  const provider = providers.find((p) => p.id === providerId)
  if (!provider) {
    return { models: [], model: '', supportsThinking: false }
  }
  const model = provider.default_model ?? provider.models[0] ?? ''
  return { models: provider.models, model, supportsThinking: provider.supports_thinking }
}

/** 无档位时的兜底能力：不显示强度选择，也不发送强度参数 */
export const EMPTY_THINKING: ModelThinking = { effort_levels: [], default_effort: null }

/** 当前模型的思考档位元数据；缺失（如元数据未覆盖）时按无档位处理 */
export function modelThinking(
  provider: ProviderOption | null | undefined,
  model: string,
): ModelThinking {
  return provider?.model_thinking?.[model] ?? EMPTY_THINKING
}

/**
 * 切换服务商/模型后同步思考强度选中值：
 * - 新模型无档位（Qwen3.7、MiMo）：清空，不发送强度参数；
 * - 当前值仍合法：保留；
 * - 当前值非法（如从 DeepSeek 的 high 切到 Qwen3.8）：换成新模型的默认档位。
 */
export function syncEffort(capability: ModelThinking, current: string | null): string | null {
  if (capability.effort_levels.length === 0) return null
  if (current !== null && capability.effort_levels.includes(current)) return current
  return capability.default_effort ?? capability.effort_levels[0] ?? null
}
