<script setup lang="ts">
import type {
  CameraOverlayData,
  OverlaySelection,
  Point,
} from '~/types/overlay'

type EditableKind = 'line' | 'polygon' | 'parking'
interface Handle {
  kind: EditableKind
  id: string
  index: number
  point: Point
}

const props = defineProps<{
  annotations: CameraOverlayData
  selection: OverlaySelection
  editable: boolean
  aspectRatio: number
}>()

const emit = defineEmits<{
  select: [selection: OverlaySelection]
  'update:annotations': [annotations: CameraOverlayData]
}>()

const svgElement = ref<SVGSVGElement | null>(null)
const dragging = ref<(Handle & { pointerId: number }) | null>(null)
const viewWidth = 1000
const viewHeight = computed(() => viewWidth / (props.aspectRatio > 0 ? props.aspectRatio : 16 / 9))

const handles = computed<Handle[]>(() => {
  const selected = props.selection
  if (!props.editable || !selected) return []

  if (selected.kind === 'line') {
    const line = props.annotations.lines.find((item) => item.id === selected.id)
    return line
      ? [
          { kind: 'line', id: line.id, index: 0, point: line.start },
          { kind: 'line', id: line.id, index: 1, point: line.end },
        ]
      : []
  }

  if (selected.kind === 'polygon' || selected.kind === 'parking') {
    const shapes = selected.kind === 'polygon'
      ? props.annotations.polygons
      : props.annotations.parkingZones
    const shape = shapes.find((item) => item.id === selected.id)
    return shape?.points.map((point, index) => ({
      kind: selected.kind as EditableKind,
      id: shape.id,
      index,
      point,
    })) ?? []
  }

  return []
})

function xCoord(value: number) {
  return Math.round(value * viewWidth)
}

function yCoord(value: number) {
  return Math.round(value * viewHeight.value)
}

function pointList(points: Point[]) {
  return points.map((point) => xCoord(point.x) + ',' + yCoord(point.y)).join(' ')
}

function labelX(points: Point[]) {
  return xCoord(points[0]?.x ?? 0) + 8
}

function labelY(points: Point[]) {
  return Math.max(28, yCoord(points[0]?.y ?? 0) - 12)
}

function isSelected(kind: NonNullable<OverlaySelection>['kind'], id: string) {
  return props.selection?.kind === kind && props.selection.id === id
}

function select(kind: NonNullable<OverlaySelection>['kind'], id: string) {
  emit('select', { kind, id })
}

function beginDrag(handle: Handle, event: PointerEvent) {
  if (!props.editable || !svgElement.value) return
  select(handle.kind, handle.id)
  dragging.value = { ...handle, pointerId: event.pointerId }
  svgElement.value.setPointerCapture(event.pointerId)
}

function onPointerMove(event: PointerEvent) {
  const drag = dragging.value
  const bounds = svgElement.value?.getBoundingClientRect()
  if (!drag || drag.pointerId !== event.pointerId || !bounds?.width || !bounds.height) return

  const point: Point = {
    x: Math.min(1, Math.max(0, (event.clientX - bounds.left) / bounds.width)),
    y: Math.min(1, Math.max(0, (event.clientY - bounds.top) / bounds.height)),
  }
  const current = props.annotations

  if (drag.kind === 'line') {
    emit('update:annotations', {
      ...current,
      lines: current.lines.map((line) => line.id === drag.id
        ? {
            ...line,
            start: drag.index === 0 ? point : line.start,
            end: drag.index === 1 ? point : line.end,
          }
        : line),
    })
    return
  }

  if (drag.kind === 'polygon') {
    emit('update:annotations', {
      ...current,
      polygons: current.polygons.map((polygon) => polygon.id === drag.id
        ? { ...polygon, points: polygon.points.map((old, index) => index === drag.index ? point : old) }
        : polygon),
    })
    return
  }

  emit('update:annotations', {
    ...current,
    parkingZones: current.parkingZones.map((zone) => zone.id === drag.id
      ? { ...zone, points: zone.points.map((old, index) => index === drag.index ? point : old) }
      : zone),
  })
}

function endDrag(event: PointerEvent) {
  if (dragging.value?.pointerId !== event.pointerId) return
  if (svgElement.value?.hasPointerCapture(event.pointerId)) {
    svgElement.value.releasePointerCapture(event.pointerId)
  }
  dragging.value = null
}
</script>

<template>
  <svg
    ref="svgElement"
    class="pointer-events-none select-none"
    :viewBox="'0 0 1000 ' + viewHeight"
    preserveAspectRatio="none"
    aria-label="카메라 오버레이"
    :style="{ touchAction: editable ? 'none' : 'auto' }"
    @pointermove="onPointerMove"
    @pointerup="endDrag"
    @pointercancel="endDrag"
  >
    <g
      v-for="zone in annotations.parkingZones"
      :key="'parking-' + zone.id"
      class="pointer-events-auto cursor-pointer"
      role="button"
      tabindex="0"
      :aria-label="(zone.label || zone.id) + (zone.occupied ? ' 주차 중' : ' 빈 자리')"
      @click.stop="select('parking', zone.id)"
      @keydown.enter.stop="select('parking', zone.id)"
      @keydown.space.prevent.stop="select('parking', zone.id)"
    >
      <polygon
        :points="pointList(zone.points)"
        :fill="zone.occupied ? '#f59e0b' : '#10b981'"
        fill-opacity="0.16"
        :stroke="isSelected('parking', zone.id) ? '#ffffff' : (zone.occupied ? '#fbbf24' : '#34d399')"
        stroke-width="3"
        vector-effect="non-scaling-stroke"
      />
      <text
        :x="labelX(zone.points)"
        :y="labelY(zone.points)"
        fill="white"
        font-size="26"
        font-weight="700"
        stroke="#111827"
        stroke-width="5"
        paint-order="stroke"
        class="pointer-events-none"
      >{{ zone.label || zone.id }} · {{ zone.occupied ? '주차 중' : '빈 자리' }}</text>
    </g>

    <g
      v-for="polygon in annotations.polygons"
      :key="'polygon-' + polygon.id"
      class="pointer-events-auto cursor-pointer"
      role="button"
      tabindex="0"
      :aria-label="polygon.label || polygon.id"
      @click.stop="select('polygon', polygon.id)"
      @keydown.enter.stop="select('polygon', polygon.id)"
      @keydown.space.prevent.stop="select('polygon', polygon.id)"
    >
      <polygon
        :points="pointList(polygon.points)"
        fill="#06b6d4"
        fill-opacity="0.12"
        :stroke="isSelected('polygon', polygon.id) ? '#ffffff' : '#67e8f9'"
        stroke-width="3"
        vector-effect="non-scaling-stroke"
      />
      <text
        v-if="polygon.label"
        :x="labelX(polygon.points)"
        :y="labelY(polygon.points)"
        fill="white"
        font-size="26"
        font-weight="700"
        stroke="#111827"
        stroke-width="5"
        paint-order="stroke"
        class="pointer-events-none"
      >{{ polygon.label }}</text>
    </g>

    <g
      v-for="line in annotations.lines"
      :key="'line-' + line.id"
      class="pointer-events-auto cursor-pointer"
      role="button"
      tabindex="0"
      :aria-label="line.label || line.id"
      @click.stop="select('line', line.id)"
      @keydown.enter.stop="select('line', line.id)"
      @keydown.space.prevent.stop="select('line', line.id)"
    >
      <line
        :x1="xCoord(line.start.x)"
        :y1="yCoord(line.start.y)"
        :x2="xCoord(line.end.x)"
        :y2="yCoord(line.end.y)"
        stroke="transparent"
        stroke-width="24"
        vector-effect="non-scaling-stroke"
      />
      <line
        :x1="xCoord(line.start.x)"
        :y1="yCoord(line.start.y)"
        :x2="xCoord(line.end.x)"
        :y2="yCoord(line.end.y)"
        :stroke="isSelected('line', line.id) ? '#ffffff' : '#fb7185'"
        stroke-width="3"
        stroke-dasharray="9 6"
        vector-effect="non-scaling-stroke"
        class="pointer-events-none"
      />
      <text
        v-if="line.label"
        :x="xCoord(line.start.x) + 8"
        :y="Math.max(28, yCoord(line.start.y) - 12)"
        fill="white"
        font-size="26"
        font-weight="700"
        stroke="#111827"
        stroke-width="5"
        paint-order="stroke"
        class="pointer-events-none"
      >{{ line.label }}</text>
    </g>

    <g
      v-for="person in annotations.persons"
      :key="'person-' + person.id"
      class="pointer-events-auto cursor-pointer"
      role="button"
      tabindex="0"
      :aria-label="'사람 트랙 ' + (person.trackId ?? '대기')"
      @click.stop="select('person', person.id)"
      @keydown.enter.stop="select('person', person.id)"
      @keydown.space.prevent.stop="select('person', person.id)"
    >
      <rect
        :x="xCoord(person.bbox.x)"
        :y="yCoord(person.bbox.y)"
        :width="xCoord(person.bbox.width)"
        :height="yCoord(person.bbox.height)"
        :stroke="isSelected('person', person.id) || person.highlighted ? '#facc15' : '#4ade80'"
        stroke-width="3"
        fill="#facc15"
        fill-opacity="0.06"
        vector-effect="non-scaling-stroke"
      />
      <text
        :x="xCoord(person.bbox.x) + 8"
        :y="Math.max(28, yCoord(person.bbox.y) - 12)"
        fill="white"
        font-size="28"
        font-weight="700"
        stroke="#111827"
        stroke-width="5"
        paint-order="stroke"
        class="pointer-events-none"
      >{{ person.trackId === null ? '추적 중' : 'ID ' + person.trackId }}</text>
    </g>

    <circle
      v-for="handle in handles"
      :key="'handle-' + handle.kind + '-' + handle.id + '-' + handle.index"
      class="pointer-events-auto cursor-move"
      :cx="xCoord(handle.point.x)"
      :cy="yCoord(handle.point.y)"
      r="12"
      fill="#ffffff"
      stroke="#0891b2"
      stroke-width="3"
      vector-effect="non-scaling-stroke"
      @pointerdown.stop.prevent="beginDrag(handle, $event)"
    />
  </svg>
</template>