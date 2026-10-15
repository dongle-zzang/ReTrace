<script setup lang="ts">
import { AlertTriangle, Download, LoaderCircle, Square } from '@lucide/vue'
import { Badge } from '~/components/ui/badge'
import { Button } from '~/components/ui/button'
import TrainingChart from '~/components/TrainingChart.vue'
import TrainingComparison from '~/components/TrainingComparison.vue'
import { deltaLabel, formatDuration, formatMetric, formatMib, isActiveStatus } from '~/lib/training'
import { CLASS_MODE_LABELS, JOB_STATUS_LABELS, TRAIN_MODE_LABELS } from '~/types/training'
import type { EvaluationModelMetrics, TrainingJobDetail } from '~/types/training'
import { useI18n } from '~/composables/useI18n'

const props = defineProps<{ job: TrainingJobDetail; canControl: boolean; stopping: boolean }>()
const emit = defineEmits<{ stop: []; download: [name: string, url: string]; evaluate: [modelId: string | null] }>()

const { t } = useI18n()
const active = computed(() => isActiveStatus(props.job.status))
// Server messages are English; a protection stop joins several guard reasons after a fixed prefix.
const PROTECTION_PREFIX = 'Production protection: '
function serverText(text: string | null | undefined) {
  if (!text) return ''
  if (!text.startsWith(PROTECTION_PREFIX)) return t(text)
  return t('Production protection: {reasons}', { reasons: text.slice(PROTECTION_PREFIX.length).split('; ').map(reason => t(reason)).join('; ') })
}
const evaluating = computed(() => props.job.config.kind === 'evaluate')
// Train All validates (if at all) on its own training images: only the training loss is shown.
const trainAll = computed(() => props.job.config.train_mode === 'all')
const PHASES: Record<string, string> = {
  queued: 'Waiting for the GPU worker', preparing: 'Preparing the dataset snapshot', validating: 'Validating the dataset',
  waiting_gpu: 'Waiting for free GPU resources', training: 'Training on the GPU', evaluating: 'Evaluating on the test split',
  evaluating_new: 'Evaluating on new CCTV images',
  saving: 'Saving results',
}
const lossSeries = computed(() => [
  { key: 'train_loss', label: t('Train'), className: 'stroke-primary', dotClass: 'bg-primary' },
  ...(trainAll.value ? [] : [{ key: 'val_loss', label: t('Validation'), className: 'stroke-amber-300', dotClass: 'bg-amber-300' }]),
])
const phaseText = computed(() => props.job.message || PHASES[evaluating.value && props.job.phase === 'evaluating' ? 'evaluating_new' : props.job.phase] || props.job.phase)
const metricSeries = [
  { key: 'precision', label: 'P', className: 'stroke-sky-400', dotClass: 'bg-sky-400' },
  { key: 'recall', label: 'R', className: 'stroke-violet-400', dotClass: 'bg-violet-400' },
  { key: 'map50', label: 'mAP50', className: 'stroke-primary', dotClass: 'bg-primary' },
  { key: 'map50_95', label: 'mAP50-95', className: 'stroke-amber-300', dotClass: 'bg-amber-300' },
]
const latest = computed(() => props.job.latest)
const tiles = computed(() => trainAll.value ? [{ label: t('Training loss'), value: formatMetric(latest.value?.train_loss) }] : [
  { label: t('Training loss'), value: formatMetric(latest.value?.train_loss) },
  { label: t('Validation loss'), value: formatMetric(latest.value?.val_loss) },
  { label: t('Precision'), value: formatMetric(latest.value?.precision) },
  { label: t('Recall'), value: formatMetric(latest.value?.recall) },
  { label: 'mAP50', value: formatMetric(latest.value?.map50) },
  { label: 'mAP50-95', value: formatMetric(latest.value?.map50_95) },
])
const guardSample = computed(() => props.job.monitor?.samples?.at(-1))
const statusClass = computed(() => ({
  completed: 'border-primary/25 bg-primary/10 text-primary', failed: 'border-red-400/25 bg-red-400/10 text-red-300',
  stopped: 'border-white/15 bg-white/5 text-muted-foreground', running: 'border-sky-400/25 bg-sky-400/10 text-sky-300',
  stopping: 'border-amber-300/25 bg-amber-300/10 text-amber-200', queued: 'border-amber-300/25 bg-amber-300/10 text-amber-200',
}[props.job.status]))
const evaluationModels = computed(() => props.job.evaluation?.metrics?.models ?? {})
const evalRows = computed(() => {
  const labels: Record<string, string> = { baseline: 'Current YOLO11m (COCO)', previous: 'Base of this run', new: evaluating.value ? 'Evaluated model' : 'New model' }
  return Object.entries(evaluationModels.value).map(([key, value]) => ({ key, label: labels[key] ? t(labels[key]) : key, value }))
})
const captionParams = computed(() => ({ images: props.job.evaluation?.metrics?.images ?? '–', boxes: props.job.evaluation?.metrics?.gt_boxes ?? '–', conf: props.job.evaluation?.metrics?.conf ?? 0.25 }))
const baselineMetrics = computed(() => evaluationModels.value.baseline)
const perClass = computed(() => {
  const model = evaluationModels.value.new
  return model?.per_class ?? model?.ultralytics_val?.per_class ?? null
})
function op(model: EvaluationModelMetrics | undefined) { return model?.operating_point ?? {} }
const logElement = ref<HTMLElement | null>(null)
watch(() => props.job.log_tail.length, () => nextTick(() => { if (logElement.value) logElement.value.scrollTop = logElement.value.scrollHeight }))
const downloads = computed(() => Object.entries(props.job.artifacts ?? {}).filter(([name]) => name !== 'eval_predictions.json'))
</script>

<template>
  <div class="space-y-5">
    <div class="flex flex-wrap items-start gap-3">
      <div class="mr-auto min-w-0 space-y-1">
        <div class="flex flex-wrap items-center gap-2">
          <h3 class="text-base font-semibold">{{ job.name }}</h3>
          <Badge variant="outline" :class="['px-2 py-0.5', statusClass]">{{ t(JOB_STATUS_LABELS[job.status]) }}</Badge>
          <Badge v-if="job.config.smoke" variant="outline" class="px-2 py-0.5 text-muted-foreground">{{ t('Smoke test') }}</Badge>
          <Badge v-if="evaluating" variant="outline" class="border-violet-400/25 bg-violet-400/10 px-2 py-0.5 text-violet-300">{{ t('New-image evaluation') }}</Badge>
          <Badge v-else-if="trainAll" variant="outline" class="border-sky-400/25 bg-sky-400/10 px-2 py-0.5 text-sky-300">{{ t(TRAIN_MODE_LABELS.all) }}</Badge>
        </div>
        <p v-if="evaluating" class="text-xs text-muted-foreground">
          {{ t('{count} new images', { count: job.config.eval_images ?? 0 }) }} · {{ job.config.imgsz }} px · dataset_{{ job.export_id.slice(0, 8) }}<template v-if="job.dataset_summary.excluded"> · {{ t('left out {trained} trained-on and {session} same-session images', { trained: job.dataset_summary.excluded.trained, session: job.dataset_summary.excluded.same_session }) }}</template>
        </p>
        <p v-else class="text-xs text-muted-foreground">
          {{ t(CLASS_MODE_LABELS[job.config.class_mode]) }} · {{ job.config.epochs }} {{ t('epochs') }} · {{ job.config.imgsz }} px · {{ t('batch') }} {{ job.config.batch }} · {{ t('seed') }} {{ job.config.seed }} · dataset_{{ job.export_id.slice(0, 8) }}
        </p>
        <p v-if="active" class="flex items-center gap-1.5 text-sm" role="status"><LoaderCircle class="size-4 animate-spin text-primary" />{{ t(phaseText) }}</p>
      </div>
      <Button v-if="!active && !evaluating && job.status === 'completed' && job.model_id" variant="outline" @click="emit('evaluate', job.model_id)">{{ t('Evaluate on new images') }}</Button>
      <Button v-if="active" variant="destructive" :disabled="!canControl || stopping || job.status === 'stopping'" :title="canControl ? undefined : t('Unlock training control first')" @click="emit('stop')">
        <LoaderCircle v-if="stopping || job.status === 'stopping'" class="animate-spin" /><Square v-else /> {{ job.status === 'stopping' ? t('Stopping…') : t('Stop training') }}
      </Button>
    </div>

    <div v-if="job.error || job.stop_reason" :class="['flex gap-3 rounded-xl border p-3 text-sm', job.status === 'failed' ? 'border-red-400/20 bg-red-400/5 text-red-300' : 'border-amber-300/20 bg-amber-300/5 text-amber-200']" role="alert">
      <AlertTriangle class="mt-0.5 size-4 shrink-0" />
      <div class="space-y-1"><p>{{ serverText(job.error || job.stop_reason) }}</p><p v-if="job.error_code" class="font-mono text-xs opacity-70">{{ job.error_code }}</p></div>
    </div>

    <p v-if="trainAll" class="rounded-xl border border-sky-400/20 bg-sky-400/5 p-3 text-xs text-sky-200">{{ t('Train All: train, val and test images all trained the weights, so there is no independent validation or test in this run. Early stopping is off, the final epoch is the result (best.pt = last.pt), and scores on training images are not shown.') }}</p>
    <p v-if="(job.dataset_summary.test_overlap ?? 0) > 0" class="rounded-xl border border-amber-300/20 bg-amber-300/5 p-3 text-xs text-amber-200">{{ t('{count} test image(s) were already used to train the base model, so this test is not independent.', { count: job.dataset_summary.test_overlap ?? 0 }) }}</p>

    <div v-if="!evaluating" class="space-y-2">
      <div class="flex flex-wrap justify-between gap-2 text-xs text-muted-foreground">
        <span>{{ t('Epoch') }} <span class="font-mono tabular-nums text-foreground">{{ job.epoch }} / {{ job.epochs }}</span><template v-if="job.live?.batches && active"> · {{ t('batch') }} {{ job.live.batch }}/{{ job.live.batches }}</template></span>
        <span>{{ Math.round(job.progress * 100) }}% · {{ t('elapsed {duration}', { duration: formatDuration(job.elapsed_seconds) }) }}</span>
      </div>
      <div class="h-2 overflow-hidden rounded-full bg-white/10" role="progressbar" :aria-valuenow="Math.round(job.progress * 100)" aria-valuemin="0" aria-valuemax="100" :aria-label="t('Training progress')">
        <div class="h-full rounded-full bg-primary transition-[width] duration-700" :style="{ width: `${job.progress * 100}%` }" />
      </div>
    </div>

    <dl v-if="!evaluating" :class="['grid grid-cols-2 gap-2', trainAll ? 'sm:grid-cols-2' : 'sm:grid-cols-3 xl:grid-cols-6']">
      <div v-for="tile in tiles" :key="tile.label" class="rounded-xl border border-white/10 bg-black/10 p-3"><dt class="text-[0.7rem] text-muted-foreground">{{ tile.label }}</dt><dd class="font-mono text-lg tabular-nums">{{ tile.value }}</dd></div>
    </dl>

    <div v-if="!evaluating" class="grid gap-2 text-xs sm:grid-cols-3">
      <p class="rounded-xl border border-white/10 bg-black/10 p-3"><span class="block text-muted-foreground">{{ t('GPU memory (training process)') }}</span><span class="font-mono tabular-nums">{{ formatMib(job.gpu?.training_mib) }}</span><span v-if="job.live?.torch_reserved_mib" class="text-muted-foreground"> · torch {{ formatMib(job.live.torch_reserved_mib) }}</span></p>
      <p class="rounded-xl border border-white/10 bg-black/10 p-3"><span class="block text-muted-foreground">{{ t('GPU total') }}</span><span class="font-mono tabular-nums">{{ formatMib(job.gpu?.used_mib) }} / {{ formatMib(job.gpu?.total_mib) }} · {{ job.gpu?.util ?? '–' }}%</span></p>
      <p class="rounded-xl border border-white/10 bg-black/10 p-3"><span class="block text-muted-foreground">{{ t('Production FPS vs. before training') }}</span><span class="font-mono tabular-nums">{{ guardSample?.fps_ratio != null ? `${Math.round(guardSample.fps_ratio * 100)}%` : '–' }}</span><span class="text-muted-foreground"> · {{ guardSample?.total_fps ?? '–' }} fps · {{ t('max frame age') }} {{ guardSample?.max_frame_age ?? '–' }} s</span></p>
    </div>
    <p v-if="job.monitor?.last_reasons?.length && active" class="text-xs text-amber-200">{{ t('Protection warning') }} ({{ job.monitor.violations }}/{{ job.monitor.limits?.guard_samples }}): {{ job.monitor.last_reasons.map(reason => t(reason)).join('; ') }}</p>

    <div v-if="!evaluating" :class="['grid gap-3', trainAll ? '' : 'lg:grid-cols-2']">
      <TrainingChart :title="t('Loss')" :points="job.metrics" :series="lossSeries" />
      <TrainingChart v-if="!trainAll" :title="t('Validation metrics')" :points="job.metrics" :series="metricSeries" />
    </div>

    <section v-if="job.status === 'completed'" class="space-y-4 rounded-2xl border border-white/10 bg-white/[0.02] p-4" :aria-label="t('Results')">
      <div class="flex flex-wrap items-center gap-2">
        <h3 class="mr-auto text-sm font-semibold">{{ t('Results') }}</h3>
        <span v-if="job.best && !trainAll && !evaluating" class="text-xs text-muted-foreground">{{ t('Best validation epoch {epoch}', { epoch: job.best.epoch }) }} · mAP50-95 {{ formatMetric(job.best.map50_95) }}</span>
        <span v-else-if="trainAll" class="text-xs text-muted-foreground">{{ t('Final epoch {epoch} weights', { epoch: job.epoch }) }}</span>
      </div>
      <div v-if="trainAll" class="flex flex-wrap items-center gap-3 rounded-xl border border-sky-400/20 bg-sky-400/5 p-3 text-sm text-sky-200"><span class="min-w-0 flex-1">{{ t('No independent score: every image of this dataset was used for training. Evaluate the model on newly captured CCTV images.') }}</span><Button v-if="job.model_id" size="sm" variant="outline" @click="emit('evaluate', job.model_id)">{{ t('Evaluate on new images') }}</Button></div>
      <div v-else-if="job.evaluation?.status !== 'completed'" class="rounded-xl border border-amber-300/20 bg-amber-300/5 p-3 text-sm text-amber-200">{{ t('Test comparison not available:') }} {{ job.evaluation?.reason ?? t('not run') }}</div>
      <template v-else>
        <div :class="['rounded-xl border p-3 text-sm', job.evaluation.conclusive ? 'border-primary/20 bg-primary/5 text-primary' : 'border-amber-300/20 bg-amber-300/5 text-amber-200']">
          <p class="font-medium">{{ job.evaluation.conclusive ? t('Independent test set is large enough to compare.') : t('Not enough independent test data: do not treat the difference as a confirmed improvement.') }}</p>
          <ul class="mt-1 list-disc space-y-0.5 pl-5 text-xs opacity-90"><li v-for="caveat in job.evaluation.caveats ?? []" :key="caveat">{{ t(caveat) }}</li></ul>
        </div>
        <div class="overflow-x-auto">
          <table class="w-full min-w-[40rem] text-left text-xs">
            <caption class="pb-2 text-left text-muted-foreground">{{ evaluating ? t('New CCTV images ({images} images, {boxes} boxes) · class-agnostic vehicle · conf {conf}', captionParams) : t('Test split ({images} images, {boxes} boxes) · class-agnostic vehicle · conf {conf}', captionParams) }}</caption>
            <thead class="text-muted-foreground"><tr><th class="py-1.5 pr-3 font-medium">{{ t('Model') }}</th><th class="pr-3 font-medium">mAP50</th><th class="pr-3 font-medium">mAP50-95</th><th class="pr-3 font-medium">{{ t('Precision') }}</th><th class="pr-3 font-medium">{{ t('Recall') }}</th><th class="pr-3 font-medium">TP</th><th class="pr-3 font-medium">FP</th><th class="font-medium">{{ t('Missed') }}</th></tr></thead>
            <tbody class="font-mono tabular-nums">
              <tr v-for="row in evalRows" :key="row.key" class="border-t border-white/5">
                <td class="py-1.5 pr-3 font-sans">{{ row.label }}</td>
                <td class="pr-3">{{ formatMetric(row.value.vehicle?.mAP50) }} <span v-if="row.key !== 'baseline'" class="text-muted-foreground">{{ deltaLabel(row.value.vehicle?.mAP50, baselineMetrics?.vehicle?.mAP50) }}</span></td>
                <td class="pr-3">{{ formatMetric(row.value.vehicle?.['mAP50-95']) }} <span v-if="row.key !== 'baseline'" class="text-muted-foreground">{{ deltaLabel(row.value.vehicle?.['mAP50-95'], baselineMetrics?.vehicle?.['mAP50-95']) }}</span></td>
                <td class="pr-3">{{ formatMetric(op(row.value).precision) }}</td><td class="pr-3">{{ formatMetric(op(row.value).recall) }}</td>
                <td class="pr-3">{{ op(row.value).tp ?? '–' }}</td><td class="pr-3">{{ op(row.value).fp ?? '–' }}</td><td>{{ op(row.value).fn ?? '–' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-if="perClass" class="overflow-x-auto">
          <table class="w-full min-w-[28rem] text-left text-xs">
            <caption class="pb-2 text-left text-muted-foreground">{{ evaluating ? t('Evaluated model, per class (new images)') : t('New model, per class (test)') }}</caption>
            <thead class="text-muted-foreground"><tr><th class="py-1.5 pr-3 font-medium">{{ t('Class') }}</th><th class="pr-3 font-medium">{{ t('Precision') }}</th><th class="pr-3 font-medium">{{ t('Recall') }}</th><th class="pr-3 font-medium">mAP50</th><th class="font-medium">mAP50-95</th></tr></thead>
            <tbody class="font-mono tabular-nums"><tr v-for="(value, name) in perClass" :key="name" class="border-t border-white/5"><td class="py-1.5 pr-3 font-sans">{{ name }}</td><td class="pr-3">{{ formatMetric(value.precision) }}</td><td class="pr-3">{{ formatMetric(value.recall) }}</td><td class="pr-3">{{ formatMetric(value.mAP50) }}</td><td>{{ formatMetric(value['mAP50-95']) }}</td></tr></tbody>
          </table>
        </div>
        <TrainingComparison v-if="job.has_comparison" :job-id="job.id" :new-images="evaluating" />
      </template>
    </section>

    <div v-if="downloads.length" class="flex flex-wrap items-center gap-2">
      <span class="text-xs text-muted-foreground">{{ t('Files') }}</span>
      <Button v-for="[name, meta] in downloads" :key="name" size="sm" variant="outline" :disabled="!canControl" :title="canControl ? `${name} · ${(meta.size / 1048576).toFixed(1)} MiB` : t('Unlock training control to download')" @click="emit('download', name, meta.download_url)"><Download /> {{ name }}</Button>
    </div>

    <details class="group rounded-xl border border-white/10 bg-black/20" :open="active || job.status === 'failed'">
      <summary class="cursor-pointer px-3 py-2 text-xs font-medium text-muted-foreground">{{ t('Training log ({count} recent lines)', { count: job.log_tail.length }) }}</summary>
      <pre ref="logElement" class="max-h-72 overflow-auto px-3 pb-3 font-mono text-[0.7rem] leading-relaxed whitespace-pre-wrap text-muted-foreground">{{ job.log_tail.join('\n') || t('No log output yet.') }}</pre>
    </details>
  </div>
</template>
