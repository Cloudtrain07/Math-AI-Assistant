import { afterEach, describe, expect, it, vi } from 'vitest'
import { adminResetPassword, fetchAdminStats, uploadImage } from './api'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('uploadImage（上传阶段停止）', () => {
  it('把 AbortSignal 原样传给 fetch，停止时中止且不解析响应', async () => {
    const fetchMock = vi.fn(
      (_url: string, init?: { signal?: AbortSignal }) =>
        new Promise<Response>((_, reject) => {
          init?.signal?.addEventListener('abort', () =>
            reject(new DOMException('This operation was aborted', 'AbortError')),
          )
        }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const controller = new AbortController()
    const pending = uploadImage(new Blob(['x']), 'a.jpg', controller.signal)
    controller.abort()
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/images',
      expect.objectContaining({ signal: controller.signal }),
    )
  })

  it('未中止时正常返回服务端 image_id', async () => {
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ image_id: 'abc', width: 2, height: 3 }), {
        status: 200,
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const result = await uploadImage(new Blob(['x']), 'a.jpg')
    expect(result).toEqual({ image_id: 'abc', width: 2, height: 3 })
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/images',
      expect.objectContaining({ method: 'POST' }),
    )
  })
})

describe('管理员接口', () => {
  it('fetchAdminStats：非 2xx 时抛出后端 detail（含 503 读取失败）', async () => {
    const fetchMock = vi.fn(async () =>
      new Response(
        JSON.stringify({ detail: '今日用量读取失败，请稍后重试（模拟损坏）' }),
        { status: 503 },
      ),
    )
    vi.stubGlobal('fetch', fetchMock)

    await expect(fetchAdminStats()).rejects.toThrow('今日用量读取失败')
    expect(fetchMock).toHaveBeenCalledWith('/api/admin/stats')
  })

  it('fetchAdminStats：正常返回结构化统计', async () => {
    const payload = {
      date: '2026-10-05',
      global_used: 1234,
      global_daily_limit: 3000000,
      logins: 2,
      questions: 5,
      accounts: [
        { username: 'alice', display_name: '小明', enabled: true, used: 1234 },
      ],
    }
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify(payload), { status: 200 })),
    )

    const stats = await fetchAdminStats()
    expect(stats).toEqual(payload)
    expect(stats.accounts[0]).not.toHaveProperty('password')
  })

  it('adminResetPassword：按 snake_case 提交用户名与新密码', async () => {
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ ok: true }), { status: 200 }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const result = await adminResetPassword('bob', 'new-pw')
    expect(result).toEqual({ ok: true })
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/admin/reset-password',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ username: 'bob', new_password: 'new-pw' }),
      }),
    )
  })

  it('adminResetPassword：失败时抛出后端 detail', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(JSON.stringify({ detail: '新密码不能为空' }), { status: 400 }),
      ),
    )
    await expect(adminResetPassword('bob', ' ')).rejects.toThrow('新密码不能为空')
  })
})
