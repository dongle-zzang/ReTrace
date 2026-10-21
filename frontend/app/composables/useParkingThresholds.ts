import { DEFAULT_OCCUPANCY_POLICY, type ZoneThresholds } from '~/lib/vehicleOccupancy'

const STORAGE_KEY = 'retrace.parking.thresholds'

function readSaved(): Record<string, number> {
  try {
    const parsed: unknown = JSON.parse(globalThis.localStorage?.getItem(STORAGE_KEY) ?? '{}')
    if (!parsed || typeof parsed !== 'object') return {}
    return Object.fromEntries(Object.entries(parsed).filter(([, value]) => typeof value === 'number' && Number.isFinite(value)))
  } catch {
    return {}
  }
}

/**
 * Per-zone "occupied from" overlap for vehicle-detection occupancy, set in the zone editor.
 * Kept in this browser (like the detection reading itself) and shared by every parking screen.
 */
export function useParkingThresholds() {
  const thresholds = useState<Record<string, number>>('parking-zone-thresholds', readSaved)

  function setThreshold(zoneId: string, value: number | null) {
    const { [zoneId]: _removed, ...rest } = thresholds.value
    thresholds.value = value === null ? rest : { ...rest, [zoneId]: value }
    try {
      globalThis.localStorage?.setItem(STORAGE_KEY, JSON.stringify(thresholds.value))
    } catch {
      // Blocked storage: the setting lasts for this visit only.
    }
  }

  return { thresholds: readonly(thresholds) as Readonly<Ref<ZoneThresholds>>, setThreshold, defaultThreshold: DEFAULT_OCCUPANCY_POLICY.enterOverlap }
}
