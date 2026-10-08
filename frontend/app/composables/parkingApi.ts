import type { Point } from '~/types/overlay'
import type {
  ParkingSpace, ParkingSpaceDto, ParkingStatus, ParkingStatusReport, ParkingZone, ParkingZoneDto, ParkingZoneUpdate,
} from '~/types/parking'
import { findSpaceByLabel, isClientZoneId, parseStatusReport, randomId, validateLabel, validateZones } from '~/lib/parking'

/** Data access for parking. Screens only use this interface, never fetch directly. */
export interface ParkingApi {
  readonly kind: 'mock' | 'http'
  listSpaces(): Promise<ParkingSpace[]>
  /** Rejects with code 'duplicate' when the label already exists. */
  createSpace(label: string): Promise<ParkingSpace>
  listZones(cameraId: string): Promise<ParkingZone[]>
  /** Saves one camera's zones as a whole and resolves to what the server holds afterwards. */
  saveZones(cameraId: string, update: ParkingZoneUpdate): Promise<ParkingZone[]>
  getStatus(): Promise<ParkingStatusReport>
  /** Registers the zone's current view as its empty baseline. Run only while the spot is empty. */
  calibrateZone(zoneId: string): Promise<void>
  resetCalibration(zoneId: string): Promise<void>
}

export type ParkingApiErrorCode = 'duplicate' | 'invalid' | 'unavailable' | 'partial' | 'not-found'

export class ParkingApiError extends Error {
  constructor(
    readonly code: ParkingApiErrorCode,
    message: string,
    /** Server zones re-read after a failed save, when that re-read succeeded. */
    readonly zones?: ParkingZone[],
  ) {
    super(message)
    this.name = 'ParkingApiError'
  }
}

type Method = 'GET' | 'POST' | 'PATCH' | 'DELETE'
type Request = <T>(url: string, options?: { method?: Method; body?: unknown }) => Promise<T>

function errorStatus(error: unknown) {
  return (error as { statusCode?: number } | null)?.statusCode ?? (error as { status?: number } | null)?.status
}

function toApiError(error: unknown): ParkingApiError {
  if (error instanceof ParkingApiError) return error
  const status = errorStatus(error)
  if (status === 409) return new ParkingApiError('duplicate', 'That label already exists.')
  if (status === 400 || status === 422) return new ParkingApiError('invalid', 'The server rejected the parking data.')
  if (status === 404) return new ParkingApiError('not-found', "This server doesn't have the parking API yet (404).")
  return new ParkingApiError('unavailable', "Can't reach the parking server.")
}

const fromSpaceDto = (dto: ParkingSpaceDto): ParkingSpace => ({ id: dto.id, label: dto.label })

const fromZoneDto = (dto: ParkingZoneDto): ParkingZone => ({
  id: dto.id,
  cameraId: dto.cameraId,
  parkingSpaceId: dto.parkingSpaceId,
  polygon: dto.polygon.map(({ x, y }) => ({ x, y })),
  revision: dto.revision,
  updatedAt: dto.updatedAt ?? null,
})

/** Accepts a bare array or `{ items }`-style wrappers. */
function listOf<T>(body: unknown, key: string): T[] {
  if (Array.isArray(body)) return body as T[]
  const wrapped = (body as Record<string, unknown> | null)?.[key]
  return Array.isArray(wrapped) ? wrapped as T[] : []
}

const plainPolygon = (polygon: Point[]) => polygon.map(({ x, y }) => ({ x, y }))

const samePolygon = (a: Point[], b: Point[]) =>
  a.length === b.length && a.every((point, index) => point.x === b[index]!.x && point.y === b[index]!.y)

type Operation = { method: Method; url: string; body?: unknown; goneIsDone?: boolean }

/** Turns an edited zone list into per-zone REST calls against the list the edit started from. */
export function planZoneSave(cameraId: string, update: ParkingZoneUpdate): Operation[] {
  const zoneUrl = (id: string) => `/api/parking/zones/${encodeURIComponent(id)}`
  const base = new Map(update.base.map((zone) => [zone.id, zone]))
  const kept = new Set(update.zones.map((zone) => zone.id))
  const operations: Operation[] = []
  for (const zone of base.values()) {
    if (!kept.has(zone.id)) operations.push({ method: 'DELETE', url: zoneUrl(zone.id), goneIsDone: true })
  }
  for (const zone of update.zones) {
    const before = base.get(zone.id)
    if (!before || isClientZoneId(zone.id)) {
      // Drawn in this edit (client `pz-…` id): the server assigns the id.
      operations.push({
        method: 'POST',
        url: '/api/parking/zones',
        body: { cameraId, parkingSpaceId: zone.parkingSpaceId, polygon: plainPolygon(zone.polygon) },
      })
      continue
    }
    const body: { parkingSpaceId?: string; polygon?: Point[] } = {}
    if (zone.parkingSpaceId !== before.parkingSpaceId) body.parkingSpaceId = zone.parkingSpaceId
    if (!samePolygon(zone.polygon, before.polygon)) body.polygon = plainPolygon(zone.polygon)
    if (body.parkingSpaceId !== undefined || body.polygon) operations.push({ method: 'PATCH', url: zoneUrl(zone.id), body })
  }
  return operations
}

/**
 * Server REST adapter. Same-origin relative paths only; the dev server proxies /api.
 * The editor saves a camera's zones as a whole but the server has per-zone CRUD, so `saveZones`
 * sends DELETE / PATCH / POST one at a time (not atomic, no concurrent-edit detection) and then
 * re-reads the camera. If any request failed it rejects with 'partial' carrying the re-read zones.
 */
export function createHttpParkingApi(request: Request): ParkingApi {
  async function call<T>(url: string, options?: Parameters<Request>[1]) {
    try {
      return await request<T>(url, options)
    } catch (error) {
      throw toApiError(error)
    }
  }

  const loadZones = async (cameraId: string) =>
    listOf<ParkingZoneDto>(await call(`/api/parking/zones?cameraId=${encodeURIComponent(cameraId)}`), 'zones')
      // Guard against a server that ignores the filter.
      .filter((dto) => dto.cameraId === cameraId)
      .map(fromZoneDto)

  return {
    kind: 'http',
    async listSpaces() {
      return listOf<ParkingSpaceDto>(await call('/api/parking/spaces'), 'spaces').map(fromSpaceDto)
    },
    async createSpace(label) {
      return fromSpaceDto(await call<ParkingSpaceDto>('/api/parking/spaces', { method: 'POST', body: { label: label.trim() } }))
    },
    listZones: loadZones,
    async saveZones(cameraId, update) {
      const operations = planZoneSave(cameraId, update)
      let failed = 0
      let rejected = false
      for (const { method, url, body, goneIsDone } of operations) {
        try {
          await request(url, body === undefined ? { method } : { method, body })
        } catch (error) {
          // Deleting a zone that is already gone reaches the wanted state.
          if (goneIsDone && errorStatus(error) === 404) continue
          failed++
          rejected ||= toApiError(error).code === 'invalid'
        }
      }
      let zones: ParkingZone[]
      try {
        zones = await loadZones(cameraId)
      } catch {
        throw new ParkingApiError('unavailable', failed
          ? `${failed} of ${operations.length} changes weren't saved, and the server state couldn't be reloaded.`
          : "Saved, but the server state couldn't be reloaded. Refresh to check.")
      }
      if (failed) {
        throw new ParkingApiError(
          'partial',
          `${failed} of ${operations.length} changes weren't saved.${rejected ? ' The server rejected some data.' : ''} Reloaded the saved state from the server.`,
          zones,
        )
      }
      return zones
    },
    async getStatus() {
      return parseStatusReport(await call('/api/parking/status'))
    },
    async calibrateZone(zoneId) {
      await call(`/api/parking/zones/${encodeURIComponent(zoneId)}/calibrate`, { method: 'POST' })
    },
    async resetCalibration(zoneId) {
      await call(`/api/parking/zones/${encodeURIComponent(zoneId)}/calibrate`, { method: 'DELETE' })
    },
  }
}

// ---------------------------------------------------------------------------------------------
// Mock: spaces and zones persist in this browser's localStorage; status is generated (no detector).

const MOCK_STORAGE_KEY = 'retrace.parking.mock.v2'
const MOCK_STATUS_PERIOD_MS = 30_000

interface MockData {
  spaces: ParkingSpace[]
  zones: ParkingZone[]
}

interface MockOptions {
  storage?: Pick<Storage, 'getItem' | 'setItem'> | null
  now?: () => number
  latencyMs?: number
  seed?: MockData
}

/** A row of perspective zones: the far edge (top) is narrower than the near edge (bottom). */
function perspectiveRow(cameraId: string, labels: string[], far: [number, number, number], near: [number, number, number]) {
  const [farY, farX0, farX1] = far
  const [nearY, nearX0, nearX1] = near
  const count = labels.length
  const at = (y: number, x0: number, x1: number, index: number): Point => ({
    x: Number((x0 + ((x1 - x0) * index) / count).toFixed(4)),
    y,
  })
  return labels.map((label, index): ParkingZone => ({
    id: `seed-${cameraId}-${label}`,
    cameraId,
    parkingSpaceId: `seed-${label}`,
    polygon: [
      at(farY, farX0, farX1, index),
      at(farY, farX0, farX1, index + 1),
      at(nearY, nearX0, nearX1, index + 1),
      at(nearY, nearX0, nearX1, index),
    ],
  }))
}

/** A-04…A-06 and B-01/B-02 are seen by both cameras, to show the multi-camera view. */
export function mockSeed(): MockData {
  const labels = [
    ...['A-01', 'A-02', 'A-03', 'A-04', 'A-05', 'A-06'],
    ...['B-01', 'B-02', 'B-03', 'B-04', 'B-05'],
    ...['C-01', 'C-02'],
  ]
  return {
    spaces: labels.map((label) => ({ id: `seed-${label}`, label })),
    zones: [
      ...perspectiveRow('b1_parking_overview', ['A-01', 'A-02', 'A-03', 'A-04', 'A-05', 'A-06'], [0.42, 0.18, 0.78], [0.62, 0.04, 0.92]),
      ...perspectiveRow('b1_parking_overview', ['B-01', 'B-02', 'B-03', 'B-04', 'B-05'], [0.68, 0.02, 0.96], [0.96, 0, 1]),
      ...perspectiveRow('1f_outdoor_parking', ['A-04', 'A-05', 'A-06', 'B-01', 'B-02'], [0.5, 0.2, 0.8], [0.82, 0.05, 0.95]),
    ],
  }
}

function hash(text: string) {
  let value = 2166136261
  for (let index = 0; index < text.length; index++) {
    value ^= text.charCodeAt(index)
    value = Math.imul(value, 16777619)
  }
  return value >>> 0
}

/** Cameras mostly agree on a space; now and then one cannot see it or sees the opposite. */
function mockZoneStatus(zone: ParkingZone, bucket: number): { status: ParkingStatus; score: number | null } {
  const truth: ParkingStatus = hash(zone.parkingSpaceId + ':' + bucket) % 100 < 55 ? 'occupied' : 'empty'
  const roll = hash(zone.id + ':' + bucket) % 100
  const score = 0.55 + (hash(zone.id + ':score:' + bucket) % 44) / 100
  if (roll < 12) return { status: 'unknown', score: null }
  if (roll < 18) return { status: truth === 'occupied' ? 'empty' : 'occupied', score: Number((score - 0.2).toFixed(2)) }
  return { status: truth, score: Number(score.toFixed(2)) }
}

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}

function defaultStorage() {
  try {
    return globalThis.localStorage ?? null
  } catch {
    return null
  }
}

export function createMockParkingApi(options: MockOptions = {}): ParkingApi {
  const storage = options.storage === undefined ? defaultStorage() : options.storage
  const now = options.now ?? Date.now
  const latencyMs = options.latencyMs ?? 120
  let data: MockData | null = null

  function load() {
    if (data) return data
    try {
      const saved = storage?.getItem(MOCK_STORAGE_KEY)
      if (saved) data = JSON.parse(saved) as MockData
    } catch {
      // Corrupt or blocked storage: start from the seed.
    }
    data ??= clone(options.seed ?? mockSeed())
    return data
  }

  function persist() {
    try {
      storage?.setItem(MOCK_STORAGE_KEY, JSON.stringify(data))
    } catch {
      // Private mode or quota: the mock keeps working in memory.
    }
  }

  const delay = <T>(value: () => T) => new Promise<T>((resolve, reject) => {
    const run = () => {
      try {
        resolve(value())
      } catch (error) {
        reject(error)
      }
    }
    if (latencyMs > 0) setTimeout(run, latencyMs)
    else queueMicrotask(run)
  })

  return {
    kind: 'mock',
    listSpaces: () => delay(() => clone(load().spaces)),
    createSpace: (label) => delay(() => {
      const all = load()
      if (validateLabel(label)) throw new ParkingApiError('invalid', 'The server rejected the parking data.')
      if (findSpaceByLabel(all.spaces, label)) throw new ParkingApiError('duplicate', 'That label already exists.')
      const space = { id: 'ms-' + randomId(), label: label.trim() }
      data = { ...all, spaces: [...all.spaces, space] }
      persist()
      return clone(space)
    }),
    listZones: (cameraId) => delay(() => clone(load().zones.filter((zone) => zone.cameraId === cameraId))),
    saveZones: (cameraId, update) => delay(() => {
      const all = load()
      const spaceIds = new Set(all.spaces.map((space) => space.id))
      if (validateZones(update.zones, spaceIds).length || new Set(update.zones.map((zone) => zone.id)).size !== update.zones.length) {
        throw new ParkingApiError('invalid', 'The server rejected the parking data.')
      }
      const updatedAt = new Date(now()).toISOString()
      const saved = update.zones.map((zone): ParkingZone => ({
        id: isClientZoneId(zone.id) ? 'mz-' + randomId() : zone.id,
        cameraId,
        parkingSpaceId: zone.parkingSpaceId,
        polygon: plainPolygon(zone.polygon),
        updatedAt,
      }))
      data = { ...all, zones: [...all.zones.filter((zone) => zone.cameraId !== cameraId), ...saved] }
      persist()
      return clone(saved)
    }),
    getStatus: () => delay(() => {
      const bucket = Math.floor(now() / MOCK_STATUS_PERIOD_MS)
      const observedAt = new Date(now()).toISOString()
      return {
        zones: load().zones.map((zone) => ({
          zoneId: zone.id,
          cameraId: zone.cameraId,
          parkingSpaceId: zone.parkingSpaceId,
          ...mockZoneStatus(zone, bucket),
          updatedAt: observedAt,
        })),
        spaces: [],
      }
    }),
    calibrateZone: (zoneId) => delay(() => {
      if (!load().zones.some((zone) => zone.id === zoneId)) throw new ParkingApiError('not-found', 'Zone not found.')
    }),
    resetCalibration: (zoneId) => delay(() => {
      if (!load().zones.some((zone) => zone.id === zoneId)) throw new ParkingApiError('not-found', 'Zone not found.')
    }),
  }
}

let shared: ParkingApi | null = null

/** Server REST by default; `NUXT_PUBLIC_PARKING_API=mock` switches to browser-local mock data. */
export function useParkingApi(): ParkingApi {
  if (shared) return shared
  const mode = String(useRuntimeConfig().public.parkingApi ?? 'http')
  shared = mode !== 'mock'
    ? createHttpParkingApi(<T>(url: string, options?: { method?: Method; body?: unknown }) =>
        $fetch<T>(url, options as Parameters<typeof $fetch>[1]) as Promise<T>)
    : createMockParkingApi()
  return shared
}
