<script setup lang="ts">
import type { Point } from '~/types/overlay'
import type { ParkingStatus, ParkingZone } from '~/types/parking'
import { parkingStatusLabel } from '~/lib/parking'
import { clientToNormalized, distance, normalizedToPixels, polygonCentroid } from '~/lib/videoGeometry'

/**
 * Parking zones over the video content. Place it exactly on the content rect (see
 * useVideoStage().contentStyle) and pass that rect's pixel size; the viewBox uses the same
 * pixels so strokes, handles and labels keep their screen size at any scale.
 * Each zone shows its label and status as text, not only as a color.
 */
const props = withDefaults(defineProps<{
  zones: ParkingZone[]
  /** parkingSpaceId → label */
  spaceLabels: Record<string, string>
  /** zoneId → status */
  statuses: Record<string, ParkingStatus>
  /** parkingSpaceIds whose cameras disagree; their zones get a warning mark. */
  conflictSpaceIds?: string[]
  width: number
  height: number
  editing?: boolean
  selectedId?: string | null
  /** Vertices of the polygon being drawn, or null when not drawing. */
  drawing?: Point[] | null
  invalidIds?: string[]
  labels?: boolean
}>(), {
  editing: false,
  selectedId: null,
  drawing: null,
  invalidIds: () => [],
  conflictSpaceIds: () => [],
  labels: true,
})

const emit = defineEmits<{
  select: [zoneId: string | null]
  'add-point': [point: Point]
  'close-drawing': []
  'move-vertex': [zoneId: string, index: number, point: Point]
  'insert-vertex': [zoneId: string, index: number, point: Point]
  'remove-vertex': [zoneId: string, index: number]
}>()

const CLOSE_RADIUS_PX = 12

const svgElement = ref<SVGSVGElement | null>(null)
const dragging = ref<{ zoneId: string; index: number; pointerId: number } | null>(null)
const hover = ref<Point | null>(null)

const size = computed(() => ({ width: Math.max(1, props.width), height: Math.max(1, props.height) }))
const isDrawing = computed(() => props.editing && !!props.drawing)
const showLabels = computed(() => props.labels && props.width >= 320)
const showStatusText = computed(() => props.labels && props.width >= 440)

const palette: Record<ParkingStatus, { fill: string; stroke: string }> = {
  occupied: { fill: '#ef4444', stroke: '#f87171' },
  empty: { fill: '#22c55e', stroke: '#4ade80' },
  unknown: { fill: '#9ca3af', stroke: '#a1a1aa' },
}

function px(point: Point) {
  return normalizedToPixels(point, size.value)
}

function pointList(points: Point[]) {
  return points.map((point) => {
    const { x, y } = px(point)
    return x.toFixed(1) + ',' + y.toFixed(1)
  }).join(' ')
}

const shapes = computed(() => props.zones.map((zone) => {
  const status = props.statuses[zone.id] ?? 'unknown'
  return {
    zone,
    status,
    name: props.spaceLabels[zone.parkingSpaceId] ?? (zone.parkingSpaceId ? '?' : 'No label'),
    points: pointList(zone.polygon),
    label: px(polygonCentroid(zone.polygon)),
    selected: zone.id === props.selectedId,
    invalid: props.invalidIds.includes(zone.id),
    conflict: !!zone.parkingSpaceId && props.conflictSpaceIds.includes(zone.parkingSpaceId),
  }
}))

const selectedZone = computed(() =>
  props.editing && !isDrawing.value ? props.zones.find((zone) => zone.id === props.selectedId) ?? null : null,
)

const midpoints = computed(() => {
  const polygon = selectedZone.value?.polygon ?? []
  return polygon.map((point, index) => {
    const next = polygon[(index + 1) % polygon.length]!
    return { index: index + 1, point: { x: (point.x + next.x) / 2, y: (point.y + next.y) / 2 } }
  })
})

const canClose = computed(() => (props.drawing?.length ?? 0) >= 3)

function toNormalized(event: PointerEvent | MouseEvent) {
  const bounds = svgElement.value?.getBoundingClientRect()
  return bounds ? clientToNormalized(event.clientX, event.clientY, bounds) : null
}

function onBackgroundPointerDown(event: PointerEvent) {
  if (!props.editing || event.button !== 0) return
  const point = toNormalized(event)
  if (!point) return
  if (!props.drawing) {
    emit('select', null)
    return
  }
  const first = props.drawing[0]
  if (first && canClose.value && distance(px(first), px(point)) <= CLOSE_RADIUS_PX) emit('close-drawing')
  else emit('add-point', point)
}

function onZonePointerDown(zoneId: string, event: PointerEvent) {
  if (!props.editing || isDrawing.value || event.button !== 0) return
  event.stopPropagation()
  emit('select', zoneId)
}

function beginDrag(zoneId: string, index: number, event: PointerEvent) {
  if (event.button !== 0 || !svgElement.value) return
  dragging.value = { zoneId, index, pointerId: event.pointerId }
  svgElement.value.setPointerCapture(event.pointerId)
}

function onMidpointPointerDown(zoneId: string, index: number, point: Point, event: PointerEvent) {
  if (event.button !== 0) return
  emit('insert-vertex', zoneId, index, point)
  beginDrag(zoneId, index, event)
}

function onPointerMove(event: PointerEvent) {
  const drag = dragging.value
  if (drag && drag.pointerId === event.pointerId) {
    const point = toNormalized(event)
    if (point) emit('move-vertex', drag.zoneId, drag.index, point)
    return
  }
  hover.value = isDrawing.value ? toNormalized(event) : null
}

function endDrag(event: PointerEvent) {
  if (dragging.value?.pointerId !== event.pointerId) return
  if (svgElement.value?.hasPointerCapture(event.pointerId)) svgElement.value.releasePointerCapture(event.pointerId)
  dragging.value = null
}

watch(isDrawing, (drawing) => {
  if (!drawing) hover.value = null
})
</script>

<template>
  <svg
    ref="svgElement"
    class="select-none overflow-visible"
    :class="[
      editing ? 'pointer-events-auto' : 'pointer-events-none',
      isDrawing ? 'cursor-crosshair' : '',
    ]"
    :viewBox="`0 0 ${size.width} ${size.height}`"
    preserveAspectRatio="none"
    role="group"
    aria-label="Parking zones"
    :style="{ touchAction: editing ? 'none' : 'auto' }"
    @pointerdown="onBackgroundPointerDown"
    @pointermove="onPointerMove"
    @pointerleave="hover = null"
    @pointerup="endDrag"
    @pointercancel="endDrag"
    @contextmenu="editing && $event.preventDefault()"
  >
    <g
      v-for="shape in shapes"
      :key="shape.zone.id"
      :class="editing && !isDrawing ? 'cursor-pointer' : 'pointer-events-none'"
      :role="editing ? 'button' : 'img'"
      :tabindex="editing && !isDrawing ? 0 : undefined"
      :aria-label="shape.name + ' ' + parkingStatusLabel[shape.status] + (shape.conflict ? ', cameras disagree' : '')"
      :aria-pressed="editing ? shape.selected : undefined"
      @pointerdown="onZonePointerDown(shape.zone.id, $event)"
      @keydown.enter.prevent="editing && emit('select', shape.zone.id)"
      @keydown.space.prevent="editing && emit('select', shape.zone.id)"
    >
      <!-- The selected zone is outlined in the theme accent (--primary), apart from the status colors. -->
      <polygon
        :points="shape.points"
        :fill="palette[shape.status].fill"
        :fill-opacity="shape.selected ? 0.34 : 0.2"
        :stroke="shape.invalid ? '#facc15' : shape.selected ? undefined : palette[shape.status].stroke"
        :class="['transition-[fill,stroke,fill-opacity] duration-500', shape.selected && !shape.invalid ? 'stroke-primary' : '']"
        :stroke-width="shape.selected ? 2.5 : 1.75"
        :stroke-dasharray="shape.invalid ? '6 4' : undefined"
        stroke-linejoin="round"
      />
      <text
        v-if="showLabels"
        :x="shape.label.x"
        :y="shape.label.y"
        text-anchor="middle"
        fill="white"
        stroke="rgba(0,0,0,0.7)"
        stroke-width="3"
        paint-order="stroke"
        class="pointer-events-none"
      >
        <tspan :x="shape.label.x" :dy="showStatusText ? '-0.15em' : '0.35em'" font-size="12" font-weight="700">{{ shape.conflict ? '⚠ ' : '' }}{{ shape.name }}</tspan>
        <tspan v-if="showStatusText" :x="shape.label.x" dy="1.2em" font-size="10" font-weight="500" fill-opacity="0.85">{{ parkingStatusLabel[shape.status] }}</tspan>
      </text>
    </g>

    <g v-if="selectedZone">
      <circle
        v-for="mid in midpoints"
        :key="'mid-' + mid.index"
        :cx="px(mid.point).x"
        :cy="px(mid.point).y"
        r="4.5"
        fill="#ffffff"
        fill-opacity="0.45"
        stroke="#0f172a"
        stroke-width="1"
        class="cursor-copy"
        @pointerdown.stop.prevent="onMidpointPointerDown(selectedZone.id, mid.index, mid.point, $event)"
      >
        <title>Drag to add a point</title>
      </circle>
      <circle
        v-for="(point, index) in selectedZone.polygon"
        :key="'vertex-' + index"
        :cx="px(point).x"
        :cy="px(point).y"
        r="7"
        fill="#ffffff"
        stroke="#0f172a"
        stroke-width="2"
        class="cursor-move"
        @pointerdown.stop.prevent="beginDrag(selectedZone.id, index, $event)"
        @dblclick.stop="emit('remove-vertex', selectedZone.id, index)"
        @contextmenu.stop.prevent="emit('remove-vertex', selectedZone.id, index)"
      >
        <title>Drag to move · double-click or right-click to delete</title>
      </circle>
    </g>

    <g v-if="isDrawing && drawing" class="pointer-events-none">
      <polygon
        v-if="drawing.length >= 3"
        :points="pointList(hover ? [...drawing, hover] : drawing)"
        class="fill-primary"
        fill-opacity="0.15"
        stroke="none"
      />
      <polyline
        :points="pointList(hover ? [...drawing, hover] : drawing)"
        fill="none"
        class="stroke-primary"
        stroke-width="2"
        stroke-dasharray="6 4"
        stroke-linejoin="round"
      />
      <circle
        v-for="(point, index) in drawing"
        :key="'draw-' + index"
        :cx="px(point).x"
        :cy="px(point).y"
        :r="index === 0 && canClose ? CLOSE_RADIUS_PX - 3 : 5"
        :fill="index === 0 && canClose ? undefined : '#ffffff'"
        :class="index === 0 && canClose ? 'fill-primary' : ''"
        :fill-opacity="index === 0 && canClose ? 0.35 : 1"
        stroke="#0f172a"
        stroke-width="2"
      />
    </g>
  </svg>
</template>
