export type DatasetStatus = 'pending' | 'in_progress' | 'reviewed'
export interface VehicleBox {
  id: string
  class_id: number
  x: number
  y: number
  width: number
  height: number
  source: 'manual'
}
export interface DatasetImage {
  id: string
  camera_id: string
  captured_at: string
  capture_session: string
  width: number
  height: number
  status: DatasetStatus
  revision: number
  box_count: number
  image_url: string
  thumbnail_url: string
}
export interface DatasetDetail extends DatasetImage {
  boxes: VehicleBox[]
  reviewed_empty: boolean
  previous_id: string | null
  next_id: string | null
}
export interface DatasetPage { items: DatasetImage[]; total: number; page: number; page_size: number }
export interface DatasetExport {
  id: string
  status: 'building' | 'ready' | 'failed'
  created_at: string
  error: string | null
  manifest: { counts: Record<string, number>; warnings: string[] } | null
  download_url: string | null
}
export const DATASET_CLASSES = ['car', 'truck', 'bus'] as const
export const DATASET_STATUS_LABELS: Record<DatasetStatus, string> = { pending: 'Pending', in_progress: 'In progress', reviewed: 'Reviewed' }
