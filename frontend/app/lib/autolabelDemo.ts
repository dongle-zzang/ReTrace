import type { AutoLabelJob, AutoLabelPlan, VehicleBox } from '~/types/dataset'

// Dev-only preview of the auto-label flow before the Backend/worker part exists.
// Open /datasets?demo=autolabel under `npm run dev`. Nothing here talks to the server or saves labels.

export const AUTO_LABEL_DEMO_QUERY = { demo: 'autolabel' } as const

export function isAutoLabelDemo(query: Record<string, unknown>) {
  return import.meta.dev && query.demo === 'autolabel'
}

export const demoModels = [
  { id: 'demo-finetuned', kind: 'finetuned', label: 'vehicle (demo) v1', class_mode: 'coco80', status: 'available' },
  { id: 'demo-base', kind: 'base', label: 'YOLO11m COCO', class_mode: 'coco80', status: 'available' },
]

export function demoPlan(pending: number): AutoLabelPlan {
  const eligible = Math.max(pending, 6)
  return { eligible, skipped: { reviewed: 12, labeled: 2 }, max_images: 500, ready: true, blocker: null }
}

/** A fake job that finishes after ~8 s; `at(ms)` returns its state that long after start. */
export function demoJob(total: number, conf: number) {
  const started = Date.now()
  const duration = 8000
  return (now = Date.now()): AutoLabelJob => {
    const ratio = Math.min(1, (now - started) / duration)
    const processed = ratio < 0.12 ? 0 : Math.round(total * ratio)
    const done = ratio >= 1
    const labeled = Math.round(processed * 0.85)
    return {
      id: 'demo-job', status: done ? 'completed' : processed ? 'running' : 'queued', phase: done ? 'saving' : 'training',
      message: null, progress: ratio, error: null, config: { kind: 'autolabel', conf, images: total }, created_at: new Date(started).toISOString(),
      dataset_summary: { kind: 'autolabel', camera_id: null, model_label: 'demo', total, processed, labeled, boxes: labeled * 3, no_detections: processed - labeled, skipped_changed: 0 },
    }
  }
}

/** Three plausible draft boxes in the lower half of a parking-camera frame. */
export function demoBoxes(width: number, height: number): VehicleBox[] {
  const rows: [number, number, number, number, number][] = [
    [0.18, 0.55, 0.16, 0.2, 0.93], [0.42, 0.6, 0.15, 0.19, 0.78], [0.68, 0.52, 0.13, 0.17, 0.41],
  ]
  return rows.map(([x, y, w, h, score], index) => ({
    id: `00000000-0000-4000-8000-00000000000${index + 1}`, class_id: 0,
    x: x * width, y: y * height, width: w * width, height: h * height, source: 'auto', score,
  }))
}
