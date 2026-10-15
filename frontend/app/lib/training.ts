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
  return config.kind === 'evaluate' ? 'evaluate' : config.train_mode === 'all' ? 'all' : 'standard'
}
