<script setup lang="ts">
/**
 * 图片编辑弹窗：基于 Cropper.js v1 的自由比例裁剪 / 90° 旋转 / 微调角度 /
 * 缩放移动 / 重置。像素处理全部由 Cropper 在原图自然分辨率上完成
 * （getCroppedCanvas），界面预览缩放不影响导出像素。
 *
 * 恢复编辑：始终从原图 + EditParams（rotate + 原图坐标裁剪框）重建，
 * 不对上次导出的 JPEG 反复处理；取消时调用方保留此前已确认的版本。
 */
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import Cropper from 'cropperjs'
import 'cropperjs/dist/cropper.css'
import {
  MAX_EXPORT_EDGE,
  MAX_IMAGE_BYTES,
  canvasToJpeg,
  defaultEditParams,
  expectedExportSize,
  nextDownscale,
  type EditParams,
  type EditorResult,
  type EditorSource,
} from '../imageEdit'

const props = defineProps<{
  open: boolean
  source: EditorSource | null
  initialParams: EditParams | null
}>()

const emit = defineEmits<{
  (e: 'cancel'): void
  (e: 'confirm', result: EditorResult): void
}>()

const imgEl = ref<HTMLImageElement | null>(null)
const fineAngle = ref(0)
const exporting = ref(false)
const busyNote = ref('')
/** cropper 就绪状态（响应式；cropper 实例本身保持非响应式引用） */
const cropperReady = ref(false)

let cropper: Cropper | null = null
let objectUrl = ''
let baseRotation = 0
let naturalWidth = 0
let naturalHeight = 0

watch(
  () => props.open,
  async (open) => {
    if (open) {
      if (props.source) objectUrl = URL.createObjectURL(props.source.blob)
      baseRotation = 0
      fineAngle.value = 0
      busyNote.value = ''
      document.addEventListener('keydown', onKeydown)
      await nextTick()
      imgEl.value?.focus()
    } else {
      destroy()
    }
  },
)

onBeforeUnmount(destroy)

function destroy() {
  document.removeEventListener('keydown', onKeydown)
  cropper?.destroy()
  cropper = null
  cropperReady.value = false
  if (objectUrl) {
    URL.revokeObjectURL(objectUrl)
    objectUrl = ''
  }
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') onCancel()
}

function onImgLoad() {
  if (!imgEl.value) return
  // 参数恢复必须等 cropper 内部 ready（含 EXIF 方向检查）完成，
  // 否则 rotateTo/setData 会被忽略
  cropper = new Cropper(imgEl.value, {
    viewMode: 1,
    autoCropArea: 0.9,
    checkOrientation: true,
    ready: () => {
      if (!cropper) return
      const image = cropper.getImageData()
      naturalWidth = image.naturalWidth
      naturalHeight = image.naturalHeight
      cropperReady.value = true
      // 仅开发联调/验收：暴露 cropper 实例供像素级验证（生产构建不含此分支）
      if (import.meta.env.DEV) {
        ;(window as unknown as Record<string, unknown>).__editorCropper = cropper
      }
      const p = props.initialParams
      if (import.meta.env.DEV) {
        ;(window as unknown as Record<string, unknown>).__lastInitialParams = p
      }
      if (p && (p.rotate !== 0 || p.crop)) {
        // 单次 setData 同时应用旋转与裁剪框：getData/setData 在同一
        // （旋转后）坐标系上互逆；先 rotateTo 再 setData 会因画布
        // 重排导致裁剪框被二次调整，无法保真恢复
        cropper.setData({ ...(p.crop ?? {}), rotate: p.rotate })
        baseRotation = Math.round(p.rotate / 90) * 90
        fineAngle.value = Math.round(p.rotate - baseRotation)
      }
    },
  })
}

function rotateBy(degree: number) {
  if (!cropper) return
  baseRotation += degree
  cropper.rotateTo(baseRotation + fineAngle.value)
}

function onFineInput() {
  cropper?.rotateTo(baseRotation + fineAngle.value)
}

function zoomBy(ratio: number) {
  cropper?.zoom(ratio)
}

function resetAll() {
  if (!cropper) return
  cropper.reset()
  baseRotation = 0
  fineAngle.value = 0
}

function onCancel() {
  if (exporting.value) return
  emit('cancel')
}

/** 用 Cropper 导出真实像素：按原图分辨率渲染，再决定是否需要收边/压缩 */
async function exportResult(identity: boolean): Promise<EditorResult> {
  if (!cropper) throw new Error('编辑器未就绪')
  if (identity) {
    // 使用原图：复位变换后显式选取全图（reset 的默认框是 90% 区域而非全图）
    resetAll()
    cropper.setData({
      x: 0,
      y: 0,
      width: naturalWidth,
      height: naturalHeight,
      rotate: 0,
    })
  }

  const canvas = cropper.getCroppedCanvas({
    fillColor: '#ffffff',
    imageSmoothingQuality: 'high',
    maxWidth: MAX_EXPORT_EDGE,
    maxHeight: MAX_EXPORT_EDGE,
  })
  if (!canvas) throw new Error('图片导出失败')

  const params: EditParams = identity
    ? defaultEditParams()
    : readParams()
  let note = ''
  // 仅当理想导出尺寸超过画布安全上限（我们传入 maxWidth/maxHeight，
  // Cropper 会等比缩小）时提示，普通裁剪/旋转不触发
  const expected = expectedExportSize(naturalWidth, naturalHeight, params)
  if (Math.max(expected.width, expected.height) > MAX_EXPORT_EDGE) {
    note = `原图过大，已收边到 ${MAX_EXPORT_EDGE}px 以内导出。`
  }

  let width = canvas.width
  let height = canvas.height
  let blob = await canvasToJpeg(canvas)
  while (blob.size > MAX_IMAGE_BYTES) {
    const next = nextDownscale(width, height)
    if (!next) break
    width = next.width
    height = next.height
    const scaled = document.createElement('canvas')
    scaled.width = width
    scaled.height = height
    const ctx = scaled.getContext('2d')!
    ctx.fillStyle = '#ffffff'
    ctx.fillRect(0, 0, width, height)
    ctx.drawImage(canvas, 0, 0, width, height)
    blob = await canvasToJpeg(scaled)
    note = `图片较大，已自动缩小到 ${width}×${height} 以符合 10MB 限制。`
  }
  return { blob, width, height, params, note }
}

function readParams(): EditParams {
  const data = cropper!.getData(true)
  return {
    rotate: data.rotate,
    crop: { x: data.x, y: data.y, width: data.width, height: data.height },
  }
}

async function onConfirm() {
  if (exporting.value || !cropper) return
  exporting.value = true
  busyNote.value = ''
  try {
    const result = await exportResult(false)
    emit('confirm', result)
  } catch (err) {
    busyNote.value = err instanceof Error ? err.message : '图片导出失败'
  } finally {
    exporting.value = false
  }
}

async function onUseOriginal() {
  if (exporting.value || !cropper) return
  exporting.value = true
  busyNote.value = ''
  try {
    const result = await exportResult(true)
    emit('confirm', result)
  } catch (err) {
    busyNote.value = err instanceof Error ? err.message : '图片导出失败'
  } finally {
    exporting.value = false
  }
}
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="editor-overlay">
      <div class="editor-dialog" role="dialog" aria-modal="true" aria-label="编辑图片" tabindex="-1">
        <header class="editor-header">
          <h2>编辑图片</h2>
          <button class="icon-btn" type="button" aria-label="关闭" :disabled="exporting" @click="onCancel">
            <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M5 5l10 10M15 5L5 15" /></svg>
          </button>
        </header>

        <p class="editor-hint">
          拖动裁剪框与边角选择范围，拖动空白处移动画面，滚轮或按钮缩放。
        </p>

        <div class="crop-stage">
          <img
            ref="imgEl"
            :src="objectUrl"
            alt="待编辑图片"
            tabindex="0"
            @load="onImgLoad"
          />
        </div>

        <div class="toolbar">
          <button class="tool" type="button" title="向左旋转 90°" :disabled="!cropperReady || exporting" @click="rotateBy(-90)">
            <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 8a7 7 0 1 1 2 5" /><path d="M4 3v5h5" /></svg>
          </button>
          <button class="tool" type="button" title="向右旋转 90°" :disabled="!cropperReady || exporting" @click="rotateBy(90)">
            <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M16 8a7 7 0 1 0-2 5" /><path d="M16 3v5h-5" /></svg>
          </button>
          <label class="fine">
            <span>微调</span>
            <input
              v-model.number="fineAngle"
              type="range"
              min="-45"
              max="45"
              step="1"
              :disabled="!cropperReady || exporting"
              @input="onFineInput"
            />
            <span class="fine-val">{{ fineAngle }}°</span>
          </label>
          <span class="toolbar-divider" aria-hidden="true"></span>
          <button class="tool" type="button" title="放大" :disabled="!cropperReady || exporting" @click="zoomBy(0.1)">
            <svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="9" cy="9" r="5.5" /><path d="M13.5 13.5L17 17M9 6.8v4.4M6.8 9h4.4" /></svg>
          </button>
          <button class="tool" type="button" title="缩小" :disabled="!cropperReady || exporting" @click="zoomBy(-0.1)">
            <svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="9" cy="9" r="5.5" /><path d="M13.5 13.5L17 17M6.8 9h4.4" /></svg>
          </button>
          <button class="tool" type="button" title="重置" :disabled="!cropperReady || exporting" @click="resetAll">
            <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M16 10a6 6 0 1 1-1.8-4.3" /><path d="M15 2.5V6h-3.5" /></svg>
          </button>
        </div>

        <footer class="editor-footer">
          <span class="editor-note" :class="{ warn: !!busyNote }">{{ busyNote }}</span>
          <div class="editor-actions">
            <button class="btn ghost" type="button" :disabled="exporting" @click="onCancel">取消</button>
            <button class="btn ghost" type="button" :disabled="exporting" @click="onUseOriginal">使用原图</button>
            <button class="btn primary" type="button" :disabled="exporting" @click="onConfirm">
              {{ exporting ? '处理中…' : '确认' }}
            </button>
          </div>
        </footer>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.editor-overlay {
  position: fixed;
  inset: 0;
  z-index: 60;
  background: rgba(15, 23, 42, 0.55);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
}
.editor-dialog {
  background: #fff;
  border-radius: 14px;
  width: min(680px, 100%);
  max-height: min(92dvh, 760px);
  display: flex;
  flex-direction: column;
  box-shadow: 0 18px 48px rgba(15, 23, 42, 0.28);
  overflow: hidden;
}
.editor-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px 8px;
}
.editor-header h2 {
  margin: 0;
  font-size: 1rem;
  color: #1f2328;
}
.icon-btn {
  border: none;
  background: transparent;
  width: 30px;
  height: 30px;
  border-radius: 8px;
  cursor: pointer;
  color: #57606a;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.icon-btn:hover {
  background: #f0f3f7;
  color: #1f2328;
}
.icon-btn svg,
.tool svg {
  width: 18px;
  height: 18px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.7;
  stroke-linecap: round;
  stroke-linejoin: round;
}
.editor-hint {
  margin: 0 16px 8px;
  font-size: 0.82rem;
  color: #8b949e;
}
.crop-stage {
  margin: 0 16px;
  height: clamp(220px, 44vh, 420px);
  border: 1px solid #e1e4e8;
  border-radius: 10px;
  overflow: hidden;
  background:
    repeating-conic-gradient(#e9edf2 0% 25%, #f7f9fb 0% 50%)
    0 0 / 18px 18px;
}
.crop-stage img {
  display: block;
  max-width: 100%;
  outline: none;
}
/* 裁剪框与控制点在浅色棋盘上保持清晰 */
.crop-stage :deep(.cropper-view-box) {
  outline: 1.5px solid #1a73e8;
}
.crop-stage :deep(.cropper-line),
.crop-stage :deep(.cropper-point) {
  background-color: #1a73e8;
}
.crop-stage :deep(.cropper-point) {
  width: 7px;
  height: 7px;
  opacity: 0.9;
}
.toolbar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  padding: 10px 16px 4px;
}
.tool {
  border: 1px solid #d0d7de;
  background: #fff;
  color: #43505e;
  width: 34px;
  height: 34px;
  border-radius: 9px;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.tool:hover:not(:disabled) {
  border-color: #1a73e8;
  color: #1a73e8;
  background: #f2f7ff;
}
.tool:disabled {
  opacity: 0.45;
  cursor: default;
}
.tool svg {
  width: 17px;
  height: 17px;
}
.fine {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  flex: 1;
  min-width: 160px;
  max-width: 240px;
  padding: 0 6px;
  color: #57606a;
  font-size: 0.82rem;
}
.fine input[type='range'] {
  flex: 1;
  accent-color: #1a73e8;
}
.fine-val {
  min-width: 34px;
  text-align: right;
  font-variant-numeric: tabular-nums;
  color: #1f2328;
}
.toolbar-divider {
  width: 1px;
  height: 20px;
  background: #e1e4e8;
}
.editor-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
  padding: 10px 16px 14px;
}
.editor-note {
  font-size: 0.82rem;
  color: #8b949e;
  flex: 1;
  min-width: 120px;
}
.editor-note.warn {
  color: #9a6700;
}
.editor-actions {
  display: flex;
  gap: 8px;
}
.btn {
  border-radius: 9px;
  padding: 7px 14px;
  font-size: 0.88rem;
  cursor: pointer;
  white-space: nowrap;
}
.btn.ghost {
  border: 1px solid #d0d7de;
  background: #fff;
  color: #1f2328;
}
.btn.ghost:hover:not(:disabled) {
  border-color: #1a73e8;
  color: #1a73e8;
}
.btn.primary {
  border: none;
  background: #1a73e8;
  color: #fff;
}
.btn.primary:hover:not(:disabled) {
  background: #1765cc;
}
.btn:disabled {
  opacity: 0.5;
  cursor: default;
}
.btn:focus-visible,
.tool:focus-visible,
.icon-btn:focus-visible {
  outline: 2px solid #1a73e8;
  outline-offset: 1px;
}
@media (max-width: 560px) {
  .editor-overlay {
    padding: 8px;
  }
  .crop-stage {
    height: clamp(200px, 38vh, 340px);
  }
  .toolbar {
    gap: 5px;
    padding: 8px 12px 2px;
  }
  .tool {
    width: 38px;
    height: 38px;
  }
  .fine {
    min-width: 100%;
    max-width: none;
    order: 10;
  }
  .editor-footer {
    padding: 8px 12px 12px;
  }
  .editor-actions {
    flex: 1;
  }
  .editor-actions .btn {
    flex: 1;
    padding: 9px 0;
  }
}
</style>
