import type { Camera } from '~/types/camera'

// Basement labels (B1, B2, ...) sort below ground floors: B2, B1, 1, 3, 4, ...
export function floorOrder(floor: number | string) {
  const basement = /^B(\d+)$/i.exec(String(floor))
  return basement ? -Number(basement[1]) : Number(floor)
}

/** Floor label for the UI: basement floors stay as-is (B1), others get an F suffix (3F). */
export function floorLabel(floor: number | string) {
  return /^B\d+$/i.test(String(floor)) ? String(floor).toUpperCase() : `${floor}F`
}

export function compareCameras(a: Camera, b: Camera) {
  return floorOrder(a.floor) - floorOrder(b.floor) || a.name.localeCompare(b.name, 'ko')
}
