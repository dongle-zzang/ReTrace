// Pure helpers for the training page (unit-tested in tests/training.test.cjs).

export interface TrainingForm {
  epochs: number | string
  imgsz: number
  batch: number | string
  seed: number | string
  patience: number | string
}
type Range = [number, number]
export interface FormLimits { epochs: Range; batch: Range; seed: Range; patience: Range; imgsz: number[] }

function integerIn(value: number | string, [min, max]: Range) {
  const number = typeof value === 'number' ? value : String(value).trim() === '' ? Number.NaN : Number(value)
  return Number.isInteger(number) && number >= min && number <= max ? number : null
}

// Mirrors the Backend JobCreate model so mistakes are shown before anything is sent.
export function validateTrainingForm(form: TrainingForm, limits: FormLimits) {
  const errors: string[] = []
  const check = (label: string, value: number | string, range: Range) => {
    if (integerIn(value, range) === null) errors.push(`${label} must be a whole number from ${range[0]} to ${range[1]}.`)
  }
  check('Epochs', form.epochs, limits.epochs)
  check('Batch size', form.batch, limits.batch)
  check('Seed', form.seed, limits.seed)
  check('Patience', form.patience, limits.patience)
  if (!limits.imgsz.includes(form.imgsz)) errors.push(`Image size must be ${limits.imgsz.join(' or ')}.`)
  return errors
}

export function toInteger(value: number | string) {
  return typeof value === 'number' ? value : Number(String(value).trim())
}

export function estimateVramMib(imgsz: number, batch: number, model = { base_mib: 150, per_image_mib_at_640: 800 }) {
  return Math.round(model.base_mib + model.per_image_mib_at_640 * batch * (imgsz / 640) ** 2)
}

export function requiredFreeMib(imgsz: number, batch: number, policy: { min_free_mib: number; reserve_mib: number; vram_estimate?: { base_mib: number; per_image_mib_at_640: number } }) {
  return Math.max(policy.min_free_mib, estimateVramMib(imgsz, batch, policy.vram_estimate) + policy.reserve_mib)
}

// Largest batch whose estimate fits the current free memory (0 when even batch 1 does not).
export function safeBatch(imgsz: number, freeMib: number, policy: Parameters<typeof requiredFreeMib>[2], max = 32) {
  let best = 0
  for (let batch = 1; batch <= max; batch++) if (requiredFreeMib(imgsz, batch, policy) <= freeMib) best = batch
  return best
}

export function formatDuration(seconds: number) {
  const s = Math.max(0, Math.round(seconds))
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60
  return h ? `${h}h ${String(m).padStart(2, '0')}m` : m ? `${m}m ${String(r).padStart(2, '0')}s` : `${r}s`
}

export function formatMetric(value: number | null | undefined, digits = 3) {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '–'
}

export function formatMib(value: number | null | undefined) {
  return typeof value === 'number' ? `${(value / 1024).toFixed(1)} GiB` : '–'
}

export function isActiveStatus(status: string) {
  return status === 'queued' || status === 'running' || status === 'stopping'
}

export interface ChartSeries { key: string; label: string; className: string; dotClass: string }
export interface ChartLayout { width: number; height: number; padding: number }

// Shared y domain over all series so lines are comparable; flat data gets a visible band.
export function chartDomain(points: Record<string, number | null>[], keys: string[]) {
  const values = points.flatMap(point => keys.map(key => point[key])).filter((v): v is number => typeof v === 'number' && Number.isFinite(v))
  if (!values.length) return null
  let min = Math.min(...values), max = Math.max(...values)
  if (min === max) { min -= Math.abs(min) * 0.1 || 0.5; max += Math.abs(max) * 0.1 || 0.5 }
  return { min, max }
}

export function linePath(points: Record<string, number | null>[], key: string, domain: { min: number; max: number }, layout: ChartLayout, xKey = 'epoch') {
  const xs = points.map(point => point[xKey]).filter((v): v is number => typeof v === 'number')
  const xMin = Math.min(...xs), xMax = Math.max(...xs)
  const x = (value: number) => layout.padding + (xMax === xMin ? 0.5 : (value - xMin) / (xMax - xMin)) * (layout.width - 2 * layout.padding)
  const y = (value: number) => layout.height - layout.padding - (value - domain.min) / (domain.max - domain.min) * (layout.height - 2 * layout.padding)
  let path = '', pen = false
  for (const point of points) {
    const xv = point[xKey], yv = point[key]
    if (typeof xv !== 'number' || typeof yv !== 'number' || !Number.isFinite(yv)) { pen = false; continue }
    path += `${pen ? 'L' : 'M'}${x(xv).toFixed(1)} ${y(yv).toFixed(1)} `
    pen = true
  }
  return path.trim()
}

// Pixel xyxy box -> percentage rectangle over the displayed image.
export function boxStyle(box: number[], width: number, height: number) {
  const [x1 = 0, y1 = 0, x2 = 0, y2 = 0] = box
  return { left: `${(x1 / width) * 100}%`, top: `${(y1 / height) * 100}%`, width: `${((x2 - x1) / width) * 100}%`, height: `${((y2 - y1) / height) * 100}%` }
}

export function deltaLabel(next: number | null | undefined, base: number | null | undefined) {
  if (typeof next !== 'number' || typeof base !== 'number') return null
  const delta = next - base
  return `${delta >= 0 ? '+' : ''}${delta.toFixed(3)}`
}

export interface JobLike { status: string; export_id: string; created_at: string; name: string; config: { kind?: string; train_mode?: string; smoke?: boolean }; best: { epoch: number; map50_95: number | null } | null }

// Train All has no validation to stop on, so its epoch count should come from a standard run on the
// same data: the epoch where validation mAP50-95 peaked (the latest such run wins).
export function suggestedTrainAllEpochs(jobs: JobLike[], exportId: string) {
  const runs = jobs.filter(job => job.status === 'completed' && job.export_id === exportId && (job.config.kind ?? 'train') === 'train'
    && (job.config.train_mode ?? 'standard') === 'standard' && !job.config.smoke && job.best && typeof job.best.map50_95 === 'number')
  runs.sort((a, b) => b.created_at.localeCompare(a.created_at))
  const run = runs[0]
  return run?.best ? { epoch: run.best.epoch, name: run.name } : null
}

export function jobKind(config: { kind?: string; train_mode?: string }) {
  return config.kind === 'deploy' ? 'deploy' : config.kind === 'evaluate' ? 'evaluate' : config.train_mode === 'all' ? 'all' : 'standard'
}

/** Phases reported by the DeepStream runtime (vehicle_deployment.py) and the deployment worker. */
/** Short class description of a deployable model, e.g. for the deploy button area. */
export function deployClasses(classMode: string) {
  return classMode === 'single' ? '1 class (car 0 → runtime car)' : 'COCO80 (car = class 2)'
}

export const DEPLOY_PHASES: Record<string, string> = {
  idle: 'Ready to deploy', queued: 'Waiting for the GPU worker', waiting_gpu: 'Waiting for free GPU resources',
  preparing: 'Preparing deployment', exporting: 'Exporting ONNX and checking compatibility',
  validating: 'Checking model compatibility', building: 'Building TensorRT engine', applying: 'Applying model',
  verifying: 'Verifying live inference', rolling_back: 'Restoring previous model', saving: 'Saving results',
  completed: 'Deployment verified', failed: 'Deployment failed', rolled_back: 'Previous model restored',
  rollback_failed: 'Recovery could not be verified',
}

/** One failed or advisory check from vehicle_deployment.py assess(); blocking=false never fails a rollback. */
export interface DeploymentCheck {
  check: string; camera?: string; fps?: number; baseline_fps?: number; state?: string; inference_age?: number | null
  frames?: number; status?: number; loaded_after_seconds?: number; cameras?: string[]; blocking?: boolean
  baseline_low?: number | null; inference_fps?: number | null; baseline_state?: string; pipeline_slow?: boolean
}
/** fps/baseline_fps are window medians; ranges are [p10, p90] of 1 s samples (vehicle_deployment.py). */
export interface DeploymentCameraEvidence {
  frames: number; detections: number; fps?: number; baseline_fps?: number; state?: string; inference_age?: number | null
  baseline_range?: (number | null)[]; fps_range?: (number | null)[]; inference_fps?: number | null; baseline_inference_fps?: number | null
}
export interface DeploymentService {
  fps: number; baseline_fps: number; cameras?: Record<string, { fps: number; baseline_fps: number; state: string }>
  vehicle_inference_fps?: number; baseline_vehicle_inference_fps?: number
}
/** After a rollback: FPS of the slow cameras before, with the candidate and with the previous model again. */
export interface DeploymentDiagnosis {
  cameras: Record<string, { baseline_fps: number; baseline_range?: (number | null)[] | null; candidate_fps: number; rollback_fps: number; verdict: string }>
  service?: { baseline_fps?: number | null; candidate_fps?: number | null; rollback_fps?: number | null }
}
/** Frozen at the moment a model or rollback check failed; a later rollback does not overwrite it. */
export interface DeploymentFailure {
  code: string; phase: string; model_id?: string; load_event?: boolean; model_loaded?: boolean
  elapsed_seconds?: number; loaded_after_seconds?: number | null; checks: DeploymentCheck[]
  evidence?: Record<string, DeploymentCameraEvidence> | null; service?: DeploymentService | null
}
export interface DeploymentReport {
  evidence?: Record<string, DeploymentCameraEvidence> | null; service?: DeploymentService | null
  checks?: DeploymentCheck[] | null; failures?: DeploymentFailure[] | null; diagnosis?: DeploymentDiagnosis | null
}

const DEPLOY_CHECKS: Record<string, string> = {
  model_not_loaded: 'nvinfer did not confirm the model load',
  model_load_failed: 'nvinfer could not load the model (status {status})',
  settling: 'The model loaded after {seconds} s; stabilization did not finish in time',
  camera_unavailable: '{camera}: camera state is {state}',
  camera_offline_before_deployment: '{camera}: already {state} before the switch; excluded from verification',
  camera_went_offline: '{camera}: went {state} during the deployment (was {was})',
  camera_went_offline_others: 'Other cameras went offline during the deployment: {cameras}',
  fps_below_baseline: '{camera}: median {fps} fps, below 85% of the pre-switch median {baseline} fps (own low {low} fps, inferred {inference} fps)',
  fps_below_baseline_pipeline_ok: '{camera}: median {fps} fps, below 85% of the pre-switch median {baseline} fps, while the shared pipeline kept its rate (other cameras and inference throughput ≥ 85%): camera source, not the model',
  camera_fps_variation: '{camera}: median {fps} fps is below 85% of {baseline} fps but inside its own pre-switch range (low {low} fps) and inference keeps pace ({inference} fps)',
  vehicle_inference_below_baseline: 'Parking cameras inferred {fps} frames/s in total, below 85% of {baseline} frames/s before the switch',
  inference_stalled: '{camera}: no inferred frame for {age} s',
  insufficient_inference_frames: '{camera}: only {frames} inferred frames',
  no_vehicle_detections: 'No vehicle detections on the parking cameras',
  service_fps_below_baseline: 'Other cameras total {fps} fps, below 85% of {baseline} fps before the switch (slow: {cameras})',
}

export const DIAGNOSIS_VERDICTS: Record<string, string> = {
  model_suspected: 'recovered with the previous model: the new model is the likely cause',
  camera_source_suspected: 'stayed low with the previous model: the camera/RTSP source is the likely cause',
  inconclusive: 'camera was not live after the rollback: cause undetermined',
}

/** English i18n key and params for a runtime check; unknown codes are shown as is. */
export function deploymentCheckText(check: DeploymentCheck) {
  const value = (number?: number | null) => number == null ? '–' : String(number)
  return {
    key: (check.check === 'camera_went_offline' && !check.camera ? DEPLOY_CHECKS.camera_went_offline_others
      : check.check === 'fps_below_baseline' && check.pipeline_slow === false ? DEPLOY_CHECKS.fps_below_baseline_pipeline_ok
        : DEPLOY_CHECKS[check.check]) ?? check.check,
    params: {
      camera: check.camera ?? '–', state: check.state ?? '–', fps: value(check.fps), baseline: value(check.baseline_fps),
      low: value(check.baseline_low), inference: value(check.inference_fps), was: check.baseline_state ?? '–',
      age: value(check.inference_age), frames: value(check.frames), status: value(check.status),
      seconds: value(check.loaded_after_seconds), cameras: check.cameras?.length ? check.cameras.join(', ') : '–',
    },
  }
}

export interface DeployableModel { id: string; kind: string; class_mode: string; names_count?: number; config: Record<string, unknown> | null; files: Record<string, unknown> }
export interface DeploymentLike { available: boolean; active?: string | null }

/** null when the model may be deployed now, else the reason (English i18n key).
 *  COCO80 (car = class 2) deploys as is; a 1-class car head (class 0) is remapped to the runtime car id. */
export function deployBlocker(model: DeployableModel, deployment: DeploymentLike | null, busy: boolean) {
  if (model.kind !== 'finetuned' || !model.files['best.pt']) return 'A completed model with best.pt is required'
  if (model.config?.imgsz !== 640) return 'Only 640px models can be hot-deployed; other input sizes need a restart and new FPS/VRAM measurements'
  const classes = model.names_count ?? 0
  if (model.class_mode === 'vehicle3' ? classes > 0 && classes !== 80 : model.class_mode !== 'single' || classes !== 1)
    return 'Requires YOLO11m with COCO80 classes or a single car class'
  if (!deployment?.available) return 'Deployment must be enabled on the DeepStream runtime and the training worker must be online.'
  if (deployment.active === model.id) return 'This model is already active'
  if (busy) return 'Another training, evaluation or deployment job is active'
  return null
}
