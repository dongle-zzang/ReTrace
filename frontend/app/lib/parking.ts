import type {
  ParkingCounts, ParkingSpace, ParkingSpaceSummary, ParkingSpaceVerdict, ParkingStatus, ParkingStatusReport,
  ParkingZone, ParkingZoneState,
} from '~/types/parking'
import { isSelfIntersecting, polygonArea } from '~/lib/videoGeometry'

/** Smallest accepted zone, as a fraction of the frame area (filters accidental clicks). */
export const MIN_ZONE_AREA = 0.0002
export const MAX_LABEL_LENGTH = 40

/** Only cameras whose display name mentions 주차장 get the parking editor entry. */
export function isParkingCamera(name: string | null | undefined) {
  return (name ?? '').includes('주차장')
}

export const parkingStatusLabel: Record<ParkingStatus, string> = {
  occupied: 'Occupied',
  empty: 'Empty',
  unknown: 'Unknown',
}

/** Accepts the server's OCCUPIED / EMPTY / UNKNOWN in any case; anything else is unknown. */
export function normalizeStatus(value: unknown): ParkingStatus {
  const status = typeof value === 'string' ? value.toLowerCase() : ''
  return status === 'occupied' || status === 'empty' ? status : 'unknown'
}

/** Comparison key for labels: ' a-01 ' and 'A-01' are the same space. */
export function labelKey(label: string) {
  return label.trim().replace(/\s+/g, ' ').toUpperCase()
}

export function findSpaceByLabel(spaces: ParkingSpace[], label: string) {
  const key = labelKey(label)
  return key ? spaces.find((space) => labelKey(space.label) === key) ?? null : null
}

export function validateLabel(label: string): string | null {
  const trimmed = label.trim()
  if (!trimmed) return 'Enter a label'
  if (trimmed.length > MAX_LABEL_LENGTH) return `Use at most ${MAX_LABEL_LENGTH} characters`
  return null
}

/** Labels sort naturally: A-2 before A-10. */
export function compareLabels(a: string, b: string) {
  return a.localeCompare(b, 'en', { numeric: true, sensitivity: 'base' })
}

/**
 * Whether a zone report may replace the one already shown. Reports for an older polygon revision
 * lose; on the same revision an older `updatedAt` (observed time) loses. Reports without revision
 * or time (mock) always win.
 */
export function isNewerState(current: ParkingZoneState | undefined, incoming: ParkingZoneState) {
  if (!current) return true
  if (current.revision !== undefined && incoming.revision !== undefined && incoming.revision !== current.revision) {
    return incoming.revision > current.revision
  }
  if (current.updatedAt && incoming.updatedAt) return Date.parse(incoming.updatedAt) >= Date.parse(current.updatedAt)
  return true
}

const isRecord = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object'

/**
 * Status payload from GET /api/parking/status or /ws `parking.status_updated`:
 * `{ spaces: [{ parkingSpaceId, label, status, conflict, zones: [{ zoneId, cameraId, status, score, updatedAt }] }] }`.
 * Malformed entries are skipped. Fields beyond that contract are not read.
 */
export function parseStatusReport(payload: unknown): ParkingStatusReport {
  const body = isRecord(payload) ? payload : {}
  const zones: ParkingZoneState[] = []
  const spaces: ParkingSpaceVerdict[] = []
  for (const item of Array.isArray(body.spaces) ? body.spaces as unknown[] : []) {
    if (!isRecord(item) || typeof item.parkingSpaceId !== 'string') continue
    spaces.push({
      spaceId: item.parkingSpaceId,
      label: typeof item.label === 'string' ? item.label : undefined,
      status: normalizeStatus(item.status),
      conflict: typeof item.conflict === 'boolean' ? item.conflict : undefined,
    })
    for (const zone of Array.isArray(item.zones) ? item.zones as unknown[] : []) {
      if (!isRecord(zone) || typeof zone.zoneId !== 'string' || typeof zone.cameraId !== 'string') continue
      const score = typeof zone.score === 'number' && Number.isFinite(zone.score) ? Math.min(1, Math.max(0, zone.score)) : null
      zones.push({
        zoneId: zone.zoneId,
        cameraId: zone.cameraId,
        parkingSpaceId: item.parkingSpaceId,
        status: normalizeStatus(zone.status),
        score,
        updatedAt: typeof zone.updatedAt === 'string' ? zone.updatedAt : null,
      })
    }
  }
  return { zones, spaces }
}

/** `parking.status_updated` from /ws (payload in `data`, or at the top level). Null for any other message. */
export function statusReportFromMessage(message: { type?: unknown; data?: unknown }) {
  if (message.type !== 'parking.status_updated') return null
  return parseStatusReport(message.data ?? message)
}

/**
 * One status for a space seen by several cameras. Unknown views are ignored. When the known views
 * disagree it is a conflict: the server's verdict wins if it sent one, otherwise the side with the
 * higher detection score (occupied on a tie or without scores, so a spot is never shown free by mistake).
 */
export function combineSpaceStatus(zones: ParkingZoneState[], verdict?: ParkingSpaceVerdict) {
  const known = zones.filter((zone) => zone.status !== 'unknown')
  const statuses = new Set(known.map((zone) => zone.status))
  const conflict = verdict?.conflict ?? statuses.size > 1
  if (verdict) return { status: verdict.status, conflict }
  if (!known.length) return { status: 'unknown' as ParkingStatus, conflict: false }
  if (statuses.size === 1) return { status: known[0]!.status, conflict: false }
  const best = (status: ParkingStatus) => Math.max(-1, ...known.filter((zone) => zone.status === status).map((zone) => zone.score ?? -1))
  return { status: (best('empty') > best('occupied') ? 'empty' : 'occupied') as ParkingStatus, conflict }
}

/** Summaries of spaces that have at least one zone, sorted by label. */
export function summarizeSpaces(
  spaces: ParkingSpace[],
  zoneStates: ParkingZoneState[],
  verdicts: Record<string, ParkingSpaceVerdict> = {},
): ParkingSpaceSummary[] {
  const bySpace = new Map<string, ParkingZoneState[]>()
  for (const state of zoneStates) {
    if (!state.parkingSpaceId) continue
    const list = bySpace.get(state.parkingSpaceId) ?? []
    list.push(state)
    bySpace.set(state.parkingSpaceId, list)
  }
  return spaces
    .filter((space) => bySpace.has(space.id))
    .map((space) => {
      const zones = bySpace.get(space.id)!.sort((a, b) => a.cameraId.localeCompare(b.cameraId))
      return { space, zones, ...combineSpaceStatus(zones, verdicts[space.id]) }
    })
    .sort((a, b) => compareLabels(a.space.label, b.space.label))
}

/** Items without a reported status count as unknown. */
export function countStatuses(ids: string[], statuses: Record<string, ParkingStatus>): ParkingCounts {
  const counts: ParkingCounts = { total: ids.length, occupied: 0, empty: 0, unknown: 0 }
  for (const id of ids) counts[statuses[id] ?? 'unknown']++
  return counts
}

export interface ZoneProblem {
  zoneId: string
  message: string
}

export function validatePolygon(polygon: ParkingZone['polygon']): string | null {
  if (polygon.length < 3) return 'Needs at least 3 points'
  if (polygon.some((point) => !(point.x >= 0 && point.x <= 1 && point.y >= 0 && point.y <= 1))) {
    return 'A point is outside the video'
  }
  if (isSelfIntersecting(polygon)) return 'Edges cross each other'
  if (polygonArea(polygon) < MIN_ZONE_AREA) return 'Area is too small'
  return null
}

/**
 * Problems that block saving one camera's zones, at most one per zone. Every zone needs a label,
 * and one camera may not draw the same space twice (other cameras may, that is the point).
 */
export function validateZones(zones: ParkingZone[], knownSpaceIds?: Set<string>): ZoneProblem[] {
  const uses = new Map<string, number>()
  for (const zone of zones) if (zone.parkingSpaceId) uses.set(zone.parkingSpaceId, (uses.get(zone.parkingSpaceId) ?? 0) + 1)
  const problems: ZoneProblem[] = []
  for (const zone of zones) {
    const message = !zone.parkingSpaceId
      ? 'Choose a label'
      : knownSpaceIds && !knownSpaceIds.has(zone.parkingSpaceId)
        ? 'Label no longer exists'
        : (uses.get(zone.parkingSpaceId) ?? 0) > 1
            ? 'Label used twice on this camera'
            : validatePolygon(zone.polygon)
    if (message) problems.push({ zoneId: zone.id, message })
  }
  return problems
}

// crypto.randomUUID needs a secure context; the dashboard is also served over plain-HTTP LAN.
export function randomId() {
  return globalThis.crypto?.randomUUID?.() ?? Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10)
}

/** Client id for a zone drawn in the editor; never sent to the server. */
export function createZoneId() {
  return 'pz-' + randomId()
}

export function isClientZoneId(id: string) {
  return id.startsWith('pz-')
}

