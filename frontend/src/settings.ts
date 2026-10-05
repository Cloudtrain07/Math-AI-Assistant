/**
 * 服务商切换的纯同步逻辑（与旧界面 on_provider_change 对齐）：
 * 切换服务商时模型列表与选中值同步为新服务商的配置，思考控件按能力显隐，
 * 避免残留上一个服务商的无效模型设置。
 */

import type { ProviderOption } from './api'

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
