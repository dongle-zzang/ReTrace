<script setup lang="ts">
import { AlertTriangle, CircleCheck, Download, LoaderCircle, Square } from '@lucide/vue'
import { Badge } from '~/components/ui/badge'
import { Button } from '~/components/ui/button'
import TrainingChart from '~/components/TrainingChart.vue'
import TrainingComparison from '~/components/TrainingComparison.vue'
import { deltaLabel, formatDuration, formatMetric, formatMib, isActiveStatus } from '~/lib/training'
import { CLASS_MODE_LABELS, JOB_STATUS_LABELS } from '~/types/training'
import type { EvaluationModelMetrics, TrainingJobDetail } from '~/types/training'

const props = defineProps<{ job: TrainingJobDetail; canControl: boolean; stopping: boolean }>()
const emit = defineEmits<{ stop: []; download: [name: string, url: string] }>()

const active = computed(() => isActiveStatus(props.job.status))
const PHASES: Record<string, string> = {
  queued: 'Waiting for the GPU worker', preparing: 'Preparing the dataset snapshot', validating: 'Validating the dataset',
  waiting_gpu: 'Waiting for free GPU resources', training: 'Training on the GPU', evaluating: 'Evaluating on the test split',
  saving: 'Saving results',
}
const lossSeries = [
  { key: 'train_loss', label: 'Train', className: 'stroke-primary', dotClass: 'bg-primary' },
  { key: 'val_loss', label: 'Validation', className: 'stroke-amber-300', dotClass: 'bg-amber-300' },
]
const metricSeries = [
  { key: 'precision', label: 'P', className: 'stroke-sky-400', dotClass: 'bg-sky-400' },
  { key: 'recall', label: 'R', className: 'stroke-violet-400', dotClass: 'bg-violet-400' },
  { key: 'map50', label: 'mAP50', className: 'stroke-primary', dotClass: 'bg-primary' },
  { key: 'map50_95', label: 'mAP50-95', className: 'stroke-amber-300', dotClass: 'bg-amber-300' },
]
const latest = computed(() => props.job.latest)
const tiles = computed(() => [
  { label: 'Training loss', value: formatMetric(latest.value?.train_loss) },
  { label: 'Validation loss', value: formatMetric(latest.value?.val_loss) },
  { label: 'Precision', value: formatMetric(latest.value?.precision) },
  { label: 'Recall', value: formatMetric(latest.value?.recall) },
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
  const labels: Record<string, string> = { baseline: 'Current YOLO11m (COCO)', previous: 'Base of this run', new: 'New model' }
  return Object.entries(evaluationModels.value).map(([key, value]) => ({ key, label: labels[key] ?? key, value }))
})
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
          <Badge variant="outline" :class="['px-2 py-0.5', statusClass]">{{ JOB_STATUS_LABELS[job.status] }}</Badge>
          <Badge v-if="job.config.smoke" variant="outline" class="px-2 py-0.5 text-muted-foreground">Smoke test</Badge>
        </div>
        <p class="text-xs text-muted-foreground">
          {{ CLASS_MODE_LABELS[job.config.class_mode] }} · {{ job.config.epochs }} epochs · {{ job.config.imgsz }} px · batch {{ job.config.batch }} · seed {{ job.config.seed }} · dataset_{{ job.export_id.slice(0, 8) }}
        </p>
        <p v-if="active" class="flex items-center gap-1.5 text-sm" role="status"><LoaderCircle class="size-4 animate-spin text-primary" />{{ job.message || PHASES[job.phase] || job.phase }}</p>
      </div>
      <Button v-if="active" variant="destructive" :disabled="!canControl || stopping || job.status === 'stopping'" :title="canControl ? undefined : 'Unlock training control first'" @click="emit('stop')">
        <LoaderCircle v-if="stopping || job.status === 'stopping'" class="animate-spin" /><Square v-else /> {{ job.status === 'stopping' ? 'Stopping…' : 'Stop training' }}
      </Button>
    </div>

    <div v-if="job.error || job.stop_reason" :class="['flex gap-3 rounded-xl border p-3 text-sm', job.status === 'failed' ? 'border-red-400/20 bg-red-400/5 text-red-300' : 'border-amber-300/20 bg-amber-300/5 text-amber-200']" role="alert">
      <AlertTriangle class="mt-0.5 size-4 shrink-0" />
      <div class="space-y-1"><p>{{ job.error || job.stop_reason }}</p><p v-if="job.error_code" class="font-mono text-xs opacity-70">{{ job.error_code }}</p></div>
    </div>

    <div class="space-y-2">
      <div class="flex flex-wrap justify-between gap-2 text-xs text-muted-foreground">
        <span>Epoch <span class="font-mono tabular-nums text-foreground">{{ job.epoch }} / {{ job.epochs }}</span><template v-if="job.live?.batches && active"> · batch {{ job.live.batch }}/{{ job.live.batches }}</template></span>
        <span>{{ Math.round(job.progress * 100) }}% · elapsed {{ formatDuration(job.elapsed_seconds) }}</span>
      </div>
      <div class="h-2 overflow-hidden rounded-full bg-white/10" role="progressbar" :aria-valuenow="Math.round(job.progress * 100)" aria-valuemin="0" aria-valuemax="100" aria-label="Training progress">
        <div class="h-full rounded-full bg-primary transition-[width] duration-700" :style="{ width: `${job.progress * 100}%` }" />
      </div>
    </div>

    <dl class="grid grid-cols-2 gap-2 sm:grid-cols-3 xl:grid-cols-6">
      <div v-for="tile in tiles" :key="tile.label" class="rounded-xl border border-white/10 bg-black/10 p-3"><dt class="text-[0.7rem] text-muted-foreground">{{ tile.label }}</dt><dd class="font-mono text-lg tabular-nums">{{ tile.value }}</dd></div>
    </dl>

    <div class="grid gap-2 text-xs sm:grid-cols-3">
      <p class="rounded-xl border border-white/10 bg-black/10 p-3"><span class="block text-muted-foreground">GPU memory (training process)</span><span class="font-mono tabular-nums">{{ formatMib(job.gpu?.training_mib) }}</span><span v-if="job.live?.torch_reserved_mib" class="text-muted-foreground"> · torch {{ formatMib(job.live.torch_reserved_mib) }}</span></p>
      <p class="rounded-xl border border-white/10 bg-black/10 p-3"><span class="block text-muted-foreground">GPU total</span><span class="font-mono tabular-nums">{{ formatMib(job.gpu?.used_mib) }} / {{ formatMib(job.gpu?.total_mib) }} · {{ job.gpu?.util ?? '–' }}%</span></p>
      <p class="rounded-xl border border-white/10 bg-black/10 p-3"><span class="block text-muted-foreground">Production FPS vs. before training</span><span class="font-mono tabular-nums">{{ guardSample?.fps_ratio != null ? `${Math.round(guardSample.fps_ratio * 100)}%` : '–' }}</span><span class="text-muted-foreground"> · {{ guardSample?.total_fps ?? '–' }} fps · max frame age {{ guardSample?.max_frame_age ?? '–' }} s</span></p>
    </div>
    <p v-if="job.monitor?.last_reasons?.length && active" class="text-xs text-amber-200">Protection warning ({{ job.monitor.violations }}/{{ job.monitor.limits?.guard_samples }}): {{ job.monitor.last_reasons.join('; ') }}</p>

    <div class="grid gap-3 lg:grid-cols-2">
      <TrainingChart title="Loss" :points="job.metrics" :series="lossSeries" />
      <TrainingChart title="Validation metrics" :points="job.metrics" :series="metricSeries" />
    </div>

    <section v-if="job.status === 'completed'" class="space-y-4 rounded-2xl border border-white/10 bg-white/[0.02] p-4" aria-label="Results">
      <div class="flex flex-wrap items-center gap-2">
        <CircleCheck class="size-4 text-primary" /><h3 class="mr-auto text-sm font-semibold">Results</h3>
        <span v-if="job.best" class="text-xs text-muted-foreground">Best validation epoch {{ job.best.epoch }} · mAP50-95 {{ formatMetric(job.best.map50_95) }}</span>
      </div>
      <div v-if="job.evaluation?.status !== 'completed'" class="rounded-xl border border-amber-300/20 bg-amber-300/5 p-3 text-sm text-amber-200">Test comparison not available: {{ job.evaluation?.reason ?? 'not run' }}</div>
      <template v-else>
        <div :class="['rounded-xl border p-3 text-sm', job.evaluation.conclusive ? 'border-primary/20 bg-primary/5 text-primary' : 'border-amber-300/20 bg-amber-300/5 text-amber-200']">
          <p class="font-medium">{{ job.evaluation.conclusive ? 'Independent test set is large enough to compare.' : 'Not enough independent test data: do not treat the difference as a confirmed improvement.' }}</p>
          <ul class="mt-1 list-disc space-y-0.5 pl-5 text-xs opacity-90"><li v-for="caveat in job.evaluation.caveats ?? []" :key="caveat">{{ caveat }}</li></ul>
        </div>
        <div class="overflow-x-auto">
          <table class="w-full min-w-[40rem] text-left text-xs">
            <caption class="pb-2 text-left text-muted-foreground">Test split ({{ job.evaluation.metrics?.images }} images, {{ job.evaluation.metrics?.gt_boxes ?? '–' }} boxes) · class-agnostic vehicle · conf {{ job.evaluation.metrics?.conf ?? 0.25 }}</caption>
            <thead class="text-muted-foreground"><tr><th class="py-1.5 pr-3 font-medium">Model</th><th class="pr-3 font-medium">mAP50</th><th class="pr-3 font-medium">mAP50-95</th><th class="pr-3 font-medium">Precision</th><th class="pr-3 font-medium">Recall</th><th class="pr-3 font-medium">TP</th><th class="pr-3 font-medium">FP</th><th class="font-medium">Missed</th></tr></thead>
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
            <caption class="pb-2 text-left text-muted-foreground">New model, per class (test)</caption>
            <thead class="text-muted-foreground"><tr><th class="py-1.5 pr-3 font-medium">Class</th><th class="pr-3 font-medium">Precision</th><th class="pr-3 font-medium">Recall</th><th class="pr-3 font-medium">mAP50</th><th class="font-medium">mAP50-95</th></tr></thead>
            <tbody class="font-mono tabular-nums"><tr v-for="(value, name) in perClass" :key="name" class="border-t border-white/5"><td class="py-1.5 pr-3 font-sans">{{ name }}</td><td class="pr-3">{{ formatMetric(value.precision) }}</td><td class="pr-3">{{ formatMetric(value.recall) }}</td><td class="pr-3">{{ formatMetric(value.mAP50) }}</td><td>{{ formatMetric(value['mAP50-95']) }}</td></tr></tbody>
          </table>
        </div>
        <TrainingComparison v-if="job.has_comparison" :job-id="job.id" />
      </template>
    </section>

    <div v-if="downloads.length" class="flex flex-wrap items-center gap-2">
      <span class="text-xs text-muted-foreground">Files</span>
      <Button v-for="[name, meta] in downloads" :key="name" size="sm" variant="outline" :disabled="!canControl" :title="canControl ? `${name} · ${(meta.size / 1048576).toFixed(1)} MiB` : 'Unlock training control to download'" @click="emit('download', name, meta.download_url)"><Download /> {{ name }}</Button>
    </div>

    <details class="group rounded-xl border border-white/10 bg-black/20" :open="active || job.status === 'failed'">
      <summary class="cursor-pointer px-3 py-2 text-xs font-medium text-muted-foreground">Training log ({{ job.log_tail.length }} recent lines)</summary>
      <pre ref="logElement" class="max-h-72 overflow-auto px-3 pb-3 font-mono text-[0.7rem] leading-relaxed whitespace-pre-wrap text-muted-foreground">{{ job.log_tail.join('\n') || 'No log output yet.' }}</pre>
    </details>
  </div>
</template>
