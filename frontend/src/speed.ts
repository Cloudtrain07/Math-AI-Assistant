export interface SpeedFields {
  completion_tokens?: number | null
  gen_seconds?: number | null
  first_token_seconds?: number | null
  tok_s?: number | null
  tok_s_estimated?: boolean
}

export function liveTokS(charCount: number, seconds: number): number | null {
  if (charCount <= 0 || seconds <= 0) return null
  return Math.round((charCount / 2 / seconds) * 10) / 10
}

export function formatLiveSpeed(value: number | null): string {
  return value === null ? '' : `约 ${value} tok/s`
}

export function formatAvgSpeed(d: SpeedFields): string {
  if (d.tok_s === null || d.tok_s === undefined) return ''
  const label = d.tok_s_estimated ? '平均约' : '平均'
  const tokens = d.completion_tokens ?? 0
  const seconds = (d.gen_seconds ?? 0).toFixed(1)
  return `${label} ${d.tok_s} tok/s（输出 ${tokens} tokens · 生成 ${seconds}s）`
}