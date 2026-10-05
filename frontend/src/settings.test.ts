import { describe, expect, it } from 'vitest'
import { providerView } from './settings'
import type { ProviderOption } from './api'

const providers: ProviderOption[] = [
  {
    id: 'deepseek',
    name: 'DeepSeek',
    models: ['deepseek-v4-flash', 'deepseek-v4-pro'],
    default_model: 'deepseek-v4-flash',
    supports_thinking: true,
  },
  {
    id: 'plain',
    name: 'Plain',
    models: ['plain-model'],
    default_model: null,
    supports_thinking: false,
  },
]

describe('providerView', () => {
  it('返回所选服务商的模型列表与默认模型', () => {
    expect(providerView(providers, 'deepseek')).toEqual({
      models: ['deepseek-v4-flash', 'deepseek-v4-pro'],
      model: 'deepseek-v4-flash',
      supportsThinking: true,
    })
  })

  it('默认模型缺失时回退到第一个选项', () => {
    expect(providerView(providers, 'plain')).toEqual({
      models: ['plain-model'],
      model: 'plain-model',
      supportsThinking: false,
    })
  })

  it('未知或未选服务商返回空视图（思考控件隐藏）', () => {
    expect(providerView(providers, 'nope')).toEqual({
      models: [],
      model: '',
      supportsThinking: false,
    })
    expect(providerView(providers, null)).toEqual({
      models: [],
      model: '',
      supportsThinking: false,
    })
  })
})
