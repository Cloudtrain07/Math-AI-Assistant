export function browserStorage(): Storage | null {
  try { return window.localStorage } catch { return null }
}

/** Browser-local preference; a removed provider always falls back to the server default. */
export function selectedProvider(storage: Storage | null, username: string, providers: { id: string }[], fallback: string | null): string | null {
  try {
    const saved = storage?.getItem(`maa.provider.${encodeURIComponent(username)}`)
    return saved && providers.some(p => p.id === saved) ? saved : fallback
  } catch { return fallback }
}

export function saveProvider(storage: Storage | null, username: string, provider: string): void {
  try { storage?.setItem(`maa.provider.${encodeURIComponent(username)}`, provider) } catch { /* storage may be unavailable */ }
}
