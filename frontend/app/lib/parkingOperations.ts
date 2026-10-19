import type { ParkingSpaceSummary, ParkingZone } from '~/types/parking'
import { labelKey } from '~/lib/parking'

export type SpaceFilter = 'all' | 'occupied' | 'empty' | 'unknown' | 'check' | 'unused' | 'uncalibrated'
export function filterParkingSpaces(rows: ParkingSpaceSummary[], query: string, filter: SpaceFilter, zones: ParkingZone[]) {
  const key = labelKey(query)
  return rows.filter(row => labelKey(row.space.label).includes(key) && (
    filter === 'all' || filter === row.status || filter === 'check' && row.conflict
    || filter === 'unused' && !row.zones.length
    || filter === 'uncalibrated' && zones.some(zone => row.spaceIds.includes(zone.parkingSpaceId) && zone.calibrated === false)
  ))
}
export function unknownReason(row: ParkingSpaceSummary, disconnected: boolean, delayed: boolean, offlineCameraIds: Set<string>, detectorOffCameraIds: Set<string> = new Set()): string | null {
  if (row.status !== 'unknown') return null
  if (!row.zones.length) return 'No camera zone assigned'
  if (disconnected) return 'Server disconnected'
  if (row.zones.every(zone => offlineCameraIds.has(zone.cameraId))) return 'Camera offline or unavailable'
  if (row.zones.every(zone => detectorOffCameraIds.has(zone.cameraId))) return 'Vehicle detection is off for this camera'
  if (delayed) return 'Waiting for fresh status'
  return 'Waiting for a stable reading'
}

export const PARKING_REPORT_TTL_MS = 15_000
export function isParkingReportFresh(receivedAt: number | undefined, now: number) {
  return receivedAt !== undefined && now - receivedAt >= 0 && now - receivedAt <= PARKING_REPORT_TTL_MS
}
