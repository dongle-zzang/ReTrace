/** Server /ws text wire contract. Video frames arrive as binary messages (see parseVideoFrame). */
export type RealtimeMessage =
  | { version: 1; type: 'hello'; [key: string]: unknown }
  | { version: 1; type: 'subscribed'; cameraIds: string[]; video: boolean; videoFps: number }
  | { version: 1; type: 'error'; code: string; [key: string]: unknown }
  | {
      version: 1
      type: 'detections'
      cameraId: string
      sourceId: number
      runtimeSession: string
      generation: number
      timestamp: string
      frameNumber: number
      ptsNs: string | null
      inferenceDone: boolean
      /** Legacy field; person detection was removed and the server always sends []. */
      persons?: []
      /** Vehicle boxes for parking cameras; also drawn into the JPEG frames by the server. */
      vehicles: RealtimeVehicle[]
      vehicleDetectionEnabled: boolean
      vehicleInferenceDone: boolean
    }
  | {
      version: 1
      type: 'detections'
      cameraId: string
      runtimeSession: string
      timestamp: string
      persons?: []
      vehicles: []
      vehicleInferenceDone: false
      stale: true
    }
  | {
      // Producer extensions; delivered only for subscribed cameras. See lib/parking.ts for parking data.
      version: 1
      type: 'parking_status' | 'event' | 'track_update'
      cameraId: string
      timestamp: string
      data: unknown
      [key: string]: unknown
    }
  | {
      version: 1
      type: 'camera_status'
      cameraId: string
      sourceId: number
      runtimeSession: string
      generation: number
      timestamp: string
      status: RealtimeCameraStatus
    }

export interface RealtimeCameraStatus {
  state: 'connecting' | 'online' | 'degraded' | 'offline' | 'reconnecting' | string
  fps: number
  last_error?: string | null
  reconnect_count?: number
  generation?: number
  [key: string]: unknown
}

export interface RealtimeVehicle {
  trackId: string | null
  bbox: { x: number; y: number; width: number; height: number }
  confidence: number | null
  trackerConfidence: number | null
}
