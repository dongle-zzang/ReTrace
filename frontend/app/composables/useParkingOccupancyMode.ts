import { normalizeOccupancyMode, type OccupancyMode } from '~/lib/vehicleOccupancy'

const STORAGE_KEY = 'retrace.parking.occupancy'

function readSaved() {
  try {
    return globalThis.localStorage?.getItem(STORAGE_KEY) ?? null
  } catch {
    return null
  }
}

/**
 * How parking occupancy is decided: 'detection' (vehicle boxes over the zone, the default) or
 * 'color' (the server's color/baseline status). The build default is NUXT_PUBLIC_PARKING_OCCUPANCY;
 * a choice made on the Parking page is kept in this browser and shared by every parking screen.
 */
export function useParkingOccupancyMode() {
  const fallback = normalizeOccupancyMode(useRuntimeConfig().public.parkingOccupancy)
  const mode = useState<OccupancyMode>('parking-occupancy-mode', () => normalizeOccupancyMode(readSaved(), fallback))

  function setMode(next: OccupancyMode) {
    mode.value = next
    try {
      globalThis.localStorage?.setItem(STORAGE_KEY, next)
    } catch {
      // Private mode or blocked storage: the choice lasts for this visit only.
    }
  }

  return { mode: readonly(mode), setMode }
}
