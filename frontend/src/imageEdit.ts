/**
 * 图片编辑纯逻辑：格式/大小校验、编辑参数、导出尺寸与压缩决策。
 *
 * 像素处理全部交给 Cropper.js（getCroppedCanvas 按原图自然分辨率导出），
 * 这里只做与 UI 无关的可测试计算：
 * - 服务端与本地共用 10MB 上限；
 * - 导出前按画布安全上限收边（超出浏览器画布可靠范围时主动缩小并提示，
 *   不静默）；
 * - 超过 10MB 时按固定步长缩边重编码，同样提示。
 */

export const MAX_IMAGE_BYTES = 10 * 1024 * 1024
export const EXPORT_QUALITY = 0.92
/** 浏览器画布可靠上限兜底：超过此边长的导出先等比缩小（Chrome/Firefox/Safari 均可靠） */
export const MAX_EXPORT_EDGE = 8192
/** 超过 10MB 时的重编码缩边步长 */
export const DOWNSCALE_FACTOR = 0.75

/** 与 core/vision_client.py SUPPORTED_EXTS 对齐的浏览器可编辑格式 */
export const SUPPORTED_MIME_TYPES = [
  'image/jpeg',
  'image/png',
  'image/webp',
  'image/bmp',
  'image/gif',
]
const SUPPORTED_EXTS = ['jpg', 'jpeg', 'png', 'webp', 'bmp', 'gif']

/** 编辑弹窗的输入：原始文件（始终以此为准）与其文件名 */
export interface EditorSource {
  blob: Blob
  name: string
}

/** 编辑弹窗的输出：导出的真实像素与编辑参数（供后续恢复编辑） */
export interface EditorResult {
  blob: Blob
  width: number
  height: number
  params: EditParams
  /** 非空时为需要展示给用户的导出调整提示（收边 / 压缩） */
  note: string
}

/** 一次编辑会话的参数：始终基于原图恢复，避免对导出结果反复处理 */
export interface EditParams {
  /** 相对原图的累计旋转角度（度，顺时针为正；含 90° 步进与微调） */
  rotate: number
  /** 裁剪框（原图自然像素坐标，来自 cropper.getData()）；null 表示全图 */
  crop: { x: number; y: number; width: number; height: number } | null
}

export function defaultEditParams(): EditParams {
  return { rotate: 0, crop: null }
}

export function isSupportedImageFile(file: { type: string; name: string }): boolean {
  if (SUPPORTED_MIME_TYPES.includes(file.type)) return true
  const ext = file.name.split('.').pop()?.toLowerCase() ?? ''
  return SUPPORTED_EXTS.includes(ext)
}

export function isOverSize(file: { size: number }): boolean {
  return file.size > MAX_IMAGE_BYTES
}

/**
 * 预测导出画布尺寸（原图自然像素）：裁剪区在旋转 θ 后的外接矩形。
 *Cropper.getCroppedCanvas 缺省即按此尺寸输出，用于导出前校验与测试对照。
 */
export function expectedExportSize(
  naturalWidth: number,
  naturalHeight: number,
  params: EditParams,
): { width: number; height: number } {
  const crop = params.crop ?? {
    x: 0,
    y: 0,
    width: naturalWidth,
    height: naturalHeight,
  }
  const rad = (params.rotate * Math.PI) / 180
  const cos = Math.abs(Math.cos(rad))
  const sin = Math.abs(Math.sin(rad))
  return {
    width: Math.round(crop.width * cos + crop.height * sin),
    height: Math.round(crop.width * sin + crop.height * cos),
  }
}

/**
 * 导出前的画布收边决策：超过 MAX_EXPORT_EDGE 时等比缩小并标记需要提示。
 */
export function clampCanvasSize(
  width: number,
  height: number,
): { width: number; height: number; scaled: boolean } {
  const maxEdge = Math.max(width, height)
  if (maxEdge <= MAX_EXPORT_EDGE) return { width, height, scaled: false }
  const ratio = MAX_EXPORT_EDGE / maxEdge
  return {
    width: Math.max(1, Math.round(width * ratio)),
    height: Math.max(1, Math.round(height * ratio)),
    scaled: true,
  }
}

/**
 * 压缩循环的下一次缩边尺寸；返回 null 表示已到最小（1px）无法继续。
 */
export function nextDownscale(
  width: number,
  height: number,
): { width: number; height: number } | null {
  if (width <= 1 || height <= 1) return null
  return {
    width: Math.max(1, Math.round(width * DOWNSCALE_FACTOR)),
    height: Math.max(1, Math.round(height * DOWNSCALE_FACTOR)),
  }
}

export function formatBytes(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`
  return `${bytes} B`
}

/** 画布 → JPEG Blob；浏览器不支持 toBlob 时拒绝（理论上不会发生）。 */
export function canvasToJpeg(
  canvas: HTMLCanvasElement,
  quality = EXPORT_QUALITY,
): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob(
      (blob) => {
        if (blob) resolve(blob)
        else reject(new Error('图片导出失败'))
      },
      'image/jpeg',
      quality,
    )
  })
}
