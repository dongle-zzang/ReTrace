import type { Point } from '~/types/overlay'
import type { ParkingStatus, ParkingZone, ParkingZoneState } from '~/types/parking'
import { polygonArea } from '~/lib/videoGeometry'

/**
 * Vehicle-detection occupancy, decided in the browser from the server's latest vehicle boxes
 * (GET /api/cameras/{id}/vehicles) and the existing parking zones (/api/parking/zones).
 *
 * A zone is occupied when a detected vehicle's ground footprint (the bottom strip of its box)
 * mostly lies inside the zone polygon. The footprint rule and default thresholds follow the
 * server's vehicle occupancy policy (root docs/parking-occupancy.md, "좌표와 판정").
 * A status only changes after the same reading on several consecutive new frames.
 */

export type OccupancyMode = 'detection' | 'color'
export const OCCUPANCY_MODES: readonly OccupancyMode[] = ['detection', 'color']

export function normalizeOccupancyMode(value: unknown, fallback: OccupancyMode = 'detection'): OccupancyMode {
  const mode = String(value ?? '').trim().toLowerCase()
  return (OCCUPANCY_MODES as readonly string[]).includes(mode) ? mode as OccupancyMode : fallback
}

export interface DetectedVehicle {
  trackId: string | null
  /** Normalized to the video frame (0–1), clipped to it. */
  bbox: { x: number; y: number; width: number; height: number }
  confidence: number | null
}

/** One camera's latest vehicle frame. */
export interface VehicleFrame {
  cameraId: string
  /** Frame time as sent by the server; null when the camera has no live frame. */
  timestamp: string | null
  detectionEnabled: boolean
  inferenceDone: boolean
  vehicles: DetectedVehicle[]
}

export interface OccupancyPolicy {
  /** Bottom part of the box used as the ground footprint (fraction of its height). */
  footprintHeight: number
  /** Left and right part of the box left out of the footprint (fraction of its width, per side). */
  horizontalInset: number
  /** Footprint overlap needed to become occupied / to stay occupied. */
  enterOverlap: number
  exitOverlap: number
  /** Overlap must also cover at least this part of the zone. */
  minSpaceCoverage: number
  /**
   * Vehicles below this confidence (or without one, i.e. tracker-only) never make a zone occupied,
   * and keep it from turning empty.
   */
  minConfidence: number
  /** The footprint's bottom-centre point must lie inside the zone. */
  requireAnchor: boolean
  /** Consecutive new frames with the same reading before the status changes. */
  occupiedFrames: number
  emptyFrames: number
}

export const DEFAULT_OCCUPANCY_POLICY: OccupancyPolicy = {
  footprintHeight: 0.25,
  horizontalInset: 0.1,
  enterOverlap: 0.55,
  exitOverlap: 0.35,
  minSpaceCoverage: 0.1,
  minConfidence: 0.4,
  requireAnchor: true,
  occupiedFrames: 2,
  emptyFrames: 3,
}

const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value)
const clip = (value: number) => Math.min(1, Math.max(0, value))

/**
 * Parses GET /api/cameras/{id}/vehicles: `{ timestamp, bbox_width, bbox_height, vehicle_detection_enabled,
 * vehicle_inference_done, vehicles: [{ track_id, confidence, bbox: { x, y, width, height } }] }` with pixel boxes.
 */
export function parseVehicleFrame(cameraId: string, payload: unknown): VehicleFrame {
  const body = (payload ?? {}) as Record<string, unknown>
  const width = body.bbox_width, height = body.bbox_height
  const timestamp = typeof body.timestamp === 'string' ? body.timestamp : null
  const vehicles: DetectedVehicle[] = []
  if (finite(width) && finite(height) && width > 0 && height > 0 && Array.isArray(body.vehicles)) {
    for (const item of body.vehicles as Record<string, unknown>[]) {
      const box = (item?.bbox ?? null) as Record<string, unknown> | null
      if (!box || !finite(box.x) || !finite(box.y) || !finite(box.width) || !finite(box.height)) continue
      const x0 = clip(box.x / width), y0 = clip(box.y / height)
      const x1 = clip((box.x + box.width) / width), y1 = clip((box.y + box.height) / height)
      if (x1 <= x0 || y1 <= y0) continue
      vehicles.push({
        trackId: item.track_id === null || item.track_id === undefined ? null : String(item.track_id),
        bbox: { x: x0, y: y0, width: x1 - x0, height: y1 - y0 },
        confidence: finite(item.confidence) ? item.confidence : null,
      })
    }
  }
  return {
    cameraId,
    timestamp,
    detectionEnabled: body.vehicle_detection_enabled === true,
    inferenceDone: body.vehicle_inference_done === true,
    vehicles,
  }
}

/** Ground footprint of a vehicle box: its bottom strip, without the sides. */
export function vehicleFootprint(bbox: DetectedVehicle['bbox'], policy: OccupancyPolicy = DEFAULT_OCCUPANCY_POLICY) {
  const inset = bbox.width * policy.horizontalInset
  const height = bbox.height * policy.footprintHeight
  return { x0: bbox.x + inset, x1: bbox.x + bbox.width - inset, y0: bbox.y + bbox.height - height, y1: bbox.y + bbox.height }
}

type Rect = ReturnType<typeof vehicleFootprint>

/** Part of a (possibly concave) polygon inside an axis-aligned rectangle (Sutherland–Hodgman). */
export function clipPolygonToRect(polygon: Point[], rect: Rect): Point[] {
  const edges: [(p: Point) => boolean, (a: Point, b: Point) => Point][] = [
    [p => p.x >= rect.x0, (a, b) => ({ x: rect.x0, y: a.y + (b.y - a.y) * (rect.x0 - a.x) / (b.x - a.x) })],
    [p => p.x <= rect.x1, (a, b) => ({ x: rect.x1, y: a.y + (b.y - a.y) * (rect.x1 - a.x) / (b.x - a.x) })],
    [p => p.y >= rect.y0, (a, b) => ({ x: a.x + (b.x - a.x) * (rect.y0 - a.y) / (b.y - a.y), y: rect.y0 })],
    [p => p.y <= rect.y1, (a, b) => ({ x: a.x + (b.x - a.x) * (rect.y1 - a.y) / (b.y - a.y), y: rect.y1 })],
  ]
  let output = polygon
  for (const [inside, cross] of edges) {
    const input = output
    output = []
    for (let index = 0; index < input.length; index++) {
      const current = input[index]!
      const previous = input[(index + input.length - 1) % input.length]!
      if (inside(current)) {
        if (!inside(previous)) output.push(cross(previous, current))
        output.push(current)
      } else if (inside(previous)) {
        output.push(cross(previous, current))
      }
    }
    if (!output.length) break
  }
  return output
}

export function pointInPolygon(point: Point, polygon: Point[]) {
  let inside = false
  for (let index = 0, previous = polygon.length - 1; index < polygon.length; previous = index++) {
    const a = polygon[index]!, b = polygon[previous]!
    if ((a.y > point.y) !== (b.y > point.y) && point.x < (b.x - a.x) * (point.y - a.y) / (b.y - a.y) + a.x) inside = !inside
  }
  return inside
}

export interface VehicleZoneMatch {
  /** Intersection area / footprint area. */
  overlap: number
  /** Intersection area / zone area. */
  coverage: number
  anchored: boolean
}

export function matchVehicleToZone(vehicle: DetectedVehicle, polygon: Point[], policy: OccupancyPolicy = DEFAULT_OCCUPANCY_POLICY): VehicleZoneMatch {
  const footprint = vehicleFootprint(vehicle.bbox, policy)
  const footprintArea = (footprint.x1 - footprint.x0) * (footprint.y1 - footprint.y0)
  const zoneArea = polygonArea(polygon)
  if (footprintArea <= 0 || zoneArea <= 0) return { overlap: 0, coverage: 0, anchored: false }
  const intersection = polygonArea(clipPolygonToRect(polygon, footprint))
  return {
    overlap: Math.min(1, intersection / footprintArea),
    coverage: Math.min(1, intersection / zoneArea),
    anchored: pointInPolygon({ x: (footprint.x0 + footprint.x1) / 2, y: footprint.y1 }, polygon),
  }
}

/** What one frame says about one zone, before confirmation over several frames. */
export interface ZoneReading {
  /** Best footprint overlap of a confident vehicle assigned to the zone (0 when none). */
  overlap: number
  /** A low-confidence vehicle overlaps the zone: not proof of either state. */
  uncertain: boolean
}

/**
 * Reads one frame against one camera's zones. Each vehicle counts for at most one zone (the one
 * its footprint overlaps most), so a car straddling a line doesn't fill both spots.
 */
export function readZones(frame: VehicleFrame, zones: ParkingZone[], policy: OccupancyPolicy = DEFAULT_OCCUPANCY_POLICY) {
  const readings = new Map<string, ZoneReading>(zones.map(zone => [zone.id, { overlap: 0, uncertain: false }]))
  for (const vehicle of frame.vehicles) {
    let best: { zoneId: string; overlap: number } | null = null
    for (const zone of zones) {
      if (zone.polygon.length < 3) continue
      const match = matchVehicleToZone(vehicle, zone.polygon, policy)
      if (policy.requireAnchor && !match.anchored) continue
      if (match.coverage < policy.minSpaceCoverage || match.overlap < policy.exitOverlap) continue
      if (!best || match.overlap > best.overlap) best = { zoneId: zone.id, overlap: match.overlap }
    }
    if (!best) continue
    const reading = readings.get(best.zoneId)!
    if (vehicle.confidence === null || vehicle.confidence < policy.minConfidence) reading.uncertain = true
    else reading.overlap = Math.max(reading.overlap, best.overlap)
  }
  return readings
}

interface ZoneMemory {
  revision?: number
  status: ParkingStatus
  candidate: ParkingStatus | null
  count: number
}

/**
 * Keeps per-zone confirmation state across frames. `observe` takes each camera's latest frame;
 * a frame already seen (same timestamp) doesn't advance the confirmation count.
 */
export function createVehicleOccupancy(policy: OccupancyPolicy = DEFAULT_OCCUPANCY_POLICY) {
  const memory = new Map<string, ZoneMemory>()
  const lastFrame = new Map<string, string>()

  function observe(frame: VehicleFrame, zones: ParkingZone[]): ParkingZoneState[] {
    const usable = frame.detectionEnabled && frame.inferenceDone && frame.timestamp !== null
    const repeated = usable && lastFrame.get(frame.cameraId) === frame.timestamp
    if (usable) lastFrame.set(frame.cameraId, frame.timestamp!)
    else lastFrame.delete(frame.cameraId)
    const readings = usable ? readZones(frame, zones, policy) : null
    return zones.map((zone) => {
      let state = memory.get(zone.id)
      // A redrawn polygon starts over: readings of the old shape say nothing about the new one.
      if (!state || state.revision !== zone.revision) {
        state = { revision: zone.revision, status: 'unknown', candidate: null, count: 0 }
        memory.set(zone.id, state)
      }
      const reading = readings?.get(zone.id)
      if (!reading) {
        Object.assign(state, { status: 'unknown', candidate: null, count: 0 })
      } else if (!repeated) {
        const threshold = state.status === 'occupied' ? policy.exitOverlap : policy.enterOverlap
        const next: ParkingStatus | null = reading.overlap >= threshold ? 'occupied' : reading.uncertain ? null : 'empty'
        if (next === null || next === state.status) Object.assign(state, { candidate: null, count: 0 })
        else {
          state.count = state.candidate === next ? state.count + 1 : 1
          state.candidate = next
          if (state.count >= (next === 'occupied' ? policy.occupiedFrames : policy.emptyFrames)) {
            Object.assign(state, { status: next, candidate: null, count: 0 })
          }
        }
      }
      return {
        zoneId: zone.id,
        cameraId: zone.cameraId,
        parkingSpaceId: zone.parkingSpaceId,
        status: state.status,
        score: reading && state.status !== 'unknown' ? Number(reading.overlap.toFixed(2)) : null,
        updatedAt: usable ? frame.timestamp : null,
        revision: zone.revision,
      }
    })
  }

  function forget(zoneIds?: Iterable<string>) {
    if (!zoneIds) { memory.clear(); lastFrame.clear(); return }
    for (const id of zoneIds) memory.delete(id)
  }

  return { observe, forget }
}
