import type { Ref } from 'vue'
import type { Point } from '~/types/overlay'
import type { ParkingSpace, ParkingZone, ParkingZoneUpdate } from '~/types/parking'
import { createZoneId, validatePolygon, validateZones } from '~/lib/parking'

/**
 * Edit session for one camera's parking zones. Changes go to a draft copy; `payload()` is saved
 * as a whole on Done, and `end()` discards the draft on Cancel. Server status and zone reloads
 * never touch the draft.
 */
export function useParkingEditor(cameraId: Ref<string>, spaces: Ref<ParkingSpace[]>) {
  const draft = ref<ParkingZone[] | null>(null)
  const selectedId = ref<string | null>(null)
  const drawing = ref<Point[] | null>(null)
  const notice = ref<string | null>(null)
  let original = '[]'

  const editing = computed(() => draft.value !== null)
  const problems = computed(() => (draft.value ? validateZones(draft.value, new Set(spaces.value.map((space) => space.id))) : []))
  const dirty = computed(() =>
    !!draft.value && (JSON.stringify(draft.value) !== original || !!drawing.value?.length),
  )

  function update(zoneId: string, change: (zone: ParkingZone) => ParkingZone) {
    if (!draft.value) return
    draft.value = draft.value.map((zone) => (zone.id === zoneId ? change(zone) : zone))
  }

  function begin(zones: ParkingZone[] | undefined) {
    const copy = JSON.parse(JSON.stringify(zones ?? [])) as ParkingZone[]
    original = JSON.stringify(copy)
    draft.value = copy
    selectedId.value = null
    drawing.value = null
    notice.value = null
  }

  function end() {
    draft.value = null
    selectedId.value = null
    drawing.value = null
    notice.value = null
  }

  function select(zoneId: string | null) {
    if (drawing.value) return
    selectedId.value = zoneId
  }

  function startDrawing() {
    if (!draft.value) return
    selectedId.value = null
    drawing.value = []
    notice.value = null
  }

  function addPoint(point: Point) {
    if (!drawing.value) return
    drawing.value = [...drawing.value, point]
    notice.value = null
  }

  function undoPoint() {
    if (drawing.value?.length) drawing.value = drawing.value.slice(0, -1)
  }

  function cancelDrawing() {
    drawing.value = null
    notice.value = null
  }

  /**
   * Turns the drawn vertices into a new zone with the given label ('' = pick one next) and selects
   * it. Returns false (with a notice) when the shape is invalid.
   */
  function finishDrawing(parkingSpaceId = '') {
    if (!draft.value || !drawing.value) return false
    const problem = validatePolygon(drawing.value)
    if (problem) {
      notice.value = problem
      return false
    }
    const zone: ParkingZone = { id: createZoneId(), cameraId: cameraId.value, parkingSpaceId, polygon: drawing.value }
    draft.value = [...draft.value, zone]
    drawing.value = null
    notice.value = null
    selectedId.value = zone.id
    return true
  }

  function moveVertex(zoneId: string, index: number, point: Point) {
    update(zoneId, (zone) => ({
      ...zone,
      polygon: zone.polygon.map((old, position) => (position === index ? point : old)),
    }))
  }

  function insertVertex(zoneId: string, index: number, point: Point) {
    update(zoneId, (zone) => ({
      ...zone,
      polygon: [...zone.polygon.slice(0, index), point, ...zone.polygon.slice(index)],
    }))
  }

  function removeVertex(zoneId: string, index: number) {
    const zone = draft.value?.find((item) => item.id === zoneId)
    if (!zone) return
    if (zone.polygon.length <= 3) {
      notice.value = 'A zone needs at least 3 points'
      return
    }
    update(zoneId, (item) => ({ ...item, polygon: item.polygon.filter((_, position) => position !== index) }))
  }

  function assign(zoneId: string, parkingSpaceId: string) {
    update(zoneId, (zone) => ({ ...zone, parkingSpaceId }))
  }

  function remove(zoneId: string) {
    if (!draft.value) return
    draft.value = draft.value.filter((zone) => zone.id !== zoneId)
    if (selectedId.value === zoneId) selectedId.value = null
  }

  function payload(): ParkingZoneUpdate {
    return {
      zones: draft.value ?? [],
      base: JSON.parse(original) as ParkingZone[],
    }
  }

  return {
    draft, selectedId, drawing, notice, editing, problems, dirty,
    begin, end, select, startDrawing, addPoint, undoPoint, cancelDrawing, finishDrawing,
    moveVertex, insertVertex, removeVertex, assign, remove, payload,
  }
}
