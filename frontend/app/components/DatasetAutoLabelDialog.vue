<script setup lang="ts">
import { LoaderCircle, Sparkles } from '@lucide/vue'
import DatasetSelect from '~/components/DatasetSelect.vue'
import { Button } from '~/components/ui/button'
import { Input } from '~/components/ui/input'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '~/components/ui/dialog'
import { trainingRequest, useTrainingToken } from '~/composables/trainingApi'
import { autoLabelErrorMessage, startAutoLabel } from '~/composables/autolabelApi'
import { useI18n } from '~/composables/useI18n'
import { AUTO_LABEL_CONF, readAutoLabelSettings, saveAutoLabelSettings } from '~/lib/datasetAutoLabel'
import { demoModels, demoPlan } from '~/lib/autolabelDemo'
import type { AutoLabelJob, AutoLabelPlan } from '~/types/dataset'

// Starts a GPU auto-label job on the training host. In batch mode only unlabeled pending images are proposed;
// reviewed or hand-labeled images are never touched. With imageId it proposes boxes for that one image
// (apply: false) and the editor merges them into its draft. Every proposal still needs a person's review.
// The chosen model/confidence is remembered so later runs start with one click.
const props = defineProps<{ open: boolean; cameraId?: string; cameraOptions?: { value: string; label: string }[]; imageId?: string; demo?: boolean; pending?: number }>()
const emit = defineEmits<{ 'update:open': [open: boolean]; started: [job: AutoLabelJob] }>()
const { t } = useI18n()
const { token, setToken } = useTrainingToken()

interface ModelRow { id: string; kind: string; label: string; class_mode: string; status: string }
const models = ref<ModelRow[]>([])
const modelId = ref(''), scope = ref('all'), conf = ref<number>(AUTO_LABEL_CONF.default), redoAuto = ref(false)
const tokenInput = ref('')
const plan = ref<AutoLabelPlan | null>(null)
const loadingPlan = ref(false), starting = ref(false)
const error = ref<string | null>(null)

const modelOptions = computed(() => models.value.filter(model => model.status === 'available').map(model => ({
  value: model.id,
  label: model.kind === 'base' ? t('YOLO11m pretrained (COCO, production)') : `${model.label} · ${model.class_mode === 'single' ? t('single class') : 'car/truck/bus'}`,
})))
const single = computed(() => !!props.imageId)
const scopeOptions = computed(() => [{ value: 'all', label: t('All cameras') }, ...(props.cameraOptions ?? [])])
const needsToken = computed(() => !props.demo && !token.value)
const canStart = computed(() => !!modelId.value && (single.value || (!!plan.value?.ready && plan.value.eligible > 0)) && (!needsToken.value || !!tokenInput.value) && !starting.value)

async function loadModels() {
  try {
    models.value = props.demo ? demoModels : await trainingRequest<ModelRow[]>('/models')
    const saved = readAutoLabelSettings()
    if (saved && modelOptions.value.some(option => option.value === saved.model_id)) { modelId.value = saved.model_id; conf.value = saved.conf }
    if (!modelOptions.value.some(option => option.value === modelId.value)) {
      // Prefer the newest fine-tuned model; the production COCO model is the fallback.
      modelId.value = modelOptions.value.find(option => models.value.find(m => m.id === option.value)?.kind === 'finetuned')?.value ?? modelOptions.value[0]?.value ?? ''
    }
  } catch (cause) { error.value = (cause as Error).message }
}
let planVersion = 0
async function loadPlan() {
  if (!props.open || !modelId.value || single.value) { plan.value = null; return }
  const version = ++planVersion
  loadingPlan.value = true
  try {
    const query = new URLSearchParams({ model_id: modelId.value, include_auto_drafts: String(redoAuto.value) })
    if (scope.value !== 'all') query.set('camera_id', scope.value)
    const data = props.demo ? demoPlan(props.pending ?? 0) : await trainingRequest<AutoLabelPlan>('/autolabels/plan?' + query)
    if (version === planVersion) { plan.value = data; error.value = null }
  } catch (cause) {
    if (version !== planVersion) return
    plan.value = null
    error.value = autoLabelErrorMessage(cause)
  }
  finally { if (version === planVersion) loadingPlan.value = false }
}
async function start() {
  if (!canStart.value) return
  saveAutoLabelSettings({ model_id: modelId.value, conf: conf.value })
  if (props.demo) {
    emit('started', { id: 'demo-job', status: 'queued', phase: 'queued', message: null, progress: 0, error: null, config: { kind: 'autolabel', conf: conf.value, images: single.value ? 1 : plan.value!.eligible }, dataset_summary: null, created_at: new Date().toISOString() })
    emit('update:open', false)
    return
  }
  starting.value = true; error.value = null
  if (!token.value) setToken(tokenInput.value)
  try {
    const job = await startAutoLabel(props.imageId
      ? { model_id: modelId.value, conf: conf.value, image_ids: [props.imageId], apply: false }
      : { model_id: modelId.value, camera_id: scope.value === 'all' ? null : scope.value, conf: conf.value, include_auto_drafts: redoAuto.value })
    tokenInput.value = ''
    emit('started', job)
    emit('update:open', false)
  } catch (cause) {
    if ((cause as { status?: number }).status === 401) setToken('')
    error.value = autoLabelErrorMessage(cause)
  } finally { starting.value = false }
}

watch(() => props.open, (open) => {
  if (!open) return
  error.value = null
  scope.value = props.cameraId || 'all'
  void loadModels().then(loadPlan)
})
watch([modelId, scope, redoAuto], () => { void loadPlan() })
</script>

<template>
  <Dialog :open="open" @update:open="!starting && emit('update:open', $event)">
    <DialogContent :show-close-button="!starting" class="gap-5 border border-white/10 bg-popover/95 p-6 shadow-xl backdrop-blur-xl sm:max-w-lg">
      <DialogHeader>
        <DialogTitle class="flex items-center gap-2"><Sparkles class="size-4 text-primary" /> {{ single ? t('Auto-label this image') : t('Auto-label with a model') }}</DialogTitle>
        <DialogDescription>{{ single ? t('The model adds draft boxes to this image. Boxes already here are kept; check the drafts, then save.') : t('The model draws draft boxes on unlabeled images. Drafts are not exported until you check and save each image.') }}</DialogDescription>
      </DialogHeader>
      <div class="space-y-4">
        <div class="grid gap-1.5"><span class="text-xs font-medium text-muted-foreground">{{ t('Model') }}</span><DatasetSelect v-model="modelId" class="w-full" :options="modelOptions" :label="t('Model')" :placeholder="t('No usable models')" :disabled="starting" /></div>
        <div v-if="!single" class="grid gap-1.5"><span class="text-xs font-medium text-muted-foreground">{{ t('Images') }}</span><DatasetSelect v-model="scope" class="w-full" :options="scopeOptions" :label="t('Cameras to auto-label')" :disabled="starting" /></div>
        <div class="grid gap-1.5">
          <div class="flex items-center justify-between text-xs"><label for="autolabel-conf" class="font-medium text-muted-foreground">{{ t('Minimum confidence') }}</label><span class="font-mono tabular-nums">{{ Math.round(conf * 100) }}%</span></div>
          <input id="autolabel-conf" v-model.number="conf" type="range" :min="AUTO_LABEL_CONF.min" :max="AUTO_LABEL_CONF.max" :step="AUTO_LABEL_CONF.step" class="w-full accent-primary" :disabled="starting">
          <p class="text-[11px] leading-relaxed text-muted-foreground">{{ t('Lower finds more vehicles but adds more wrong boxes to delete.') }}</p>
        </div>
        <label v-if="!single" class="flex items-start gap-2 text-xs leading-relaxed"><input v-model="redoAuto" type="checkbox" class="mt-0.5 accent-primary" :disabled="starting">{{ t('Also redo images that only have unedited auto-label drafts') }}</label>
        <p v-if="single && !modelOptions.length" class="text-xs text-amber-200/80">{{ t('No auto-label model is available yet.') }}</p>
        <div v-if="!single" class="rounded-lg border border-white/10 bg-black/20 px-3 py-2 text-xs leading-relaxed" role="status">
          <p v-if="!modelOptions.length && !loadingPlan" class="text-amber-200/80">{{ t('No auto-label model is available yet.') }}</p>
          <p v-else-if="loadingPlan" class="text-muted-foreground">{{ t('Counting images…') }}</p>
          <template v-else-if="plan">
            <p>{{ t('{count} images will get draft boxes.', { count: plan.eligible }) }}</p>
            <p class="text-muted-foreground">{{ t('Untouched: {reviewed} reviewed · {labeled} with your labels', { reviewed: plan.skipped.reviewed, labeled: plan.skipped.labeled }) }}</p>
            <p v-if="plan.eligible > plan.max_images" class="text-amber-200/80">{{ t('Only the oldest {count} run in one job.', { count: plan.max_images }) }}</p>
            <p v-if="plan.blocker" class="text-amber-200/80">{{ t(plan.blocker) }}</p>
          </template>
        </div>
        <div v-if="needsToken" class="grid gap-1.5"><label for="autolabel-token" class="text-xs font-medium text-muted-foreground">{{ t('Auto-labeling uses the training GPU and needs the training admin token.') }}</label><Input id="autolabel-token" v-model="tokenInput" type="password" autocomplete="off" :placeholder="t('Admin token')" class="h-9" :disabled="starting" /></div>
        <p class="text-[11px] leading-relaxed text-muted-foreground">{{ t('These settings are remembered: next time the auto-label button starts right away.') }}</p>
        <p v-if="error" class="text-sm text-red-300" role="alert">{{ t(error) }}</p>
      </div>
      <DialogFooter class="-mx-6 -mb-6 px-6">
        <Button variant="outline" :disabled="starting" @click="emit('update:open', false)">{{ t('Cancel') }}</Button>
        <Button :disabled="!canStart" @click="start"><LoaderCircle v-if="starting" class="animate-spin" /><Sparkles v-else /> {{ single ? t('Auto-label this image') : t('Start auto-labeling') }}</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
