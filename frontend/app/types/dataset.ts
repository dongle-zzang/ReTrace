export type DatasetStatus = 'pending' | 'in_progress' | 'reviewed'
export interface VehicleBox {
  id: string
  class_id: number
  x: number
  y: number
  width: number
  height: number
  /** 'auto' = proposed by the auto-label model and not edited since; saving keeps it until a person moves/resizes it. */
  source: 'manual' | 'auto'
  /** Model confidence (0–1), only on auto boxes. */
  score?: number | null
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
  /** Boxes still exactly as the auto-label model proposed them (absent on older Backends). */
  auto_box_count?: number
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
  manifest: { classes?: Record<string, string>; counts: Record<string, number>; warnings: string[] } | null
  download_url: string | null
}
export const DATASET_CLASSES = ['car', 'truck', 'bus'] as const
export const DATASET_STATUS_LABELS: Record<DatasetStatus, string> = { pending: 'Pending', in_progress: 'In progress', reviewed: 'Reviewed' }
export interface AutoLabelPlan {
  eligible: number
  skipped: { reviewed: number; labeled: number }
  max_images: number
  ready: boolean
  blocker: string | null
}
export interface AutoLabelSummary {
  kind: 'autolabel'
  camera_id: string | null
  model_label: string
  total: number
  processed: number
  labeled: number
  boxes: number
  no_detections: number
  skipped_changed: number
  /** Only for single-image jobs started with apply: false: proposals the editor merges into its draft. */
  results?: { image_id: string; boxes: { x: number; y: number; width: number; height: number; score: number }[] }[]
}
export interface AutoLabelJob {
  id: string
  status: 'queued' | 'running' | 'stopping' | 'completed' | 'stopped' | 'failed'
  phase: string
  message: string | null
  progress: number | null
  error: string | null
  config: { kind?: string; conf?: number; images?: number }
  dataset_summary: AutoLabelSummary | null
  created_at: string
}
