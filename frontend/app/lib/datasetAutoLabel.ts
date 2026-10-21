import type { VehicleBox } from '~/types/dataset'

// Auto-label helpers shared by the dataset list and the box editor (unit-tested in tests/datasetAutoLabel.test.cjs).

export const AUTO_LABEL_ACTIVE = ['queued', 'running', 'stopping'] as const
export const AUTO_LABEL_CONF = { min: 0.1, max: 0.9, step: 0.05, default: 0.35 } as const

export function isAutoLabelActive(status: string) {
  return (AUTO_LABEL_ACTIVE as readonly string[]).includes(status)
}

/** A box someone moved or resized is theirs now: it becomes 'manual' and loses the model score. */
export function markEditedBoxes(previous: VehicleBox[], next: VehicleBox[]): VehicleBox[] {
  const before = new Map(previous.map(box => [box.id, box]))
  return next.map((box) => {
    const old = before.get(box.id)
    if (box.source !== 'auto' || !old) return box
    const moved = old.x !== box.x || old.y !== box.y || old.width !== box.width || old.height !== box.height || old.class_id !== box.class_id
    if (!moved) return box
    const { score: _score, ...rest } = box
    return { ...rest, source: 'manual' }
  })
}

export function autoBoxCount(boxes: VehicleBox[]) {
  return boxes.filter(box => box.source === 'auto').length
}

export function formatScore(score: number | null | undefined) {
  return typeof score === 'number' && Number.isFinite(score) ? `${Math.round(score * 100)}%` : ''
}

/** 0–100 for the progress bar; processed/total wins over the coarse job progress. */
export function autoLabelPercent(job: { progress: number | null; dataset_summary: { total: number; processed: number } | null }) {
  const summary = job.dataset_summary
  const ratio = summary && summary.total > 0 ? summary.processed / summary.total : job.progress ?? 0
  return Math.round(Math.min(1, Math.max(0, ratio)) * 100)
}

// The last model/confidence a person started auto-labeling with, so later runs are one click.
export interface AutoLabelSettings { model_id: string; conf: number }
const SETTINGS_KEY = 'retrace.autolabelSettings'

export function readAutoLabelSettings(storage: Pick<Storage, 'getItem'> | undefined = globalThis.localStorage): AutoLabelSettings | null {
  try {
    const value = JSON.parse(storage?.getItem(SETTINGS_KEY) ?? 'null')
    if (typeof value?.model_id !== 'string' || !value.model_id || typeof value.conf !== 'number') return null
    return { model_id: value.model_id, conf: Math.min(AUTO_LABEL_CONF.max, Math.max(AUTO_LABEL_CONF.min, value.conf)) }
  } catch { return null }
}

export function saveAutoLabelSettings(settings: AutoLabelSettings, storage: Pick<Storage, 'setItem'> | undefined = globalThis.localStorage) {
  try { storage?.setItem(SETTINGS_KEY, JSON.stringify(settings)) } catch { /* private mode: ask again next time */ }
}

type Rect = Pick<VehicleBox, 'x' | 'y' | 'width' | 'height'>
export function boxIou(a: Rect, b: Rect) {
  const w = Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x)
  const h = Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y)
  if (w <= 0 || h <= 0) return 0
  const inter = w * h
  return inter / (a.width * a.height + b.width * b.height - inter)
}

/** Adds model proposals to the current draft, skipping ones that overlap a box already there. */
export function mergeAutoBoxes(existing: VehicleBox[], proposals: VehicleBox[], threshold = 0.5) {
  const kept: VehicleBox[] = []
  for (const proposal of proposals) {
    if ([...existing, ...kept].every(box => boxIou(box, proposal) < threshold)) kept.push(proposal)
  }
  return { boxes: [...existing, ...kept], added: kept.length }
}
