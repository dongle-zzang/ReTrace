export interface CameraStatus {
  camera_id: string
  state: string
  fps: number
  last_frame_at: string | null
  last_error: string | null
  reconnect_count: number
  generation: number
  runtime_session: string | null
  observed_at: string
  stale: boolean
}

export interface Camera {
  camera_id: string
  floor: number | string
  name: string
  enabled: boolean
  source_id: number | null
  preview_path: string | null
  metadata_path: string
  status: CameraStatus
}
