<script setup lang="ts">
import type { VehicleBox } from '~/types/dataset'
import { DATASET_CLASSES } from '~/types/dataset'
import { clientToNormalized } from '~/lib/videoGeometry'
import { createDatasetBoxId } from '~/lib/datasetBoxId'
import { useVideoStage } from '~/composables/useVideoStage'
import { useI18n } from '~/composables/useI18n'

const props = defineProps<{
  imageUrl: string; width: number; height: number; boxes: VehicleBox[]
  selectedId: string | null; classId: number; tool: 'draw' | 'select'; zoom: number; disabled: boolean
}>()
const emit = defineEmits<{ update: [boxes: VehicleBox[]]; select: [id: string | null]; ready: [ready: boolean]; delete: [id: string] }>()
const { t } = useI18n()
const stage = ref<HTMLElement | null>(null)
const svg = ref<SVGSVGElement | null>(null)
const loaded = ref(false)
const loadError = ref(false)
const { contentStyle, content } = useVideoStage(stage, computed(() => ({ width: props.width, height: props.height })))
const handleSize = computed(() => 5 * props.width / Math.max(1, content.value.width))
const colors = ['var(--primary)', '#fbbf24', '#7dd3fc']
const cursor = ref<{ x: number; y: number } | null>(null)
const draft = ref<VehicleBox | null>(null)
let drag: { mode: string; start: { x: number; y: number }; box: VehicleBox; pointer: number } | null = null
const corners = ['nw', 'ne', 'sw', 'se']
const selected = computed(() => props.boxes.find(box => box.id === props.selectedId))

function point(event: PointerEvent) {
  const r = svg.value!.getBoundingClientRect()
  const p = clientToNormalized(event.clientX, event.clientY, { left: r.left, top: r.top, width: r.width, height: r.height })
  return { x: p.x * props.width, y: p.y * props.height }
}
function capture(event: PointerEvent) { svg.value?.setPointerCapture(event.pointerId); svg.value?.focus({ preventScroll: true }) }
function start(event: PointerEvent) {
  if (props.disabled || !loaded.value || event.button !== 0) return
  if (props.tool !== 'draw') { emit('select', null); return }
  const p = point(event)
  draft.value = { id: createDatasetBoxId(), class_id: props.classId, x: p.x, y: p.y, width: 0, height: 0, source: 'manual' }
  drag = { mode: 'draw', start: p, box: { ...draft.value }, pointer: event.pointerId }
  capture(event)
}
function grab(box: VehicleBox, mode: string, event: PointerEvent) {
  if (props.disabled || event.button !== 0) return
  if (props.tool === 'draw') { start(event); return }
  emit('select', box.id)
  drag = { mode, start: point(event), box: { ...box }, pointer: event.pointerId }
  capture(event)
}
function move(event: PointerEvent) {
  const p = point(event)
  cursor.value = event.pointerType === 'touch' ? null : p
  if (!drag || drag.pointer !== event.pointerId) return
  const original = drag.box
  let box = { ...original }
  if (drag.mode === 'draw') {
    box = { ...box, x: Math.min(drag.start.x, p.x), y: Math.min(drag.start.y, p.y), width: Math.abs(p.x - drag.start.x), height: Math.abs(p.y - drag.start.y) }
    draft.value = box
    return
  }
  if (drag.mode === 'move') {
    box.x = Math.min(props.width - box.width, Math.max(0, original.x + p.x - drag.start.x))
    box.y = Math.min(props.height - box.height, Math.max(0, original.y + p.y - drag.start.y))
  } else {
    let left = original.x, top = original.y, right = left + original.width, bottom = top + original.height
    if (drag.mode.includes('w')) left = Math.min(right - 2, p.x)
    if (drag.mode.includes('e')) right = Math.max(left + 2, p.x)
    if (drag.mode.includes('n')) top = Math.min(bottom - 2, p.y)
    if (drag.mode.includes('s')) bottom = Math.max(top + 2, p.y)
    box = { ...box, x: left, y: top, width: right - left, height: bottom - top }
  }
  emit('update', props.boxes.map(item => item.id === box.id ? box : item))
}
function end(event: PointerEvent) {
  if (!drag || drag.pointer !== event.pointerId) return
  if (drag.mode === 'draw' && draft.value && draft.value.width >= 2 && draft.value.height >= 2) {
    emit('update', [...props.boxes, draft.value]); emit('select', draft.value.id)
  }
  draft.value = null; drag = null
  if (svg.value?.hasPointerCapture(event.pointerId)) svg.value.releasePointerCapture(event.pointerId)
}
function cancel(event: PointerEvent) { draft.value = null; drag = null; if (svg.value?.hasPointerCapture(event.pointerId)) svg.value.releasePointerCapture(event.pointerId) }
function key(event: KeyboardEvent) {
  if (props.disabled) return
  if (event.key === 'Escape') { draft.value = null; drag = null; emit('select', null) }
  if ((event.key === 'Delete' || event.key === 'Backspace') && props.selectedId) {
    event.preventDefault(); emit('delete', props.selectedId)
  }
}
function imageLoaded() { loaded.value = true; loadError.value = false; emit('ready', true) }
</script>

<template>
  <div class="relative h-full min-h-0 basis-0 overflow-auto [scrollbar-gutter:stable] [overflow-anchor:none] rounded-xl bg-black/80 ring-1 ring-white/10" :aria-label="t('Bounding box canvas')">
    <div ref="stage" class="relative" :style="{ width: `${zoom * 100}%`, height: `${zoom * 100}%` }">
      <img :src="imageUrl" :alt="t('Clean captured frame for vehicle annotation')" class="absolute inset-0 h-full w-full object-contain" draggable="false" @load="imageLoaded" @error="loadError = true; loaded = false; emit('ready', false)">
      <svg v-if="loaded" ref="svg" class="absolute z-10 touch-none select-none outline-none" :class="tool === 'draw' ? 'cursor-crosshair' : ''" :style="{ ...contentStyle, pointerEvents: disabled ? 'none' : 'auto' }" :viewBox="`0 0 ${width} ${height}`" tabindex="0" role="group" :aria-label="t('Vehicle bounding boxes')" @pointerdown="start" @pointerenter="move" @pointerleave="cursor = null" @pointermove="move" @pointerup="end" @pointercancel="cancel" @keydown="key">
        <g v-if="cursor && tool === 'draw' && !disabled" class="pointer-events-none" stroke="white" stroke-opacity="0.55" stroke-width="1">
          <line :x1="cursor.x" y1="0" :x2="cursor.x" :y2="height" vector-effect="non-scaling-stroke" />
          <line x1="0" :y1="cursor.y" :x2="width" :y2="cursor.y" vector-effect="non-scaling-stroke" />
        </g>
        <g v-for="(box, index) in boxes" :key="box.id" :style="{ color: colors[box.class_id] }" @pointerdown.stop.prevent="grab(box, 'move', $event)">
          <rect :x="box.x" :y="box.y" :width="box.width" :height="box.height" fill="currentColor" :fill-opacity="box.id === selectedId ? 0.16 : 0.05" stroke="currentColor" :stroke-width="box.id === selectedId ? 2.5 : 1.5" vector-effect="non-scaling-stroke" :class="tool === 'select' ? 'cursor-move' : ''" />
          <text :x="box.x + handleSize" :y="box.y + handleSize * 3.5" :font-size="handleSize * 2.5" fill="white" stroke="#071116" :stroke-width="handleSize * 0.7" paint-order="stroke" class="pointer-events-none">{{ DATASET_CLASSES[box.class_id] }} {{ index + 1 }}</text>
        </g>
        <g v-if="selected && tool === 'select'">
          <rect v-for="corner in corners" :key="corner" :x="selected.x + (corner.includes('e') ? selected.width : 0) - handleSize" :y="selected.y + (corner.includes('s') ? selected.height : 0) - handleSize" :width="handleSize * 2" :height="handleSize * 2" rx="1" fill="white" stroke="var(--primary)" stroke-width="1.5" vector-effect="non-scaling-stroke" :style="{ cursor: corner === 'nw' || corner === 'se' ? 'nwse-resize' : 'nesw-resize' }" @pointerdown.stop.prevent="grab(selected, corner, $event)" />
        </g>
        <rect v-if="draft" :x="draft.x" :y="draft.y" :width="draft.width" :height="draft.height" fill="var(--primary)" fill-opacity="0.1" stroke="var(--primary)" stroke-width="2" stroke-dasharray="6 4" vector-effect="non-scaling-stroke" class="pointer-events-none" />
      </svg>
    </div>
    <p v-if="loadError" class="absolute inset-0 grid place-items-center p-6 text-center text-sm text-red-300" role="alert">{{ t('Could not load the original image. Reload to try again.') }}</p>
    <p v-else-if="!loaded" class="absolute inset-0 grid place-items-center text-sm text-muted-foreground" role="status">{{ t('Loading original image…') }}</p>
  </div>
</template>
