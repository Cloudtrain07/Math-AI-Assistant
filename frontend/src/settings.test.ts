import { describe, expect, it } from 'vitest'
import { modelThinking, providerView, syncEffort } from './settings'
import type { ModelThinking, ProviderOption } from './api'

const providers: ProviderOption[] = [
  {
    id: 'deepseek',
    name: 'DeepSeek',
    models: ['deepseek-v4-flash', 'deepseek-v4-pro'],
    default_model: 'deepseek-v4-flash',
    supports_thinking: true,
    model_thinking: {
      'deepseek-v4-flash': { effort_levels: ['low', 'high', 'max'], default_effort: 'high' },
      'deepseek-v4-pro': { effort_levels: ['low', 'high', 'max'], default_effort: 'high' },
    },
  },
  {
    id: 'plain',
    name: 'Plain',
    models: ['plain-model'],
    default_model: null,
    supports_thinking: false,
    model_thinking: {
      'plain-model': { effort_levels: [], default_effort: null },
    },
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

describe('modelThinking', () => {
  it('返回当前模型的档位元数据', () => {
    expect(modelThinking(providers[0], 'deepseek-v4-flash')).toEqual({
      effort_levels: ['low', 'high', 'max'],
      default_effort: 'high',
    })
  })

  it('无档位的模型返回空档位列表', () => {
    expect(modelThinking(providers[1], 'plain-model')).toEqual({
      effort_levels: [],
      default_effort: null,
    })
  })

  it('服务商缺失或元数据未覆盖时按无档位处理（不发送强度参数）', () => {
    expect(modelThinking(null, 'deepseek-v4-flash')).toEqual({
      effort_levels: [],
      default_effort: null,
    })
    expect(modelThinking(providers[0], 'unknown-model')).toEqual({
      effort_levels: [],
      default_effort: null,
    })
  })
})

describe('syncEffort', () => {
  const deepseek = modelThinking(providers[0], 'deepseek-v4-flash')
  const qwen38: ModelThinking = {
    effort_levels: ['low', 'medium', 'xhigh'],
    default_effort: 'xhigh',
  }
  const noLevels: ModelThinking = { effort_levels: [], default_effort: null }

  it('合法值保留：DeepSeek 的 high 切换后不变', () => {
    expect(syncEffort(deepseek, 'high')).toBe('high')
  })

  it('DeepSeek 的 high 切到 Qwen3.8 时同步为默认档位 xhigh（官方映射值不再展示）', () => {
    expect(syncEffort(qwen38, 'high')).toBe('xhigh')
  })

  it('Qwen3.8 的 xhigh 切回 DeepSeek 时换成 DeepSeek 默认档位', () => {
    expect(syncEffort(deepseek, 'xhigh')).toBe('high')
  })

  it('切到无档位模型（Qwen3.7 / MiMo）时清空强度', () => {
    expect(syncEffort(noLevels, 'high')).toBeNull()
    expect(syncEffort(noLevels, null)).toBeNull()
  })

  it('当前值为 null 且模型有档位时取默认档位', () => {
    expect(syncEffort(qwen38, null)).toBe('xhigh')
  })
})
