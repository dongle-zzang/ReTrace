<script setup lang="ts">
import { useIntervalFn } from '@vueuse/core'
import { AlertTriangle, Ban, CircleCheck, CircleDashed, Cpu, Database, Download, KeyRound, LoaderCircle, Lock, Play, RefreshCw, Rocket, Server, SlidersHorizontal } from '@lucide/vue'
import DatasetSelect from '~/components/DatasetSelect.vue'
import TrainingJobPanel from '~/components/TrainingJobPanel.vue'
import { Badge } from '~/components/ui/badge'
import { Button } from '~/components/ui/button'
import { Input } from '~/components/ui/input'
import { downloadTrainingFile, trainingRequest, useTrainingToken } from '~/composables/trainingApi'
import { estimateVramMib, formatDuration, formatMetric, formatMib, isActiveStatus, requiredFreeMib, safeBatch, toInteger, validateTrainingForm } from '~/lib/training'
import { CLASS_MODE_LABELS, JOB_STATUS_LABELS, READINESS_LABELS } from '~/types/training'
import type { ClassMode, DatasetCheck, ImageSize, TrainingDataset, TrainingJob, TrainingJobDetail, TrainingModel, TrainingOptions, TrainingStatus } from '~/types/training'

useHead({ title: 'ReTrace · AI training' })

const options = ref<TrainingOptions | null>(null)
const status = ref<TrainingStatus | null>(null)
const datasets = ref<TrainingDataset[]>([])
const check = ref<DatasetCheck | null>(null)
const models = ref<TrainingModel[]>([])
const jobs = ref<TrainingJob[]>([])
const selectedJobId = ref<string | null>(null)
const detail = ref<TrainingJobDetail | null>(null)
const loading = ref(true)
const checking = ref(false)
const starting = ref(false)
const stopping = ref(false)
const error = ref<string | null>(null)
const startError = ref<string | null>(null)
const notice = ref<string | null>(null)

const form = reactive({
  exportId: '', baseModelId: 'yolo11m-coco', name: 'yolo11m-ft', classMode: 'vehicle3' as ClassMode,
  epochs: 50 as number | string, imgsz: 640 as ImageSize, batch: 4 as number | string, seed: 42 as number | string,
  patience: 15 as number | string, smoke: false, acknowledge: false,
})
const advanced = ref(false)

// ----- access ---------------------------------------------------------------------------
const { token, setToken } = useTrainingToken()
const tokenInput = ref('')
const unlocked = ref(false)
const unlocking = ref(false)
const authError = ref<string | null>(null)
async function verifyToken(value: string) {
  if (!value) return
  unlocking.value = true; authError.value = null
  setToken(value)
  try { await trainingRequest('/auth', { method: 'POST', auth: true }); unlocked.value = true; tokenInput.value = '' }
  catch (cause) { unlocked.value = false; setToken(''); authError.value = (cause as Error).message }
  finally { unlocking.value = false }
}
function lock() { setToken(''); unlocked.value = false }

// ----- data loading ---------------------------------------------------------------------
async function loadStatus() {
  const batch = toInteger(form.batch)
  const query = `?imgsz=${form.imgsz}&batch=${Number.isInteger(batch) && batch >= 1 && batch <= 32 ? batch : 4}`
  try { status.value = await trainingRequest<TrainingStatus>('/status' + query) } catch (cause) { error.value = (cause as Error).message }
}
async function loadJobs() {
  try {
    jobs.value = await trainingRequest<TrainingJob[]>('/jobs')
    if (!selectedJobId.value && jobs.value.length) selectedJobId.value = (jobs.value.find(job => isActiveStatus(job.status)) ?? jobs.value[0])!.id
  } catch (cause) { error.value = (cause as Error).message }
}
let detailVersion = 0
async function loadDetail() {
  const id = selectedJobId.value
  if (!id) { detail.value = null; return }
  const version = ++detailVersion
  try {
    const data = await trainingRequest<TrainingJobDetail>(`/jobs/${id}`)
    if (version === detailVersion) detail.value = data
  } catch (cause) { if (version === detailVersion) error.value = (cause as Error).message }
}
async function loadDatasets() {
  try {
    datasets.value = await trainingRequest<TrainingDataset[]>('/datasets')
    if (!datasets.value.some(item => item.id === form.exportId)) form.exportId = datasets.value.find(item => item.readiness !== 'blocked')?.id ?? datasets.value[0]?.id ?? ''
  } catch (cause) { error.value = (cause as Error).message }
}
async function loadModels() {
  try { models.value = await trainingRequest<TrainingModel[]>('/models') } catch (cause) { error.value = (cause as Error).message }
}
async function refreshAll() {
  error.value = null
  await Promise.all([loadStatus(), loadJobs(), loadDatasets(), loadModels()])
  await loadDetail()
}
let checkVersion = 0
watch(() => form.exportId, async (id) => {
  check.value = null; form.acknowledge = false
  if (!id) return
  const version = ++checkVersion
  checking.value = true
  try {
    const data = await trainingRequest<DatasetCheck>(`/datasets/${id}/check`)
    if (version === checkVersion) check.value = data
  } catch (cause) { if (version === checkVersion) error.value = (cause as Error).message }
  finally { if (version === checkVersion) checking.value = false }
})
watch(selectedJobId, () => { detail.value = null; void loadDetail() })
watch(() => [form.imgsz, form.batch], () => { void loadStatus() })

const activeJob = computed(() => status.value?.active_job ?? jobs.value.find(job => isActiveStatus(job.status)) ?? null)
useIntervalFn(() => { void loadStatus() }, 5000)
useIntervalFn(() => {
  if (activeJob.value || (detail.value && isActiveStatus(detail.value.status))) { void loadDetail(); void loadJobs() }
}, 3000)
useIntervalFn(() => { void loadJobs(); void loadModels() }, 15000)
watch(() => activeJob.value?.id, (id, previous) => {
  if (id) selectedJobId.value = id
  if (previous && !id) { void loadModels(); void loadDetail() }
})

onMounted(async () => {
  try { options.value = await trainingRequest<TrainingOptions>('/options') } catch (cause) { error.value = (cause as Error).message }
  if (options.value) Object.assign(form, { epochs: options.value.defaults.epochs, imgsz: options.value.defaults.imgsz, batch: options.value.defaults.batch, seed: options.value.defaults.seed, patience: options.value.defaults.patience, classMode: options.value.defaults.class_mode })
  await refreshAll()
  loading.value = false
  if (token.value && options.value?.control_enabled) void verifyToken(token.value)
})

// ----- form & pre-start checks ------------------------------------------------------------
const baseModel = computed(() => models.value.find(model => model.id === form.baseModelId))
const baseOptions = computed(() => models.value.filter(model => model.status === 'available').map(model => ({
  value: model.id, label: model.kind === 'base' ? 'YOLO11m pretrained (COCO, production)' : `${model.label} · ${model.class_mode === 'single' ? 'single class' : 'car/truck/bus'}`,
})))
const datasetOptions = computed(() => datasets.value.map(item => ({ value: item.id, label: `${item.name} · ${item.counts?.total ?? 0} images · ${READINESS_LABELS[item.readiness]}` })))
const formErrors = computed(() => {
  if (!options.value) return []
  const errors = validateTrainingForm({ epochs: form.epochs, imgsz: form.imgsz, batch: form.batch, seed: form.seed, patience: form.patience }, options.value.limits)
  if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,47}$/.test(form.name)) errors.push('Model name: 1–48 letters, digits, ".", "_" or "-", starting with a letter or digit.')
  if (baseModel.value?.kind === 'finetuned' && form.classMode === 'vehicle3' && baseModel.value.class_mode !== 'vehicle3')
    errors.push('car/truck/bus training needs YOLO11m or a model that was trained in car/truck/bus mode.')
  return errors
})
const batchNumber = computed(() => toInteger(form.batch))
const estimate = computed(() => Number.isInteger(batchNumber.value) && batchNumber.value > 0 ? estimateVramMib(form.imgsz, batchNumber.value, options.value?.gpu_policy.vram_estimate) : null)
const needed = computed(() => options.value && estimate.value !== null ? requiredFreeMib(form.imgsz, batchNumber.value, options.value.gpu_policy) : null)
const freeMib = computed(() => status.value?.worker.gpu?.free_mib)
const suggestedBatch = computed(() => options.value && typeof freeMib.value === 'number' ? safeBatch(form.imgsz, freeMib.value, options.value.gpu_policy) : null)
const readiness = computed(() => check.value?.readiness)
type CheckState = 'ok' | 'warn' | 'fail' | 'pending'
const checks = computed<{ label: string; state: CheckState; detail: string }[]>(() => [
  { label: 'Dataset', state: checking.value || !check.value ? 'pending' : readiness.value === 'ready' ? 'ok' : readiness.value === 'limited' ? (form.acknowledge ? 'ok' : 'warn') : 'fail',
    detail: !form.exportId ? 'Select a dataset' : checking.value ? 'Checking labels, splits and sessions…' : readiness.value === 'blocked' ? check.value?.errors[0] ?? 'Not usable' : readiness.value === 'limited' ? `${check.value?.limits.length} limitation(s) need confirmation` : 'Valid' },
  { label: 'GPU', state: !status.value ? 'pending' : status.value.gpu_ready ? 'ok' : 'fail', detail: status.value?.gpu_blocker ?? (needed.value ? `${formatMib(freeMib.value)} free, needs ${formatMib(needed.value)}` : '') },
  { label: 'Production stream', state: !status.value ? 'pending' : status.value.production.preview === 'reachable' ? 'ok' : options.value?.gpu_policy.require_preview ? 'fail' : 'warn',
    detail: status.value ? `Preview ${status.value.production.preview} · ${status.value.production.cameras_online} cameras online · ${status.value.production.total_fps} fps` : '' },
  { label: 'Settings', state: formErrors.value.length ? 'fail' : 'ok', detail: formErrors.value[0] ?? 'Valid' },
  { label: 'Access', state: unlocked.value ? 'ok' : 'fail', detail: !options.value?.control_enabled ? 'Training control is disabled on the server' : unlocked.value ? 'Unlocked for this tab' : 'Enter the training admin token' },
  { label: 'Queue', state: activeJob.value ? 'fail' : 'ok', detail: activeJob.value ? `${activeJob.value.name} is ${JOB_STATUS_LABELS[activeJob.value.status].toLowerCase()}` : 'No other training job' },
])
const canStart = computed(() => !starting.value && checks.value.every(item => item.state === 'ok' || (item.label === 'Production stream' && item.state === 'warn')))

const { confirm } = useConfirmDialog()
async function start() {
  if (!canStart.value) return
  const full = !form.smoke
  if (!await confirm({ title: full ? 'Start GPU training?' : 'Start smoke test?', description: `${full ? `${toInteger(form.epochs)} epochs` : '1 epoch'} at ${form.imgsz} px, batch ${toInteger(form.batch)} on the shared GPU. Production inference keeps priority: the job waits or stops if the GPU or camera FPS needs protecting.`, confirmLabel: full ? 'Start training' : 'Start smoke test' })) return
  starting.value = true; startError.value = null; notice.value = null
  try {
    const job = await trainingRequest<TrainingJobDetail>('/jobs', { method: 'POST', auth: true, body: {
      export_id: form.exportId, base_model_id: form.baseModelId, name: form.name, class_mode: form.classMode,
      epochs: toInteger(form.epochs), imgsz: form.imgsz, batch: toInteger(form.batch), seed: toInteger(form.seed),
      patience: toInteger(form.patience), device: 'gpu', smoke: form.smoke, acknowledge_limits: form.acknowledge,
    } })
    selectedJobId.value = job.id
    detail.value = job
    notice.value = 'Training job created. The GPU worker picks it up within a few seconds.'
    await Promise.all([loadJobs(), loadStatus()])
  } catch (cause) {
    startError.value = (cause as Error).message
    if ((cause as { status?: number }).status === 401) unlocked.value = false
  } finally { starting.value = false }
}
async function stop() {
  const job = detail.value
  if (!job || !await confirm({ title: 'Stop training?', description: 'Training stops after the current batch, then validates and saves last.pt/best.pt. A stopped run is not registered as a model.', confirmLabel: 'Stop training', destructive: true })) return
  stopping.value = true
  try { await trainingRequest(`/jobs/${job.id}/stop`, { method: 'POST', auth: true }); await loadDetail() }
  catch (cause) { error.value = (cause as Error).message }
  finally { stopping.value = false }
}
async function download(name: string, url: string) {
  try { await downloadTrainingFile(url, name) } catch (cause) { error.value = (cause as Error).message }
}

const time = (value: string | null) => value ? new Date(value).toLocaleString() : '–'
const stateIcon = { ok: CircleCheck, warn: AlertTriangle, fail: Ban, pending: CircleDashed }
const stateClass = { ok: 'text-primary', warn: 'text-amber-300', fail: 'text-red-300', pending: 'text-muted-foreground' }
const readinessClass = { ready: 'border-primary/25 bg-primary/10 text-primary', limited: 'border-amber-300/25 bg-amber-300/10 text-amber-200', blocked: 'border-red-400/25 bg-red-400/10 text-red-300' }
const jobStatusClass = (value: string) => ({ completed: 'text-primary', failed: 'text-red-300', running: 'text-sky-300', stopping: 'text-amber-200', queued: 'text-amber-200' } as Record<string, string>)[value] ?? 'text-muted-foreground'
const gpu = computed(() => status.value?.worker.gpu)
const gpuPercent = computed(() => gpu.value?.total_mib ? Math.round(((gpu.value.used_mib ?? 0) / gpu.value.total_mib) * 100) : 0)
const splitRows = computed(() => (['train', 'val', 'test'] as const).map(split => ({ split, images: check.value?.counts?.[split] ?? 0, boxes: check.value?.boxes?.[split] ?? 0, sessions: check.value?.sessions?.[split] ?? 0 })))
const fileSize = (bytes?: number) => typeof bytes === 'number' ? `${(bytes / 1048576).toFixed(1)} MiB` : ''
</script>

<template>
  <main class="relative mx-auto max-w-[1600px] space-y-6 px-3 py-6 sm:px-6">
    <header class="flex flex-wrap items-end gap-4">
      <div class="mr-auto space-y-2"><p class="text-xs font-semibold uppercase tracking-[0.2em] text-primary">AI training workspace</p><h1 class="text-2xl font-semibold tracking-tight">Fine-tune. Compare. Version.</h1><p class="text-sm text-muted-foreground">Fine-tune YOLO11m on a reviewed dataset on the GPU server. Production models are never replaced automatically.</p></div>
      <Button variant="ghost" size="sm" :disabled="loading" @click="refreshAll"><RefreshCw :class="loading ? 'animate-spin' : ''" /> Refresh</Button>
    </header>
    <div v-if="error" class="flex flex-wrap items-center gap-3 rounded-xl border border-red-400/20 bg-red-400/5 p-4 text-sm text-red-300" role="alert"><span class="min-w-0 flex-1">{{ error }}</span><Button size="sm" variant="ghost" @click="refreshAll"><RefreshCw /> Retry</Button></div>

    <!-- GPU / worker / production / active job -->
    <section class="grid gap-3 sm:grid-cols-2 xl:grid-cols-4" aria-label="Training resources">
      <article class="space-y-2 rounded-2xl border border-white/10 bg-white/[0.03] p-4">
        <h2 class="flex items-center gap-2 text-xs font-semibold text-muted-foreground"><Server class="size-4 text-primary" /> GPU worker</h2>
        <p v-if="!status" class="text-sm text-muted-foreground">Loading…</p>
        <template v-else><p class="flex items-center gap-2 text-sm font-medium"><span :class="['size-2 rounded-full', status.worker.online ? 'bg-primary' : 'bg-red-400']" />{{ status.worker.online ? 'Online' : 'Offline' }}</p>
          <p class="text-xs text-muted-foreground">{{ status.worker.online ? `Last report ${status.worker.age_seconds}s ago` : 'Start training/worker.py on the GPU server.' }}</p>
          <p v-if="status.worker.online && !status.worker.base_weights?.present" class="text-xs text-red-300">training/weights/yolo11m.pt is missing.</p></template>
      </article>
      <article class="space-y-2 rounded-2xl border border-white/10 bg-white/[0.03] p-4">
        <h2 class="flex items-center gap-2 text-xs font-semibold text-muted-foreground"><Cpu class="size-4 text-primary" /> GPU {{ gpu?.name ? `· ${gpu.name}` : '' }}</h2>
        <template v-if="gpu?.available"><p class="font-mono text-sm tabular-nums">{{ formatMib(gpu.used_mib) }} / {{ formatMib(gpu.total_mib) }} · {{ gpu.util }}%</p>
          <div class="h-1.5 overflow-hidden rounded-full bg-white/10"><div :class="['h-full rounded-full', gpuPercent > 85 ? 'bg-red-400' : gpuPercent > 65 ? 'bg-amber-300' : 'bg-primary']" :style="{ width: `${gpuPercent}%` }" /></div>
          <p class="text-xs text-muted-foreground">{{ formatMib(gpu.free_mib) }} free · DeepStream reserve {{ formatMib(options?.gpu_policy.reserve_mib) }}</p></template>
        <p v-else class="text-sm text-muted-foreground">{{ gpu?.error ?? 'No GPU report' }}</p>
      </article>
      <article class="space-y-2 rounded-2xl border border-white/10 bg-white/[0.03] p-4">
        <h2 class="flex items-center gap-2 text-xs font-semibold text-muted-foreground"><Rocket class="size-4 text-primary" /> Production inference</h2>
        <template v-if="status"><p class="flex items-center gap-2 text-sm font-medium"><span :class="['size-2 rounded-full', status.production.preview === 'reachable' ? 'bg-primary' : 'bg-red-400']" />Preview {{ status.production.preview }}</p>
          <p class="text-xs text-muted-foreground">{{ status.production.cameras_online }}/{{ status.production.cameras_enabled }} cameras online · {{ status.production.total_fps }} fps · parking {{ status.production.parking_cameras_online }} cams {{ status.production.parking_fps }} fps</p></template>
      </article>
      <article class="space-y-2 rounded-2xl border border-white/10 bg-white/[0.03] p-4">
        <h2 class="flex items-center gap-2 text-xs font-semibold text-muted-foreground"><Play class="size-4 text-primary" /> Running job</h2>
        <template v-if="activeJob"><button type="button" class="block text-left text-sm font-medium hover:text-primary" @click="selectedJobId = activeJob.id">{{ activeJob.name }} · {{ JOB_STATUS_LABELS[activeJob.status] }}</button>
          <p class="text-xs text-muted-foreground">Epoch {{ activeJob.epoch }}/{{ activeJob.epochs }} · {{ Math.round(activeJob.progress * 100) }}% · {{ formatDuration(activeJob.elapsed_seconds) }}</p></template>
        <p v-else class="text-sm text-muted-foreground">No training running. One job runs at a time.</p>
      </article>
    </section>

    <!-- Access -->
    <section class="flex flex-wrap items-center gap-3 rounded-2xl border border-white/10 bg-white/[0.03] px-4 py-3" aria-label="Training access">
      <KeyRound class="size-4 text-primary" />
      <template v-if="options && !options.control_enabled"><p class="text-sm text-muted-foreground">Training control is disabled on the server (TRAINING_ADMIN_TOKEN is not set). Status and results stay visible.</p></template>
      <template v-else-if="unlocked"><p class="mr-auto text-sm">Training control unlocked for this browser tab.</p><Button size="sm" variant="ghost" @click="lock"><Lock /> Lock</Button></template>
      <form v-else class="flex min-w-0 flex-1 flex-wrap items-center gap-2" @submit.prevent="verifyToken(tokenInput)">
        <label for="training-token" class="text-sm text-muted-foreground">Starting, stopping and downloading need the training admin token.</label>
        <Input id="training-token" v-model="tokenInput" type="password" autocomplete="off" placeholder="Admin token" class="h-8 w-60 max-w-full" />
        <Button size="sm" type="submit" :disabled="!tokenInput || unlocking"><LoaderCircle v-if="unlocking" class="animate-spin" /> Unlock</Button>
        <span v-if="authError" class="text-xs text-red-300" role="alert">{{ authError }}</span>
      </form>
    </section>

    <div class="grid gap-4 lg:grid-cols-2">
      <!-- Dataset -->
      <section class="min-w-0 space-y-4 rounded-2xl border border-white/10 bg-white/[0.03] p-5" aria-label="Dataset">
        <div class="flex flex-wrap items-center justify-between gap-3"><h2 class="flex items-center gap-2 text-sm font-semibold"><Database class="size-4 text-primary" /> 1. Dataset</h2>
          <DatasetSelect v-model="form.exportId" :options="datasetOptions" label="Dataset to train on" class="w-full sm:w-72" :placeholder="loading ? 'Loading datasets…' : 'No dataset exports'" :disabled="!datasets.length" /></div>
        <p v-if="!loading && !datasets.length" class="rounded-xl border border-dashed border-white/15 py-8 text-center text-sm text-muted-foreground">No dataset export yet. Create one in <NuxtLink to="/datasets" class="text-primary underline underline-offset-4">AI Datasets</NuxtLink>.</p>
        <p v-else-if="checking" class="py-6 text-center text-sm text-muted-foreground" role="status"><LoaderCircle class="mr-2 inline size-4 animate-spin" />Checking dataset…</p>
        <template v-else-if="check">
          <div class="flex flex-wrap items-center gap-2"><p class="mr-auto text-sm font-medium">{{ check.name }} <span class="text-xs font-normal text-muted-foreground">· created {{ time(check.created_at) }}</span></p><Badge variant="outline" :class="['px-2 py-0.5', readinessClass[check.readiness]]">{{ READINESS_LABELS[check.readiness] }}</Badge></div>
          <div class="overflow-x-auto"><table class="w-full text-left text-xs">
            <thead class="text-muted-foreground"><tr><th class="py-1 pr-3 font-medium">Split</th><th class="pr-3 font-medium">Images</th><th class="pr-3 font-medium">Vehicle labels</th><th class="font-medium">Capture sessions</th></tr></thead>
            <tbody class="font-mono tabular-nums"><tr v-for="row in splitRows" :key="row.split" class="border-t border-white/5"><td class="py-1.5 pr-3 font-sans capitalize">{{ row.split }}</td><td class="pr-3">{{ row.images }}</td><td class="pr-3">{{ row.boxes }}</td><td>{{ row.sessions }}</td></tr>
              <tr class="border-t border-white/10 font-semibold"><td class="py-1.5 pr-3 font-sans">Total</td><td class="pr-3">{{ check.counts?.total ?? 0 }}</td><td class="pr-3">{{ check.boxes?.total ?? 0 }}</td><td /></tr></tbody></table></div>
          <p class="text-xs text-muted-foreground">Classes {{ Object.values(check.classes ?? {}).join(', ') || '–' }} · labels per class: <span v-for="(count, name) in check.labels_per_class_total" :key="name" class="mr-2 font-mono text-foreground">{{ name }} {{ count }}</span></p>
          <ul v-if="check.errors.length" class="space-y-1 text-xs text-red-300"><li v-for="item in check.errors" :key="item" class="flex gap-2"><Ban class="mt-0.5 size-3.5 shrink-0" />{{ item }}</li></ul>
          <ul v-if="check.limits.length" class="space-y-1 text-xs text-amber-200"><li v-for="item in check.limits" :key="item" class="flex gap-2"><AlertTriangle class="mt-0.5 size-3.5 shrink-0" />{{ item }}</li></ul>
          <ul v-if="check.notes.length" class="space-y-1 text-xs text-muted-foreground"><li v-for="item in check.notes" :key="item">{{ item }}</li></ul>
          <label v-if="check.readiness === 'limited'" class="flex items-start gap-2 rounded-xl border border-amber-300/20 bg-amber-300/5 p-3 text-xs text-amber-100"><input v-model="form.acknowledge" type="checkbox" class="mt-0.5 accent-amber-300"> I understand these limitations. Results from this dataset are a trial run and will not be shown as a confirmed improvement.</label>
          <p class="text-xs text-muted-foreground">Training uses a frozen copy of this export (sha256 recorded with the job), so later labeling does not change the run.</p>
        </template>
      </section>

      <!-- Settings -->
      <section class="min-w-0 space-y-4 rounded-2xl border border-white/10 bg-white/[0.03] p-5" aria-label="Fine-tuning settings">
        <h2 class="flex items-center gap-2 text-sm font-semibold"><SlidersHorizontal class="size-4 text-primary" /> 2. Fine-tuning settings</h2>
        <div class="grid gap-4 sm:grid-cols-2">
          <div class="space-y-1.5"><span class="text-xs text-muted-foreground">Start from</span><DatasetSelect v-model="form.baseModelId" :options="baseOptions" label="Base model" class="w-full" /></div>
          <label class="space-y-1.5"><span class="text-xs text-muted-foreground">Model name</span><Input v-model="form.name" maxlength="48" :aria-invalid="!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,47}$/.test(form.name)" /></label>
          <label class="space-y-1.5"><span class="text-xs text-muted-foreground">Epochs</span><Input v-model="form.epochs" type="number" inputmode="numeric" :min="options?.limits.epochs[0]" :max="options?.limits.epochs[1]" :disabled="form.smoke" /></label>
          <div class="space-y-1.5"><span class="text-xs text-muted-foreground">Image size</span>
            <div class="flex gap-1 rounded-lg bg-white/[0.05] p-1" role="radiogroup" aria-label="Image size"><button v-for="size in [640, 960] as const" :key="size" type="button" role="radio" :aria-checked="form.imgsz === size" :class="['h-7 flex-1 rounded-md text-sm transition-colors', form.imgsz === size ? 'bg-primary/15 text-primary' : 'text-muted-foreground hover:text-foreground']" @click="form.imgsz = size">{{ size }}</button></div></div>
        </div>
        <fieldset class="space-y-2"><legend class="mb-1.5 text-xs text-muted-foreground">Classes</legend>
          <label v-for="mode in (['vehicle3', 'single'] as const)" :key="mode" :class="['flex cursor-pointer gap-3 rounded-xl border p-3 transition-colors', form.classMode === mode ? 'border-primary/40 bg-primary/5' : 'border-white/10 hover:border-white/20']">
            <input v-model="form.classMode" type="radio" name="class-mode" :value="mode" class="mt-1 accent-primary">
            <span class="space-y-0.5"><span class="block text-sm font-medium">{{ CLASS_MODE_LABELS[mode] }}<span v-if="mode === 'vehicle3'" class="ml-2 text-xs font-normal text-primary">default</span></span><span class="block text-xs text-muted-foreground">{{ options?.class_modes[mode] }}</span></span>
          </label>
        </fieldset>
        <details class="rounded-xl border border-white/10" :open="advanced" @toggle="advanced = ($event.target as HTMLDetailsElement).open">
          <summary class="cursor-pointer px-3 py-2 text-xs font-medium text-muted-foreground">Advanced (batch, seed, early stopping, smoke test)</summary>
          <div class="grid gap-3 px-3 pb-3 sm:grid-cols-3">
            <label class="space-y-1.5"><span class="text-xs text-muted-foreground">Batch size</span><Input v-model="form.batch" type="number" inputmode="numeric" min="1" max="32" /></label>
            <label class="space-y-1.5"><span class="text-xs text-muted-foreground">Seed</span><Input v-model="form.seed" type="number" inputmode="numeric" min="0" /></label>
            <label class="space-y-1.5"><span class="text-xs text-muted-foreground">Early-stop patience</span><Input v-model="form.patience" type="number" inputmode="numeric" min="1" max="300" /></label>
            <p class="text-xs text-muted-foreground sm:col-span-3">Device: GPU only (training never falls back to CPU). If the first epoch runs out of memory, Ultralytics halves the batch automatically; the final batch is recorded.</p>
            <label class="flex items-center gap-2 text-xs sm:col-span-3"><input v-model="form.smoke" type="checkbox" class="accent-primary"> Smoke test: 1 epoch to check the pipeline and GPU memory (not registered as a model)</label>
          </div>
        </details>
        <p class="text-xs text-muted-foreground">Estimated training memory {{ formatMib(estimate) }} · needs {{ formatMib(needed) }} free (incl. DeepStream reserve)<template v-if="suggestedBatch !== null"> · largest safe batch now: <span :class="suggestedBatch < batchNumber ? 'text-amber-200' : ''">{{ suggestedBatch || 'none' }}</span></template>. Estimates come from the 2026-10-13 server measurement.</p>
        <ul v-if="formErrors.length" class="space-y-1 text-xs text-red-300"><li v-for="item in formErrors" :key="item">{{ item }}</li></ul>
      </section>
    </div>

    <!-- Start -->
    <section class="space-y-4 rounded-2xl border border-white/10 bg-white/[0.03] p-5" aria-label="Start training">
      <h2 class="text-sm font-semibold">3. Checks before training</h2>
      <ul class="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
        <li v-for="item in checks" :key="item.label" class="flex gap-2 rounded-xl border border-white/10 bg-black/10 p-3 text-xs"><component :is="stateIcon[item.state]" :class="['mt-0.5 size-4 shrink-0', stateClass[item.state], item.state === 'pending' ? 'animate-pulse' : '']" /><span class="min-w-0"><span class="block font-medium text-foreground">{{ item.label }}</span><span class="text-muted-foreground">{{ item.detail }}</span></span></li>
      </ul>
      <div class="flex flex-wrap items-center gap-3">
        <Button size="lg" :disabled="!canStart" @click="start"><LoaderCircle v-if="starting" class="animate-spin" /><Play v-else /> {{ form.smoke ? 'Start smoke test' : 'Start training' }}</Button>
        <p v-if="startError" class="text-sm text-red-300" role="alert">{{ startError }}</p>
        <p v-else-if="notice" class="text-sm text-primary" role="status">{{ notice }}</p>
        <p v-else class="text-xs text-muted-foreground">Runs in the background on the GPU server; closing this page does not stop it.</p>
      </div>
    </section>

    <!-- Progress / results of the selected job -->
    <section class="space-y-4 rounded-2xl border border-white/10 bg-white/[0.03] p-5" aria-label="Training progress">
      <h2 class="text-sm font-semibold">4. Progress and results</h2>
      <TrainingJobPanel v-if="detail" :job="detail" :can-control="unlocked" :stopping="stopping" @stop="stop" @download="download" />
      <p v-else-if="selectedJobId" class="py-6 text-center text-sm text-muted-foreground" role="status"><LoaderCircle class="mr-2 inline size-4 animate-spin" />Loading job…</p>
      <p v-else class="rounded-xl border border-dashed border-white/15 py-10 text-center text-sm text-muted-foreground">No training has been run yet.</p>
    </section>

    <div class="grid gap-4 xl:grid-cols-2">
      <!-- History -->
      <section class="min-w-0 space-y-3 rounded-2xl border border-white/10 bg-white/[0.03] p-5" aria-label="Training history">
        <h2 class="text-sm font-semibold">Training history <span class="ml-1 font-normal text-muted-foreground">{{ jobs.length }}</span></h2>
        <p v-if="!jobs.length" class="py-6 text-center text-sm text-muted-foreground">No jobs yet.</p>
        <div v-else class="overflow-x-auto"><table class="w-full min-w-[36rem] text-left text-xs">
          <thead class="text-muted-foreground"><tr><th class="py-1.5 pr-3 font-medium">Job</th><th class="pr-3 font-medium">Status</th><th class="pr-3 font-medium">Epochs</th><th class="pr-3 font-medium">Best mAP50-95</th><th class="pr-3 font-medium">Duration</th><th class="font-medium">Created</th></tr></thead>
          <tbody><tr v-for="job in jobs" :key="job.id" :class="['cursor-pointer border-t border-white/5 hover:bg-white/[0.03]', job.id === selectedJobId ? 'bg-primary/5' : '']" tabindex="0" @click="selectedJobId = job.id" @keydown.enter="selectedJobId = job.id">
            <td class="py-2 pr-3"><span class="font-medium">{{ job.name }}</span><span class="block text-muted-foreground">{{ job.config.imgsz }} px · b{{ job.config.batch }} · {{ job.config.class_mode === 'single' ? 'single' : '3 cls' }}{{ job.config.smoke ? ' · smoke' : '' }}</span></td>
            <td :class="['pr-3', jobStatusClass(job.status)]">{{ JOB_STATUS_LABELS[job.status] }}</td>
            <td class="pr-3 font-mono tabular-nums">{{ job.epoch }}/{{ job.epochs }}</td><td class="pr-3 font-mono tabular-nums">{{ formatMetric(job.best?.map50_95) }}</td>
            <td class="pr-3 tabular-nums">{{ formatDuration(job.elapsed_seconds) }}</td><td class="text-muted-foreground">{{ time(job.created_at) }}</td></tr></tbody></table></div>
      </section>

      <!-- Models -->
      <section class="min-w-0 space-y-3 rounded-2xl border border-white/10 bg-white/[0.03] p-5" aria-label="Models">
        <h2 class="text-sm font-semibold">Models</h2>
        <p class="text-xs text-muted-foreground">New models are stored for download and later rollout. DeepStream keeps the current YOLO11m until a separate, approved deployment (training/README.md §8).</p>
        <article v-for="model in models" :key="model.id" class="space-y-2 rounded-xl border border-white/10 bg-black/10 p-3">
          <div class="flex flex-wrap items-center gap-2"><p class="mr-auto text-sm font-medium">{{ model.label }}</p>
            <Badge variant="outline" :class="['px-2 py-0.5', model.kind === 'base' ? 'border-sky-400/25 bg-sky-400/10 text-sky-300' : 'border-primary/25 bg-primary/10 text-primary']">{{ model.kind === 'base' ? 'Production base' : 'Fine-tuned' }}</Badge>
            <Badge v-if="model.kind === 'finetuned'" variant="outline" class="px-2 py-0.5 text-muted-foreground">Not deployed</Badge></div>
          <p v-if="model.kind === 'base'" class="text-xs text-muted-foreground">COCO 80 classes · weights on the training host{{ model.files['yolo11m.pt']?.sha256 ? ` · sha256 ${model.files['yolo11m.pt']!.sha256!.slice(0, 12)}…` : '' }}</p>
          <template v-else>
            <p class="text-xs text-muted-foreground">From {{ models.find(item => item.id === model.base_model_id)?.label ?? model.base_model_id }} · dataset_{{ model.export_id?.slice(0, 8) }} · {{ time(model.created_at) }}</p>
            <p class="text-xs text-muted-foreground">{{ model.config?.epochs_run }}/{{ model.config?.epochs }} epochs · {{ model.config?.imgsz }} px · batch {{ model.config?.final_batch ?? model.config?.batch }} · {{ model.class_mode === 'single' ? 'single class' : 'car/truck/bus' }}</p>
            <p class="font-mono text-xs tabular-nums">val mAP50 {{ formatMetric(model.metrics?.val?.map50) }} · mAP50-95 {{ formatMetric(model.metrics?.val?.map50_95) }} · test mAP50 {{ formatMetric(model.metrics?.test?.vehicle?.mAP50) }} <span class="text-muted-foreground">(YOLO11m {{ formatMetric(model.metrics?.test_baseline?.vehicle?.mAP50) }})</span></p>
            <p v-if="!model.evaluation_conclusive" class="text-xs text-amber-200">Test evidence is insufficient; improvement is not confirmed.</p>
            <div class="flex flex-wrap items-center gap-2">
              <Button v-for="name in ['best.pt', 'last.pt']" :key="name" size="sm" variant="outline" :disabled="!unlocked || !model.files[name]?.download_url" :title="unlocked ? fileSize(model.files[name]?.size) : 'Unlock training control to download'" @click="download(`${model.name}-v${model.version}-${name}`, model.files[name]!.download_url!)"><Download /> {{ name }}</Button>
              <Button size="sm" variant="outline" disabled title="Planned: requires separate approval, ONNX export and DeepStream validation">Apply to DeepStream (later)</Button>
              <button v-if="model.job_id" type="button" class="text-xs text-primary underline-offset-4 hover:underline" @click="selectedJobId = model.job_id">Open training run</button>
            </div>
          </template>
        </article>
      </section>
    </div>
  </main>
</template>
