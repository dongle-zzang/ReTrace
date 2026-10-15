<script setup lang="ts">
import { AlertTriangle, Ban, Camera, LoaderCircle, Play } from '@lucide/vue'
import DatasetSelect from '~/components/DatasetSelect.vue'
import { Button } from '~/components/ui/button'
import { trainingRequest } from '~/composables/trainingApi'
import { useI18n } from '~/composables/useI18n'
import { formatMetric } from '~/lib/training'
import type { EvaluationPlan, TrainingDataset, TrainingJobDetail, TrainingModel } from '~/types/training'

// Scores a registered model on newly captured CCTV images. The Backend leaves out every image the
// model (or its base) trained on and every frame from those capture sessions, so the result is an
// independent check, which a Train All run cannot give itself.
const props = defineProps<{ models: TrainingModel[]; datasets: TrainingDataset[]; canControl: boolean; busy: boolean; gpuReady: boolean }>()
const modelId = defineModel<string>('modelId', { default: '' })
const emit = defineEmits<{ created: [job: TrainingJobDetail] }>()

const { t } = useI18n()
const exportId = ref('')
const plan = ref<EvaluationPlan | null>(null)
const planning = ref(false)
const planError = ref<string | null>(null)
const acknowledge = ref(false)
const starting = ref(false)
const startError = ref<string | null>(null)

const evaluable = computed(() => props.models.filter(model => model.kind === 'finetuned' && model.status === 'available'))
const modelOptions = computed(() => evaluable.value.map(model => ({
  value: model.id, label: `${model.label}${model.config?.train_mode === 'all' ? ` · ${t('Train All')}` : ''}`,
})))
// Newest export first: new captures only appear in an export made after them.
const datasetOptions = computed(() => props.datasets.map(item => ({ value: item.id, label: `${item.name} · ${t('{count} images', { count: item.counts?.total ?? 0 })}` })))
watch(evaluable, (list) => { if (!list.some(model => model.id === modelId.value)) modelId.value = list[0]?.id ?? '' }, { immediate: true })
watch(() => props.datasets, (list) => { if (!list.some(item => item.id === exportId.value)) exportId.value = list[0]?.id ?? '' }, { immediate: true })

let version = 0
async function loadPlan() {
  const current = ++version
  plan.value = null; planError.value = null; acknowledge.value = false
  if (!modelId.value || !exportId.value) return
  planning.value = true
  try {
    const data = await trainingRequest<EvaluationPlan>(`/evaluations/plan?model_id=${encodeURIComponent(modelId.value)}&export_id=${exportId.value}`)
    if (current === version) plan.value = data
  } catch (cause) { if (current === version) planError.value = (cause as Error).message }
  finally { if (current === version) planning.value = false }
}
watch([modelId, exportId], loadPlan, { immediate: true })

const selectedModel = computed(() => evaluable.value.find(model => model.id === modelId.value))
const latest = computed(() => selectedModel.value?.metrics?.new_images?.at(-1) ?? null)
const canStart = computed(() => props.canControl && !props.busy && props.gpuReady && !starting.value && !!plan.value
  && plan.value.readiness !== 'blocked' && (plan.value.readiness === 'ready' || acknowledge.value))
const blocker = computed(() => !props.canControl ? t('Unlock training control first')
  : props.busy ? t('Another job is queued or running.') : !props.gpuReady ? t('The GPU is not ready.') : null)

const { confirm } = useConfirmDialog()
async function start() {
  if (!canStart.value || !plan.value) return
  if (!await confirm({ title: t('Evaluate on new images?'), description: t('{count} new images on the shared GPU: the model and the current YOLO11m are scored on the same images. Nothing is trained or deployed.', { count: plan.value.eligible }), confirmLabel: t('Start evaluation') })) return
  starting.value = true; startError.value = null
  try {
    const job = await trainingRequest<TrainingJobDetail>('/evaluations', { method: 'POST', auth: true, body: { model_id: modelId.value, export_id: exportId.value, acknowledge_limits: acknowledge.value } })
    emit('created', job)
  } catch (cause) { startError.value = (cause as Error).message }
  finally { starting.value = false }
}
</script>

<template>
  <section class="space-y-4 rounded-2xl border border-white/10 bg-white/[0.03] p-5" :aria-label="t('Evaluate on new CCTV images')">
    <div class="space-y-1">
      <h2 class="flex items-center gap-2 text-sm font-semibold"><Camera class="size-4 text-primary" /> {{ t('Evaluate on new CCTV images') }}</h2>
      <p class="text-xs text-muted-foreground">{{ t('Capture and label new images in AI Datasets, then create a new export. Images the model trained on, and frames from the same capture sessions, are left out automatically, so the score is independent.') }}</p>
    </div>
    <p v-if="!evaluable.length" class="rounded-xl border border-dashed border-white/15 py-6 text-center text-sm text-muted-foreground">{{ t('No fine-tuned model yet. Train one first.') }}</p>
    <template v-else>
      <div class="grid gap-3 sm:grid-cols-2">
        <div class="space-y-1.5"><span class="text-xs text-muted-foreground">{{ t('Model to evaluate') }}</span><DatasetSelect v-model="modelId" :options="modelOptions" :label="t('Model to evaluate')" class="w-full" /></div>
        <div class="space-y-1.5"><span class="text-xs text-muted-foreground">{{ t('Dataset with new images') }}</span><DatasetSelect v-model="exportId" :options="datasetOptions" :label="t('Dataset with new images')" class="w-full" :placeholder="t('No dataset exports')" :disabled="!datasets.length" /></div>
      </div>
      <p v-if="planning" class="text-sm text-muted-foreground" role="status"><LoaderCircle class="mr-2 inline size-4 animate-spin" />{{ t('Finding images the model has never seen…') }}</p>
      <p v-else-if="planError" class="text-sm text-red-300" role="alert">{{ t(planError) }}</p>
      <template v-else-if="plan">
        <dl class="grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
          <div class="rounded-xl border border-white/10 bg-black/10 p-3"><dt class="text-muted-foreground">{{ t('New images') }}</dt><dd class="font-mono text-lg tabular-nums text-foreground">{{ plan.eligible }}</dd></div>
          <div class="rounded-xl border border-white/10 bg-black/10 p-3"><dt class="text-muted-foreground">{{ t('Left out: trained on') }}</dt><dd class="font-mono text-lg tabular-nums">{{ plan.excluded_trained }}</dd></div>
          <div class="rounded-xl border border-white/10 bg-black/10 p-3"><dt class="text-muted-foreground">{{ t('Left out: same capture session') }}</dt><dd class="font-mono text-lg tabular-nums">{{ plan.excluded_same_session }}</dd></div>
          <div class="rounded-xl border border-white/10 bg-black/10 p-3"><dt class="text-muted-foreground">{{ t('Sessions · cameras · boxes') }}</dt><dd class="font-mono text-lg tabular-nums">{{ plan.sessions }} · {{ plan.cameras }} · {{ plan.boxes }}</dd></div>
        </dl>
        <ul v-if="plan.errors.length" class="space-y-1 text-xs text-red-300"><li v-for="item in plan.errors" :key="item" class="flex gap-2"><Ban class="mt-0.5 size-3.5 shrink-0" />{{ item }}</li></ul>
        <ul v-if="plan.limits.length" class="space-y-1 text-xs text-amber-200"><li v-for="item in plan.limits" :key="item" class="flex gap-2"><AlertTriangle class="mt-0.5 size-3.5 shrink-0" />{{ item }}</li></ul>
        <label v-if="plan.readiness === 'limited'" class="flex items-start gap-2 rounded-xl border border-amber-300/20 bg-amber-300/5 p-3 text-xs text-amber-100"><input v-model="acknowledge" type="checkbox" class="mt-0.5 accent-amber-300"> {{ t('I understand these limitations. The result is a first check, not a confirmed improvement.') }}</label>
      </template>
      <div class="flex flex-wrap items-center gap-3">
        <Button :disabled="!canStart" @click="start"><LoaderCircle v-if="starting" class="animate-spin" /><Play v-else /> {{ t('Start evaluation') }}</Button>
        <p v-if="startError" class="text-sm text-red-300" role="alert">{{ t(startError) }}</p>
        <p v-else-if="blocker" class="text-xs text-muted-foreground">{{ blocker }}</p>
      </div>
      <p v-if="latest" class="text-xs text-muted-foreground">
        {{ t('Last new-image result for this model: {images} images, mAP50 {map} (current YOLO11m {baseline}), recall {recall}.', { images: latest.images ?? '–', map: formatMetric(latest.new?.vehicle?.mAP50), baseline: formatMetric(latest.baseline?.vehicle?.mAP50), recall: formatMetric(latest.new?.operating_point?.recall) }) }}
        <span v-if="!latest.conclusive" class="text-amber-200">{{ t('Not enough images to confirm an improvement.') }}</span>
      </p>
    </template>
  </section>
</template>
