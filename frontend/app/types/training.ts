export type TrainingJobStatus = 'queued' | 'running' | 'stopping' | 'completed' | 'failed' | 'stopped'
export type DatasetReadiness = 'ready' | 'limited' | 'blocked'
export type ClassMode = 'vehicle3' | 'single'
export type ImageSize = 640 | 960

export interface TrainingOptions {
  defaults: { epochs: number; imgsz: ImageSize; batch: number; seed: number; patience: number; device: 'gpu'; class_mode: ClassMode; base_model_id: string }
  limits: { epochs: [number, number]; batch: [number, number]; seed: [number, number]; patience: [number, number]; imgsz: ImageSize[] }
  class_modes: Record<ClassMode, string>
  gpu_policy: { min_free_mib: number; reserve_mib: number; max_gpu_util: number; max_fps_drop: number; guard_samples: number; require_preview: boolean; vram_estimate: { base_mib: number; per_image_mib_at_640: number } }
  control_enabled: boolean
  worker_enabled: boolean
  max_concurrent_jobs: number
}

export interface GpuSample {
  available?: boolean
  name?: string
  total_mib?: number
  used_mib?: number
  free_mib?: number
  util?: number
  temperature?: number | null
  training_mib?: number
  error?: string
}

export interface ProductionState {
  preview: string
  cameras_enabled: number
  cameras_online: number
  total_fps: number
  parking_cameras_online: number
  parking_fps: number
  max_frame_age_seconds: number | null
  observed_at: string
}

export interface MetricPoint {
  epoch: number
  time: number | null
  train_loss: number | null
  val_loss: number | null
  precision: number | null
  recall: number | null
  map50: number | null
  map50_95: number | null
  [key: string]: number | null
}

export interface DatasetSummary {
  readiness?: DatasetReadiness
  counts?: Record<'train' | 'val' | 'test' | 'total', number>
  boxes?: Record<'train' | 'val' | 'test' | 'total', number>
  labels_per_class_total?: Record<string, number>
  classes?: Record<string, string>
  sessions?: Record<'train' | 'val' | 'test', number>
  limits?: string[]
  notes?: string[]
}

export interface TrainingDataset extends DatasetSummary {
  id: string
  name: string
  created_at: string
  seed: number | null
  readiness: DatasetReadiness
  issue_count: number
}

export interface DatasetCheck extends TrainingDataset {
  errors: string[]
  limits: string[]
  notes: string[]
  labels_per_class?: Record<'train' | 'val' | 'test', Record<string, number>>
  empty_images?: Record<'train' | 'val' | 'test', number>
  cameras?: Record<'train' | 'val' | 'test', number>
}

export interface TrainingConfig {
  epochs: number
  imgsz: ImageSize
  batch: number
  seed: number
  patience: number
  class_mode: ClassMode
  device: 'gpu'
  smoke: boolean
  acknowledged_limits: string[]
  required_free_mib: number
  estimated_training_mib: number
}

export interface MonitorSample { t: string; fps_ratio: number | null; total_fps: number; parking_fps: number; cameras_online: number; max_frame_age: number | null; gpu_used_mib?: number; gpu_util?: number; training_mib?: number }

export interface EvaluationModelMetrics {
  vehicle?: { mAP50?: number; 'mAP50-95'?: number; precision_best_f1?: number; recall_best_f1?: number }
  operating_point?: { precision?: number; recall?: number; tp?: number; fp?: number; fn?: number; gt?: number; fn_by_size?: Record<string, number>; gt_by_size?: Record<string, number> }
  per_class?: Record<string, { precision: number; recall: number; mAP50: number; 'mAP50-95': number }>
  ultralytics_val?: { precision: number; recall: number; mAP50: number; 'mAP50-95': number; single_cls: boolean; per_class?: Record<string, { precision: number; recall: number; mAP50: number; 'mAP50-95': number }> }
}

export interface Evaluation {
  status: 'completed' | 'skipped' | 'failed'
  reason?: string
  conclusive?: boolean
  caveats?: string[]
  metrics?: { images?: number; gt_boxes?: number; imgsz?: number; conf?: number; nms_iou?: number; dataset_warnings?: string[]; models?: Record<string, EvaluationModelMetrics> }
}

export interface TrainingJob {
  id: string
  name: string
  status: TrainingJobStatus
  phase: string
  message: string | null
  export_id: string
  dataset_sha256: string
  base_model_id: string
  config: TrainingConfig
  created_at: string
  started_at: string | null
  finished_at: string | null
  heartbeat_at: string | null
  epoch: number
  epochs: number
  progress: number
  latest: MetricPoint | null
  best: MetricPoint | null
  gpu: GpuSample | null
  stop_requested: boolean
  stop_reason: string | null
  error_code: string | null
  error: string | null
  model_id: string | null
  elapsed_seconds: number
  dataset_summary: DatasetSummary
}

export interface TrainingJobDetail extends TrainingJob {
  metrics: MetricPoint[]
  live: { epoch?: number; epochs?: number; batch?: number; batches?: number; batch_size?: number; train_loss?: Record<string, number>; torch_reserved_mib?: number } | null
  monitor: { baseline?: ProductionState; samples?: MonitorSample[]; violations?: number; last_reasons?: string[]; protection_stop?: boolean; limits?: { max_fps_drop: number; reserve_mib: number; guard_samples: number } } | null
  log_tail: string[]
  run_info: Record<string, unknown> | null
  evaluation: Evaluation | null
  artifacts: Record<string, { size: number; sha256: string; download_url: string }>
  has_comparison: boolean
}

export interface TrainingStatus {
  worker: { online: boolean; worker_id?: string; last_seen: string | null; age_seconds?: number; gpu?: GpuSample; base_weights?: { present?: boolean; sha256?: string }; disk_free_mib?: number | null }
  production: ProductionState
  active_job: TrainingJob | null
  estimate: { imgsz: number; batch: number; training_mib: number; required_free_mib: number }
  gpu_ready: boolean
  gpu_blocker: string | null
}

export interface TrainingModel {
  id: string
  name: string
  version: number
  kind: 'base' | 'finetuned'
  label: string
  base_model_id: string | null
  job_id: string | null
  export_id: string | null
  dataset_sha256: string | null
  class_mode: string
  config: Record<string, number | string | null> | null
  metrics: { val?: MetricPoint | null; test?: EvaluationModelMetrics | null; test_baseline?: EvaluationModelMetrics | null; test_images?: number | null; evaluation_status?: string } | null
  evaluation_conclusive: boolean
  files: Record<string, { size?: number; sha256?: string; location?: string; download_url: string | null }>
  created_at: string
  deployment: string
  status: 'available' | 'incomplete'
}

export interface ComparisonModelResult { tp: number; fp: number; fn: number; pred_tp?: number[][]; pred_fp?: number[][]; gt_fn?: number[][] }
export interface ComparisonImage { id: string; width: number; height: number; gt: number[][]; models: Record<string, ComparisonModelResult>; image_url: string | null }
export interface ComparisonPage { split: string; imgsz: number; conf: number; nms_iou: number; total: number; offset: number; images: ComparisonImage[] }

export const JOB_STATUS_LABELS: Record<TrainingJobStatus, string> = {
  queued: 'Queued', running: 'Running', stopping: 'Stopping', completed: 'Completed', failed: 'Failed', stopped: 'Stopped',
}
export const READINESS_LABELS: Record<DatasetReadiness, string> = { ready: 'Ready', limited: 'Limited', blocked: 'Blocked' }
export const CLASS_MODE_LABELS: Record<ClassMode, string> = { vehicle3: 'car / truck / bus', single: 'Single vehicle class' }
