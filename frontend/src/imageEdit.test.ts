import { describe, expect, it } from 'vitest'
import {
  defaultEditParams,
  expectedExportSize,
  isOverSize,
  isSupportedImageFile,
  MAX_IMAGE_BYTES,
  nextDownscale,
} from './imageEdit'

describe('isSupportedImageFile', () => {
  it('放行核心支持的格式（MIME 或扩展名）', () => {
    for (const type of [
      'image/jpeg',
      'image/png',
      'image/webp',
      'image/bmp',
      'image/gif',
    ]) {
      expect(isSupportedImageFile({ type, name: 'a' })).toBe(true)
    }
    expect(isSupportedImageFile({ type: '', name: 'a.JPG' })).toBe(true)
  })

  it('拒绝不支持的格式（如 HEIC / SVG）', () => {
    expect(isSupportedImageFile({ type: 'image/heic', name: 'a.heic' })).toBe(false)
    expect(isSupportedImageFile({ type: 'image/svg+xml', name: 'a.svg' })).toBe(false)
    expect(isSupportedImageFile({ type: '', name: 'a.bin' })).toBe(false)
  })
})

describe('isOverSize', () => {
  it('与核心 10MB 上限一致', () => {
    expect(isOverSize({ size: MAX_IMAGE_BYTES })).toBe(false)
    expect(isOverSize({ size: MAX_IMAGE_BYTES + 1 })).toBe(true)
  })
})

describe('expectedExportSize', () => {
  it('无裁剪时全图导出，90° 旋转交换宽高', () => {
    const base = defaultEditParams()
    expect(expectedExportSize(400, 300, base)).toEqual({ width: 400, height: 300 })
    expect(expectedExportSize(400, 300, { rotate: 90, crop: null })).toEqual({
      width: 300,
      height: 400,
    })
    expect(expectedExportSize(400, 300, { rotate: 180, crop: null })).toEqual({
      width: 400,
      height: 300,
    })
    expect(expectedExportSize(400, 300, { rotate: -90, crop: null })).toEqual({
      width: 300,
      height: 400,
    })
  })

  it('局部裁剪 + 旋转按外接矩形计算', () => {
    const crop = { x: 10, y: 10, width: 200, height: 100 }
    expect(expectedExportSize(800, 600, { rotate: 90, crop })).toEqual({
      width: 100,
      height: 200,
    })
    // 30° 微调：外接矩形 200·cos30 + 100·sin30 ≈ 223，200·sin30 + 100·cos30 ≈ 187
    const size = expectedExportSize(800, 600, { rotate: 30, crop })
    expect(size.width).toBe(223)
    expect(size.height).toBe(187)
  })
})

describe('nextDownscale', () => {
  it('按固定步长缩边，到 1px 停止', () => {
    expect(nextDownscale(1000, 800)).toEqual({ width: 750, height: 600 })
    expect(nextDownscale(1, 800)).toBeNull()
  })
})
