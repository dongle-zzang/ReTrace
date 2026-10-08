import type { Point } from '~/types/overlay'

/**
 * Parking model shared by the mock and the server REST adapter.
 *
 * A parking space is one real spot, identified by a unique label (A-01). A zone is that spot's
 * polygon in one camera; several cameras can each have a zone for the same space, so zones of
 * different cameras that share a `parkingSpaceId` are the same physical spot.
 *
 * Coordinates are normalized to the source video frame (x: 0 = left edge, 1 = right edge;
 * y: 0 = top, 1 = bottom), independent of how the frame is scaled or letterboxed on screen.
 *
 * Server REST (same origin, camelCase JSON):
 * - GET    /api/parking/spaces                  → ParkingSpaceDto[]
 * - POST   /api/parking/spaces                  ← { label }                              → 201 ParkingSpaceDto (409 if the label exists)
 * - GET    /api/parking/zones?cameraId={id}     → ParkingZoneDto[]
 * - POST   /api/parking/zones                   ← { cameraId, parkingSpaceId, polygon }  → 201 ParkingZoneDto
 * - PATCH  /api/parking/zones/{id}              ← { polygon? }  (space can't change: DELETE + POST) → ParkingZoneDto
 * - DELETE /api/parking/zones/{id}                                                        → 204
 * - GET    /api/parking/status                  → ParkingStatusDto
 * - POST   /api/parking/zones/{id}/calibrate    ← {} → ParkingZoneDto (calibrated: true); 409 no frame, 422 ROI too small
 * - DELETE /api/parking/zones/{id}/calibrate    → 204
 * /ws: `parking.status_updated` per cameraId with `data` = ParkingStatusDto for that camera's spaces
 * (upsert by parkingSpaceId; `spaces: []` after a zone is deleted → re-read REST).
 * Contract: server repo docs/color-parking.md.
 */

export type ParkingStatus = 'occupied' | 'empty' | 'unknown'

export interface ParkingSpace {
  id: string
  label: string
}

export interface ParkingZone {
  /** Server id, or a client `pz-…` id for a zone drawn in the editor and not saved yet. */
  id: string
  cameraId: string
  /** '' while a freshly drawn zone has no label yet (blocks saving). */
  parkingSpaceId: string
  /** Polygon vertices in drawing order, at least 3, normalized to the video frame. */
  polygon: Point[]
  /** Server polygon revision, when the server reports one; status for an older revision is ignored. */
  revision?: number
  updatedAt?: string | null
  /** Empty-spot baseline registered (server field). */
  calibrated?: boolean
}

export interface ParkingZoneUpdate {
  zones: ParkingZone[]
  /** The zones the edit started from; the server adapter diffs against these. */
  base: ParkingZone[]
}

/** Detection result of one zone (one camera's view of a space). */
export interface ParkingZoneState {
  zoneId: string
  cameraId: string
  parkingSpaceId: string | null
  status: ParkingStatus
  /** Detector confidence 0–1, or null when the server sends none. */
  score: number | null
  updatedAt: string | null
  revision?: number
}

/** Final status of a space as decided by the server, when it sends one. */
export interface ParkingSpaceVerdict {
  spaceId: string
  label?: string
  status: ParkingStatus
  conflict?: boolean
}

export interface ParkingStatusReport {
  zones: ParkingZoneState[]
  spaces: ParkingSpaceVerdict[]
}

/** A space's combined status across all cameras that see it. */
export interface ParkingSpaceSummary {
  space: ParkingSpace
  status: ParkingStatus
  /** Cameras that can see the spot disagree (one occupied, another empty). */
  conflict: boolean
  zones: ParkingZoneState[]
}

export interface ParkingCounts {
  total: number
  occupied: number
  empty: number
  unknown: number
}

// --- Server JSON ---------------------------------------------------------------------------

export interface ParkingSpaceDto {
  id: string
  label: string
}

export interface ParkingZoneDto {
  id: string
  cameraId: string
  parkingSpaceId: string
  polygon: Point[]
  revision?: number
  updatedAt?: string | null
  calibrated?: boolean
}

export interface ParkingZoneStatusDto {
  zoneId: string
  cameraId: string
  /** OCCUPIED | EMPTY | UNKNOWN (case-insensitive). */
  status: string
  score?: number | null
  updatedAt?: string | null
}

export interface ParkingSpaceStatusDto {
  parkingSpaceId: string
  label: string
  status: string
  conflict: boolean
  zones: ParkingZoneStatusDto[]
}

export interface ParkingStatusDto {
  spaces: ParkingSpaceStatusDto[]
}
