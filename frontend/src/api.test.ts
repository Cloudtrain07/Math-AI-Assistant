import { afterEach, describe, expect, it, vi } from 'vitest'
import { uploadImage } from './api'

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
