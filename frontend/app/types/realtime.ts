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
      persons: RealtimePerson[]
    }
  | {
      version: 1
      type: 'detections'
      cameraId: string
      runtimeSession: string
      timestamp: string
      persons: []
      stale: true
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

export interface RealtimePerson {
  trackId: string | null
  bbox: { x: number; y: number; width: number; height: number }
  confidence: number | null
  trackerConfidence: number | null
}
