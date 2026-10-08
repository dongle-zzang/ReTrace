import type { Ref } from 'vue'
import type {
  ParkingCounts, ParkingSpace, ParkingSpaceSummary, ParkingSpaceVerdict, ParkingStatus, ParkingStatusReport, ParkingZone,
  ParkingZoneState,
} from '~/types/parking'
import type { RealtimeMessage } from '~/types/realtime'
import type { ConnectionState } from '~/composables/realtimePreviewClient'
import { compareLabels, countStatuses, findSpaceByLabel, isNewerState, statusReportFromMessage, summarizeSpaces } from '~/lib/parking'
import { ParkingApiError, useParkingApi } from '~/composables/parkingApi'

const STATUS_REFRESH_MS = 10_000

/** The page's existing /ws connection (from useRealtimePreview); parking never opens its own. */
export interface ParkingRealtime {
  connection: Ref<ConnectionState>
  onMessage(listener: (message: RealtimeMessage) => void): () => void
}

export interface UseParkingOptions {
  realtime?: ParkingRealtime
  /** Cameras whose zones this page needs. Cameras that report zone status are loaded too. */
  cameraIds?: Ref<string[]>
}

const errorMessage = (cause: unknown, fallback: string) => cause instanceof Error ? cause.message : fallback

/**
 * Parking spaces (labels), zones per camera and their status. Status comes from REST (on mount,
 * every 10 s and whenever /ws (re)connects) and from /ws `parking.status_updated`; REST covers
 * cameras the page is not subscribed to. Older zone reports never overwrite newer ones.
 * The editor works on its own draft, so status and zone reloads never touch an edit in progress.
 */
export function useParking(options: UseParkingOptions = {}) {
  const api = useParkingApi()
  const spaces = shallowRef<ParkingSpace[]>([])
  const zones = shallowRef<Record<string, ParkingZone[]>>({})
  const zoneStates = shallowRef<Record<string, ParkingZoneState>>({})
  const verdicts = shallowRef<Record<string, ParkingSpaceVerdict>>({})
  const loading = ref(true)
  const error = ref<string | null>(null)
  const statusError = ref<string | null>(null)
  const zonesLoading = new Map<string, Promise<void>>()
  let spacesLoading: Promise<void> | null = null
  let statusRequest = 0
  let intervalId: ReturnType<typeof setInterval> | undefined
  let stopMessages: (() => void) | undefined

  const sortedSpaces = computed(() => [...spaces.value].sort((a, b) => compareLabels(a.label, b.label)))
  const spaceLabels = computed(() => Object.fromEntries(spaces.value.map((space) => [space.id, space.label])))

  /** Status per zone id. A report computed for an older polygon revision reads as unknown. */
  const zoneStatuses = computed(() => {
    const revisions = new Map(Object.values(zones.value).flat().map((zone) => [zone.id, zone.revision]))
    const result: Record<string, ParkingStatus> = {}
    for (const state of Object.values(zoneStates.value)) {
      const revision = revisions.get(state.zoneId)
      if (revision === undefined || state.revision === undefined || state.revision >= revision) result[state.zoneId] = state.status
    }
    return result
  })

  /**
   * Current zone states for the space summaries: known zones without a report count as unknown,
   * and reports for zones a loaded camera no longer has are dropped.
   */
  const currentZoneStates = computed(() => {
    const result: ParkingZoneState[] = []
    const loaded = zones.value
    for (const state of Object.values(zoneStates.value)) {
      const cameraZones = loaded[state.cameraId]
      if (cameraZones && !cameraZones.some((zone) => zone.id === state.zoneId)) continue
      const zone = cameraZones?.find((item) => item.id === state.zoneId)
      result.push({
        ...state,
        // The zone's own label wins over the report (it may have been relabelled since).
        parkingSpaceId: zone?.parkingSpaceId ?? state.parkingSpaceId,
        status: zoneStatuses.value[state.zoneId] ?? 'unknown',
      })
    }
    for (const zone of Object.values(loaded).flat()) {
      if (!zoneStates.value[zone.id]) {
        result.push({ zoneId: zone.id, cameraId: zone.cameraId, parkingSpaceId: zone.parkingSpaceId, status: 'unknown', score: null, updatedAt: null })
      }
    }
    return result
  })

  const summaries = computed<ParkingSpaceSummary[]>(() => summarizeSpaces(spaces.value, currentZoneStates.value, verdicts.value))
  const summaryBySpace = computed(() => Object.fromEntries(summaries.value.map((summary) => [summary.space.id, summary])))

  const totals = computed<ParkingCounts>(() => {
    const counts: ParkingCounts = { total: 0, occupied: 0, empty: 0, unknown: 0 }
    for (const summary of summaries.value) {
      counts.total++
      counts[summary.status]++
    }
    return counts
  })

  function cameraCounts(cameraId: string): ParkingCounts | null {
    const list = zones.value[cameraId]
    return list?.length ? countStatuses(list.map((zone) => zone.id), zoneStatuses.value) : null
  }

  function mergeReport(report: ParkingStatusReport) {
    let next: Record<string, ParkingZoneState> | null = null
    for (const incoming of report.zones) {
      const current = (next ?? zoneStates.value)[incoming.zoneId]
      if (!isNewerState(current, incoming)) continue
      if (current && JSON.stringify(current) === JSON.stringify(incoming)) continue
      next ??= { ...zoneStates.value }
      next[incoming.zoneId] = incoming
    }
    if (next) zoneStates.value = next
    // The status report carries labels too; pick up spaces created elsewhere without a reload.
    const known = new Set(spaces.value.map((space) => space.id))
    const added = report.spaces.filter((verdict) => verdict.label && !known.has(verdict.spaceId))
    if (added.length) spaces.value = [...spaces.value, ...added.map((verdict) => ({ id: verdict.spaceId, label: verdict.label! }))]
    if (report.spaces.length) {
      verdicts.value = { ...verdicts.value, ...Object.fromEntries(report.spaces.map((verdict) => [verdict.spaceId, verdict])) }
    }
  }

  function loadSpaces() {
    spacesLoading ??= (async () => {
      try {
        spaces.value = await api.listSpaces()
        error.value = null
      } catch (cause) {
        error.value = errorMessage(cause, "Couldn't load parking spaces.")
      } finally {
        spacesLoading = null
      }
    })()
    return spacesLoading
  }

  function loadZones(cameraId: string) {
    let pending = zonesLoading.get(cameraId)
    if (!pending) {
      pending = (async () => {
        try {
          const list = await api.listZones(cameraId)
          zones.value = { ...zones.value, [cameraId]: list }
        } catch (cause) {
          error.value = errorMessage(cause, "Couldn't load parking zones.")
        } finally {
          zonesLoading.delete(cameraId)
        }
      })()
      zonesLoading.set(cameraId, pending)
    }
    return pending
  }

  async function ensureCameras(cameraIds: string[]) {
    await Promise.all(cameraIds.filter((id) => !zones.value[id]).map(loadZones))
  }

  /** REST reports every zone, so a different zone set means someone else edited: reload those cameras. */
  function syncWithReport(report: ParkingStatusReport) {
    const reported = new Map<string, string[]>()
    for (const state of report.zones) reported.set(state.cameraId, [...(reported.get(state.cameraId) ?? []), state.zoneId])
    const stale = Object.keys(zones.value).filter((cameraId) =>
      !zonesLoading.has(cameraId)
      && (reported.get(cameraId) ?? []).sort().join() !== zones.value[cameraId]!.map((zone) => zone.id).sort().join())
    const newCameras = [...reported.keys()].filter((cameraId) => !zones.value[cameraId])
    for (const cameraId of [...stale, ...newCameras]) void loadZones(cameraId)
    const known = new Set(spaces.value.map((space) => space.id))
    if (!spacesLoading && report.zones.some((state) => state.parkingSpaceId && !known.has(state.parkingSpaceId))) void loadSpaces()
  }

  async function loadStatus() {
    const request = ++statusRequest
    try {
      const report = await api.getStatus()
      if (request !== statusRequest) return
      statusError.value = null
      mergeReport(report)
      if (api.kind === 'http' && !loading.value) syncWithReport(report)
      else if (api.kind === 'mock') void ensureCameras([...new Set(report.zones.map((state) => state.cameraId))])
    } catch (cause) {
      if (request !== statusRequest) return
      statusError.value = errorMessage(cause, "Couldn't load parking status.")
      // Server unreachable: nothing is known, show everything as unknown.
      if (api.kind === 'http') {
        zoneStates.value = {}
        verdicts.value = {}
      }
    }
  }

  function applyZones(cameraId: string, list: ParkingZone[]) {
    zones.value = { ...zones.value, [cameraId]: list }
    void loadStatus()
  }

  /**
   * Returns the space with this label, creating it on the server when it does not exist yet.
   * The same label always resolves to the same space, even if another browser created it first.
   */
  async function createSpace(label: string): Promise<ParkingSpace> {
    const existing = findSpaceByLabel(spaces.value, label)
    if (existing) return existing
    try {
      const space = await api.createSpace(label)
      spaces.value = [...spaces.value.filter((item) => item.id !== space.id), space]
      return space
    } catch (cause) {
      if (cause instanceof ParkingApiError && cause.code === 'duplicate') {
        await loadSpaces()
        const found = findSpaceByLabel(spaces.value, label)
        if (found) return found
      }
      throw cause
    }
  }

  /**
   * Baseline state per zone as far as this page knows: true after a successful calibrate, false
   * after a reset. The status contract has no calibration field, so other zones stay unknown.
   */
  const calibrated = ref<Record<string, boolean>>({})

  async function calibrate(zoneId: string, register: boolean) {
    if (register) await api.calibrateZone(zoneId)
    else await api.resetCalibration(zoneId)
    calibrated.value = { ...calibrated.value, [zoneId]: register }
    void loadStatus()
  }

  async function reload() {
    const cameraIds = new Set([...Object.keys(zones.value), ...(options.cameraIds?.value ?? [])])
    await Promise.all([loadSpaces(), ...[...cameraIds].map(loadZones)])
    loading.value = false
    await loadStatus()
  }

  if (options.realtime) {
    stopMessages = options.realtime.onMessage((message) => {
      const report = statusReportFromMessage(message)
      if (report) mergeReport(report)
    })
    // First connect and every reconnect: messages may have been missed, so resync from REST.
    watch(options.realtime.connection, (state, previous) => {
      if (state === 'open' && previous !== 'open') void loadStatus()
    })
  }

  if (options.cameraIds) {
    watch(options.cameraIds, (ids) => {
      if (!loading.value) void ensureCameras(ids)
    })
  }

  onMounted(() => {
    void reload()
    // Server: REST on load and on every /ws (re)connect, then `parking.status_updated`. Mock has no /ws.
    if (api.kind === 'mock') intervalId = setInterval(() => void loadStatus(), STATUS_REFRESH_MS)
  })

  onUnmounted(() => {
    if (intervalId) clearInterval(intervalId)
    stopMessages?.()
  })

  return {
    api, spaces: sortedSpaces, spaceLabels, zones, zoneStates, zoneStatuses, summaries, summaryBySpace, totals,
    loading, error, statusError, calibrated, cameraCounts, applyZones, createSpace, calibrate, loadSpaces, ensureCameras, reload,
  }
}

export type ParkingStore = ReturnType<typeof useParking>
