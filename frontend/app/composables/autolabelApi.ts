import { trainingRequest } from '~/composables/trainingApi'
import type { AutoLabelJob } from '~/types/dataset'

// Auto-label jobs share the training queue and its admin token (see docs/training.md).
export type AutoLabelStart =
  | { model_id: string; conf: number; camera_id: string | null; include_auto_drafts: boolean }
  | { model_id: string; conf: number; image_ids: string[]; apply: false }

export function startAutoLabel(body: AutoLabelStart) {
  return trainingRequest<AutoLabelJob>('/autolabels', { method: 'POST', auth: true, body })
}

export function fetchAutoLabelJob(id: string) {
  return trainingRequest<AutoLabelJob>('/jobs/' + id)
}

export function autoLabelErrorMessage(cause: unknown) {
  return (cause as { status?: number }).status === 404
    ? 'The auto-label API is unavailable. The backend needs the auto-label update.'
    : (cause as Error).message
}
