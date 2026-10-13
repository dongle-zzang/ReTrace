/** Coordinates relative to the video content, each value in [0, 1]. */
export interface Point {
  x: number
  y: number
}

/** x/y and width/height are normalized to the video content. */
export interface NormalizedBox extends Point {
  width: number
  height: number
}

export interface CameraLine {
  id: string
  cameraId: string
  start: Point
  end: Point
  label?: string
}

export interface CameraPolygon {
  id: string
  cameraId: string
  points: Point[]
  label?: string
}

export interface ParkingZone {
  id: string
  cameraId: string
  points: Point[]
  occupied: boolean
  label?: string
}

export interface CameraOverlayData {
  lines: CameraLine[]
  polygons: CameraPolygon[]
  parkingZones: ParkingZone[]
}

export type OverlaySelection = {
  kind: 'line' | 'polygon' | 'parking'
  id: string
} | null

export function emptyCameraOverlay(): CameraOverlayData {
  return { lines: [], polygons: [], parkingZones: [] }
}