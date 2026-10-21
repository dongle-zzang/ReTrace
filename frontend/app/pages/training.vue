<script setup lang="ts">
import { useIntervalFn } from '@vueuse/core'
import { AlertTriangle, Ban, CircleCheck, CircleDashed, Download, LoaderCircle, Lock, Play, RefreshCw } from '@lucide/vue'
import DatasetSelect from '~/components/DatasetSelect.vue'
import DeploymentReport from '~/components/DeploymentReport.vue'
import LockedOverlay from '~/components/LockedOverlay.vue'
import TrainingJobPanel from '~/components/TrainingJobPanel.vue'
import TrainingNewImageEval from '~/components/TrainingNewImageEval.vue'
import { Badge } from '~/components/ui/badge'
import { Button } from '~/components/ui/button'
import { Input } from '~/components/ui/input'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '~/components/ui/tabs'
import { downloadTrainingFile, trainingRequest, useTrainingToken } from '~/composables/trainingApi'
import { useI18n } from '~/composables/useI18n'
import { DEPLOY_PHASES, deployBlocker, deployClasses, estimateVramMib, formatDuration, formatMetric, formatMib, isActiveStatus, jobKind, requiredFreeMib, safeBatch, suggestedTrainAllEpochs, toInteger, validateTrainingForm } from '~/lib/training'
import type { DeploymentReport as DeploymentReportData } from '~/lib/training'
import { CLASS_MODE_LABELS, JOB_STATUS_LABELS, READINESS_LABELS, TRAIN_MODE_LABELS } from '~/types/training'
import type { ClassMode, DatasetCheck, ImageSize, TrainingDataset, TrainingJob, TrainingJobDetail, TrainingModel, TrainingOptions, TrainingStatus, TrainMode } from '~/types/training'

const { t, dateLocale } = useI18n()
useHead({ title: () => `ReTrace · ${t('AI training')}` })

// The workspace is split into tabs; the open tab lives in ?tab= so a reload or shared link keeps it.
const TABS = ['train', 'progress', 'evaluate', 'history'] as const
type TrainingTab = typeof TABS[number]
const route = useRoute()
const router = useRouter()
const tab = computed<TrainingTab>({
  get: () => TABS.find(item => item === route.query.tab) ?? 'train',
  set: value => { void router.replace({ query: { ...route.query, tab: value === 'train' ? undefined : value } }) },
})

const options = ref<TrainingOptions | null>(null)
const status = ref<TrainingStatus | null>(null)
const datasets = ref<TrainingDataset[]>([])
const check = ref<DatasetCheck | null>(null)
const models = ref<TrainingModel[]>([])
const jobs = ref<TrainingJob[]>([])
interface DeploymentStatus extends DeploymentReportData {
  available: boolean; confirmed: boolean; active?: string; previous?: string; phase?: string; error?: string
  /** Model class id -> running detector class id; null for COCO80 (identity). */
  active_class_map?: Record<string, number> | null
}
const deployment = ref<DeploymentStatus | null>(null)
const deploying = ref(false)
function modelLabel(id?: string | null) {
  return id === 'production-original' ? t('Original production model') : models.value.find(m => m.id === id)?.label ?? id ?? '–'
}
async function loadDeployment() {
  try { deployment.value = await trainingRequest<DeploymentStatus>('/deployment') }
  catch { deployment.value = null }
}
async function startDeployment(modelId?: string) {
  if (!unlocked.value || deploying.value || activeJob.value || !deployment.value?.available) return
  const target = modelId ? modelLabel(modelId) : modelLabel(deployment.value.previous)
  if (!await confirm({
    title: modelId ? t('Deploy {model} to DeepStream?', { model: target }) : t('Roll back to {model}?', { model: target }),
    description: t('Parking vehicle detection switches models without restarting video. Live inference is verified for about a minute; if it fails, the previous model is restored automatically. Training data and models are kept.'),
    confirmLabel: modelId ? t('Deploy to DeepStream') : t('Roll back model'),
  })) return
  deploying.value = true; error.value = null
  try {
    const job = await trainingRequest<TrainingJobDetail>('/deployments', { method: 'POST', auth: true,
      body: modelId ? { model_id: modelId, action: 'deploy' } : { action: 'rollback' } })
    selectedJobId.value = job.id; detail.value = job; tab.value = 'progress'
    await Promise.all([loadJobs(), loadStatus(), loadDeployment()])
  } catch (cause) { error.value = (cause as Error).message }
  finally { deploying.value = false }
}
const selectedJobId = ref<string | null>(null)
function openJob(id: string) { selectedJobId.value = id; tab.value = 'progress' }
const detail = ref<TrainingJobDetail | null>(null)
const loading = ref(true)
const checking = ref(false)
const starting = ref(false)
const stopping = ref(false)
const error = ref<string | null>(null)
const startError = ref<string | null>(null)
const notice = ref<string | null>(null)

const form = reactive({
  exportId: '', baseModelId: 'yolo11m-coco', name: 'yolo11m-ft', classMode: 'vehicle3' as ClassMode, trainMode: 'standard' as TrainMode,
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
  await Promise.all([loadStatus(), loadJobs(), loadDatasets(), loadModels(), loadDeployment()])
  await loadDetail()
}
let checkVersion = 0
watch(() => [form.exportId, form.trainMode] as const, async ([id, mode]) => {
  check.value = null; form.acknowledge = false
  if (!id) return
  const version = ++checkVersion
  checking.value = true
  try {
    const data = await trainingRequest<DatasetCheck>(`/datasets/${id}/check?train_mode=${mode}`)
    if (version === checkVersion) check.value = data
  } catch (cause) { if (version === checkVersion) error.value = (cause as Error).message }
  finally { if (version === checkVersion) checking.value = false }
})
watch(selectedJobId, () => { detail.value = null; void loadDetail() })
watch(() => [form.imgsz, form.batch], () => { void loadStatus() })

const activeJob = computed(() => status.value?.active_job ?? jobs.value.find(job => isActiveStatus(job.status)) ?? null)
useIntervalFn(() => { void loadStatus(); void loadDeployment() }, 5000)
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
  if (options.value) Object.assign(form, { epochs: options.value.defaults.epochs, imgsz: options.value.defaults.imgsz, batch: options.value.defaults.batch, seed: options.value.defaults.seed, patience: options.value.defaults.patience })
  await refreshAll()
  loading.value = false
  if (token.value && options.value?.control_enabled) void verifyToken(token.value)
})

// ----- form & pre-start checks ------------------------------------------------------------
const baseModel = computed(() => models.value.find(model => model.id === form.baseModelId))
const trainAll = computed(() => form.trainMode === 'all')
// Train All keeps the pretrained COCO head (car stays class 2), so it is car/truck/bus only.
const epochHint = computed(() => trainAll.value ? suggestedTrainAllEpochs(jobs.value, form.exportId) : null)
const baseOptions = computed(() => models.value.filter(model => model.status === 'available').map(model => ({
  value: model.id, label: model.kind === 'base' ? t('YOLO11m pretrained (COCO, production)') : `${model.label} · ${model.class_mode === 'single' ? t('single class') : 'car/truck/bus'}`,
})))
const datasetOptions = computed(() => datasets.value.map(item => ({ value: item.id, label: `${item.name} · ${t('{count} images', { count: item.counts?.total ?? 0 })} · ${t(READINESS_LABELS[item.readiness_by_mode?.[form.trainMode] ?? item.readiness])}` })))
const formErrors = computed(() => {
  if (!options.value) return []
  const errors = validateTrainingForm({ epochs: form.epochs, imgsz: form.imgsz, batch: form.batch, seed: form.seed, patience: form.patience }, options.value.limits)
  if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,47}$/.test(form.name)) errors.push('Model name: 1–48 letters, digits, ".", "_" or "-", starting with a letter or digit.')
  if (baseModel.value?.kind === 'finetuned' && form.classMode === 'vehicle3' && baseModel.value.class_mode !== 'vehicle3')
    errors.push('car/truck/bus training needs YOLO11m or a model that was trained in car/truck/bus mode.')
  if (trainAll.value && form.classMode !== 'vehicle3') errors.push('Train All keeps the COCO 80-class head: use car/truck/bus.')
  return errors
})
const batchNumber = computed(() => toInteger(form.batch))
const estimate = computed(() => Number.isInteger(batchNumber.value) && batchNumber.value > 0 ? estimateVramMib(form.imgsz, batchNumber.value, options.value?.gpu_policy.vram_estimate) : null)
const needed = computed(() => options.value && estimate.value !== null ? requiredFreeMib(form.imgsz, batchNumber.value, options.value.gpu_policy) : null)
const freeMib = computed(() => status.value?.worker.gpu?.free_mib)
const suggestedBatch = computed(() => options.value && typeof freeMib.value === 'number' ? safeBatch(form.imgsz, freeMib.value, options.value.gpu_policy) : null)
const readiness = computed(() => check.value?.readiness)
type CheckState = 'ok' | 'warn' | 'fail' | 'pending'
const checks = computed<{ key: string; label: string; state: CheckState; detail: string }[]>(() => [
  { key: 'dataset', label: t('Dataset'), state: checking.value || !check.value ? 'pending' : readiness.value === 'ready' ? 'ok' : readiness.value === 'limited' ? (form.acknowledge ? 'ok' : 'warn') : 'fail',
    detail: !form.exportId ? t('Select a dataset') : checking.value ? t('Checking labels, splits and sessions…') : readiness.value === 'blocked' ? check.value?.errors[0] ?? t('Not usable') : readiness.value === 'limited' ? t('{count} limitation(s) need confirmation', { count: check.value?.limits.length ?? 0 }) : t('Valid') },
  { key: 'gpu', label: 'GPU', state: !status.value ? 'pending' : status.value.gpu_ready ? 'ok' : 'fail', detail: status.value?.gpu_blocker ?? (needed.value ? t('{free} free, needs {needed}', { free: formatMib(freeMib.value), needed: formatMib(needed.value) }) : '') },
  { key: 'production', label: t('Production stream'), state: !status.value ? 'pending' : status.value.production.preview === 'reachable' ? 'ok' : options.value?.gpu_policy.require_preview ? 'fail' : 'warn',
    detail: status.value ? t('Preview {preview} · {online} cameras online · {fps} fps', { preview: t(status.value.production.preview), online: status.value.production.cameras_online, fps: status.value.production.total_fps }) : '' },
  { key: 'settings', label: t('Settings'), state: formErrors.value.length ? 'fail' : 'ok', detail: formErrors.value[0] ? t(formErrors.value[0]) : t('Valid') },
  { key: 'access', label: t('Access'), state: unlocked.value ? 'ok' : 'fail', detail: !options.value?.control_enabled ? t('Training control is disabled on the server') : unlocked.value ? t('Unlocked for this tab') : t('Enter the training admin token') },
  { key: 'queue', label: t('Queue'), state: activeJob.value ? 'fail' : 'ok', detail: activeJob.value ? t('{name} is {status}', { name: activeJob.value.name, status: t(JOB_STATUS_LABELS[activeJob.value.status]).toLowerCase() }) : t('No other training job') },
])
const canStart = computed(() => !starting.value && checks.value.every(item => item.state === 'ok' || (item.key === 'production' && item.state === 'warn')))

const { confirm } = useConfirmDialog()
async function start() {
  if (!canStart.value) return
  const full = !form.smoke
  const description = t('{epochs} at {imgsz} px, batch {batch} on the shared GPU. Production inference keeps priority: the job waits or stops if the GPU or camera FPS needs protecting.', { epochs: full ? t('{count} epochs', { count: toInteger(form.epochs) }) : t('1 epoch'), imgsz: form.imgsz, batch: toInteger(form.batch) })
  const modeNote = trainAll.value ? ` ${t('Train All: all {count} images train the weights, no early stopping, final-epoch weights. Evaluate the model on new CCTV images afterwards.', { count: check.value?.counts?.total ?? 0 })}` : ''
  if (!await confirm({ title: full ? (trainAll.value ? t('Start Train All?') : t('Start GPU training?')) : t('Start smoke test?'), description: description + modeNote, confirmLabel: full ? t('Start training') : t('Start smoke test') })) return
  starting.value = true; startError.value = null; notice.value = null
  try {
    const job = await trainingRequest<TrainingJobDetail>('/jobs', { method: 'POST', auth: true, body: {
      export_id: form.exportId, base_model_id: form.baseModelId, name: form.name, class_mode: form.classMode, train_mode: form.trainMode,
      epochs: toInteger(form.epochs), imgsz: form.imgsz, batch: toInteger(form.batch), seed: toInteger(form.seed),
      patience: toInteger(form.patience), device: 'gpu', smoke: form.smoke, acknowledge_limits: form.acknowledge,
    } })
    selectedJobId.value = job.id
    detail.value = job
    tab.value = 'progress'
    notice.value = 'Training job created. The GPU worker picks it up within a few seconds.'
    await Promise.all([loadJobs(), loadStatus()])
  } catch (cause) {
    startError.value = (cause as Error).message
    if ((cause as { status?: number }).status === 401) unlocked.value = false
  } finally { starting.value = false }
}
async function stop() {
  const job = detail.value
  if (job?.config.kind === 'deploy') {
    stopping.value = true
    try { await trainingRequest(`/jobs/${job.id}/stop`, { method: 'POST', auth: true }); await loadDetail() }
    catch (cause) { error.value = (cause as Error).message }
    finally { stopping.value = false }
    return
  }
  if (!job || !await confirm({ title: t('Stop training?'), description: t('Training stops after the current batch, then validates and saves last.pt/best.pt. A stopped run is not registered as a model.'), confirmLabel: t('Stop training'), destructive: true })) return
  stopping.value = true
  try { await trainingRequest(`/jobs/${job.id}/stop`, { method: 'POST', auth: true }); await loadDetail() }
  catch (cause) { error.value = (cause as Error).message }
  finally { stopping.value = false }
}
async function download(name: string, url: string) {
  try { await downloadTrainingFile(url, name) } catch (cause) { error.value = (cause as Error).message }
}

const time = (value: string | null) => value ? new Date(value).toLocaleString(dateLocale.value) : '–'
const stateIcon = { ok: CircleCheck, warn: AlertTriangle, fail: Ban, pending: CircleDashed }
const stateClass = { ok: 'text-primary', warn: 'text-amber-300', fail: 'text-red-300', pending: 'text-muted-foreground' }
const readinessClass = { ready: 'border-primary/25 bg-primary/10 text-primary', limited: 'border-amber-300/25 bg-amber-300/10 text-amber-200', blocked: 'border-red-400/25 bg-red-400/10 text-red-300' }
const jobStatusClass = (value: string) => ({ completed: 'text-primary', failed: 'text-red-300', running: 'text-sky-300', stopping: 'text-amber-200', queued: 'text-amber-200' } as Record<string, string>)[value] ?? 'text-muted-foreground'
const gpu = computed(() => status.value?.worker.gpu)
const gpuPercent = computed(() => gpu.value?.total_mib ? Math.round(((gpu.value.used_mib ?? 0) / gpu.value.total_mib) * 100) : 0)
const SPLIT_USE = { train: 'Training', val: 'Best epoch / early stop', test: 'Independent test' } as const
const splitRows = computed(() => (['train', 'val', 'test'] as const).map(split => ({ split, images: check.value?.counts?.[split] ?? 0, boxes: check.value?.boxes?.[split] ?? 0, sessions: check.value?.sessions?.[split] ?? 0 })))
const fileSize = (bytes?: number) => typeof bytes === 'number' ? `${(bytes / 1048576).toFixed(1)} MiB` : ''
const KIND_LABELS = { standard: 'Train / Val / Test', all: 'Train All', evaluate: 'New-image evaluation', deploy: 'Model deployment' } as const
const jobTag = (job: TrainingJob) => job.config.kind === 'deploy'
  ? (job.config.action === 'rollback' ? t('Roll back model') : t('Deploy to DeepStream'))
  : job.config.kind === 'evaluate'
  ? t('{count} new images', { count: job.config.eval_images ?? 0 })
  : `${job.config.imgsz} px · b${job.config.batch} · ${job.config.class_mode === 'single' ? t('single') : t('3 cls')}${job.config.smoke ? ` · ${t('smoke')}` : ''}`

// ----- new-image evaluation ----------------------------------------------------------------
const evalModelId = ref('')
function evaluateModel(modelId: string | null) {
  if (modelId) evalModelId.value = modelId
  tab.value = 'evaluate'
}
async function evaluationCreated(job: TrainingJobDetail) {
  selectedJobId.value = job.id
  detail.value = job
  tab.value = 'progress'
  notice.value = null
  await Promise.all([loadJobs(), loadStatus()])
}
</script>

<template>
  <main class="relative mx-auto max-w-[1600px] space-y-8 px-3 py-6 break-keep sm:px-6">
    <header class="flex flex-wrap items-end gap-4">
      <div class="mr-auto space-y-2"><p class="text-xs font-semibold uppercase tracking-[0.2em] text-primary">{{ t('AI training workspace') }}</p><h1 class="text-2xl font-semibold tracking-tight sm:text-3xl">{{ t('Fine-tune. Compare. Version.') }}</h1><p class="max-w-4xl text-sm leading-relaxed text-muted-foreground sm:text-base">{{ t('Fine-tune YOLO11m on a reviewed dataset on the GPU server. Production models are never replaced automatically.') }}</p></div>
      <Button variant="ghost" size="sm" :disabled="loading" @click="refreshAll"><RefreshCw :class="loading ? 'animate-spin' : ''" /> {{ t('Refresh') }}</Button>
    </header>
    <div v-if="error" class="flex flex-wrap items-center gap-3 rounded-xl border border-red-400/20 bg-red-400/5 p-4 text-sm text-red-300" role="alert"><span class="min-w-0 flex-1">{{ t(error) }}</span><Button size="sm" variant="ghost" @click="refreshAll"><RefreshCw /> {{ t('Retry') }}</Button></div>

    <!-- GPU / worker / production / active job -->
    <section class="grid gap-3 sm:grid-cols-2 xl:grid-cols-4" :aria-label="t('Training resources')">
      <article class="space-y-2 rounded-2xl border border-white/[0.12] bg-white/[0.04] p-4">
        <h2 class="text-sm font-medium text-muted-foreground">{{ t('GPU worker') }}</h2>
        <p v-if="!status" class="text-sm text-muted-foreground">{{ t('Loading…') }}</p>
        <template v-else><p class="flex items-center gap-2 text-base font-semibold"><span :class="['size-2.5 rounded-full', status.worker.online ? 'bg-primary' : 'bg-red-400']" />{{ status.worker.online ? t('Online') : t('Offline') }}</p>
          <p class="text-sm leading-relaxed text-muted-foreground">{{ status.worker.online ? t('Last report {seconds}s ago', { seconds: status.worker.age_seconds ?? '–' }) : t('Start training/worker.py on the GPU server.') }}</p>
          <p v-if="status.worker.online && !status.worker.base_weights?.present" class="text-sm text-red-300">{{ t('training/weights/yolo11m.pt is missing.') }}</p></template>
      </article>
      <article class="space-y-2 rounded-2xl border border-white/[0.12] bg-white/[0.04] p-4">
        <h2 class="text-sm font-medium text-muted-foreground">GPU {{ gpu?.name ? `· ${gpu.name}` : '' }}</h2>
        <template v-if="gpu?.available"><p class="font-mono text-base font-semibold tabular-nums">{{ formatMib(gpu.used_mib) }} / {{ formatMib(gpu.total_mib) }} · {{ gpu.util }}%</p>
          <div class="h-2 overflow-hidden rounded-full bg-white/10"><div :class="['h-full rounded-full', gpuPercent > 85 ? 'bg-red-400' : gpuPercent > 65 ? 'bg-amber-300' : 'bg-primary']" :style="{ width: `${gpuPercent}%` }" /></div>
          <p class="text-sm leading-relaxed text-muted-foreground">{{ t('{free} free · DeepStream reserve {reserve}', { free: formatMib(gpu.free_mib), reserve: formatMib(options?.gpu_policy.reserve_mib) }) }}</p></template>
        <p v-else class="text-sm text-muted-foreground">{{ gpu?.error ?? t('No GPU report') }}</p>
      </article>
      <article class="space-y-2 rounded-2xl border border-white/[0.12] bg-white/[0.04] p-4">
        <h2 class="text-sm font-medium text-muted-foreground">{{ t('Production inference') }}</h2>
        <template v-if="status"><p class="flex items-center gap-2 text-base font-semibold"><span :class="['size-2.5 rounded-full', status.production.preview === 'reachable' ? 'bg-primary' : 'bg-red-400']" />{{ t('Preview {state}', { state: t(status.production.preview) }) }}</p>
          <p class="text-sm leading-relaxed text-muted-foreground">{{ t('{online}/{enabled} cameras online · {fps} fps · parking {parking} cams {parkingFps} fps', { online: status.production.cameras_online, enabled: status.production.cameras_enabled, fps: status.production.total_fps, parking: status.production.parking_cameras_online, parkingFps: status.production.parking_fps }) }}</p></template>
      </article>
      <article class="space-y-2 rounded-2xl border border-white/[0.12] bg-white/[0.04] p-4">
        <h2 class="text-sm font-medium text-muted-foreground">{{ t('Running job') }}</h2>
        <template v-if="activeJob"><button type="button" class="block text-left text-base font-semibold hover:text-primary" @click="openJob(activeJob.id)">{{ activeJob.name }} · {{ t(JOB_STATUS_LABELS[activeJob.status]) }}</button>
          <p class="text-sm leading-relaxed text-muted-foreground">{{ t('Epoch') }} {{ activeJob.epoch }}/{{ activeJob.epochs }} · {{ Math.round(activeJob.progress * 100) }}% · {{ formatDuration(activeJob.elapsed_seconds) }}</p></template>
        <p v-else class="text-sm text-muted-foreground">{{ t('No training running. One job runs at a time.') }}</p>
      </article>
    </section>

    <!-- Access -->
    <section class="flex flex-wrap items-center gap-3 rounded-2xl border border-white/[0.12] bg-white/[0.04] px-4 py-3" :aria-label="t('Training access')">
      <template v-if="options && !options.control_enabled"><p class="text-sm text-muted-foreground">{{ t('Training control is disabled on the server (TRAINING_ADMIN_TOKEN is not set). Status and results stay visible.') }}</p></template>
      <template v-else-if="unlocked"><p class="mr-auto text-sm">{{ t('Training control unlocked for this browser tab.') }}</p><Button size="sm" variant="ghost" @click="lock"><Lock /> {{ t('Lock') }}</Button></template>
      <form v-else class="flex min-w-0 flex-1 flex-wrap items-center gap-2" @submit.prevent="verifyToken(tokenInput)">
        <label for="training-token" class="mr-auto text-sm text-muted-foreground">{{ t('Starting, stopping and downloading need the training admin token.') }}</label>
        <Input id="training-token" v-model="tokenInput" type="password" autocomplete="off" :placeholder="t('Admin token')" class="h-8 w-60 max-w-full" />
        <Button size="sm" type="submit" :disabled="!tokenInput || unlocking"><LoaderCircle v-if="unlocking" class="animate-spin" /> {{ t('Unlock') }}</Button>
        <span v-if="authError" class="text-xs text-red-300" role="alert">{{ t(authError) }}</span>
      </form>
    </section>

    <Tabs v-model="tab" class="flex-col gap-6">
      <TabsList variant="line" class="h-auto w-full justify-start gap-1 overflow-x-auto rounded-none p-0 sm:gap-3" :aria-label="t('Training sections')">
        <TabsTrigger value="train" class="h-10 flex-none px-1.5 text-[0.9375rem] font-medium sm:px-3 sm:text-base data-active:bg-transparent data-active:text-primary dark:data-active:border-transparent dark:data-active:bg-transparent dark:data-active:text-primary">{{ t('New training') }}</TabsTrigger>
        <TabsTrigger value="progress" class="h-10 flex-none px-1.5 text-[0.9375rem] font-medium sm:px-3 sm:text-base data-active:bg-transparent data-active:text-primary dark:data-active:border-transparent dark:data-active:bg-transparent dark:data-active:text-primary">{{ t('Progress & results') }}<span v-if="activeJob" class="size-2 animate-pulse rounded-full bg-sky-400" aria-hidden="true" /></TabsTrigger>
        <TabsTrigger value="evaluate" class="h-10 flex-none px-1.5 text-[0.9375rem] font-medium sm:px-3 sm:text-base data-active:bg-transparent data-active:text-primary dark:data-active:border-transparent dark:data-active:bg-transparent dark:data-active:text-primary">{{ t('New-image check') }}</TabsTrigger>
        <TabsTrigger value="history" class="h-10 flex-none px-1.5 text-[0.9375rem] font-medium sm:px-3 sm:text-base data-active:bg-transparent data-active:text-primary dark:data-active:border-transparent dark:data-active:bg-transparent dark:data-active:text-primary">{{ t('History & models') }}<span class="hidden text-xs font-normal text-muted-foreground tabular-nums sm:inline">{{ jobs.length }}</span></TabsTrigger>
      </TabsList>

      <TabsContent value="train" class="space-y-6">
        <div class="grid gap-4 lg:grid-cols-2">
          <!-- Dataset -->
          <section class="relative min-w-0 space-y-4 rounded-2xl border border-white/[0.12] bg-white/[0.04] p-5 sm:p-6" :inert="!unlocked" :aria-label="t('Dataset')">
            <LockedOverlay v-if="!unlocked" />
            <div class="flex flex-wrap items-center justify-between gap-3"><h2 class="flex items-center gap-2.5 text-base font-semibold"><span class="inline-grid size-6 shrink-0 place-items-center rounded-full bg-primary/15 text-xs font-semibold text-primary" aria-hidden="true">1</span>{{ t('Dataset') }}</h2>
              <DatasetSelect v-model="form.exportId" :options="datasetOptions" :label="t('Dataset to train on')" class="w-full sm:w-72" :placeholder="loading ? t('Loading datasets…') : t('No dataset exports')" :disabled="!datasets.length" /></div>
            <p v-if="!loading && !datasets.length" class="rounded-xl border border-dashed border-white/15 py-8 text-center text-sm text-muted-foreground">{{ t('No dataset export yet.') }} <NuxtLink to="/datasets" class="text-primary underline underline-offset-4">{{ t('Create one in AI Datasets') }}</NuxtLink></p>
            <p v-else-if="checking" class="py-6 text-center text-sm text-muted-foreground" role="status"><LoaderCircle class="mr-2 inline size-4 animate-spin" />{{ t('Checking dataset…') }}</p>
            <template v-else-if="check">
              <div class="flex flex-wrap items-center gap-2"><p class="mr-auto text-base font-medium">{{ check.name }} <span class="text-sm font-normal text-muted-foreground">· {{ t('created {time}', { time: time(check.created_at) }) }}</span></p><Badge variant="outline" :class="['px-2 py-0.5', readinessClass[check.readiness]]">{{ t(READINESS_LABELS[check.readiness]) }}</Badge></div>
              <div class="overflow-x-auto"><table class="w-full text-left text-sm">
                <thead class="text-xs text-muted-foreground"><tr class="whitespace-nowrap"><th class="py-2 pr-3 font-medium">{{ t('Split') }}</th><th class="pr-3 font-medium">{{ t('Images') }}</th><th class="pr-3 font-medium">{{ t('Vehicle labels') }}</th><th class="pr-3 font-medium">{{ t('Capture sessions') }}</th><th class="font-medium">{{ t('Used for') }}</th></tr></thead>
                <tbody class="font-mono tabular-nums"><tr v-for="row in splitRows" :key="row.split" class="border-t border-white/5"><td class="py-2 pr-3 font-sans whitespace-nowrap capitalize">{{ t(row.split) }}</td><td class="pr-3">{{ row.images }}</td><td class="pr-3">{{ row.boxes }}</td><td class="pr-3">{{ row.sessions }}</td><td class="font-sans text-muted-foreground">{{ trainAll ? t('Training') : t(SPLIT_USE[row.split]) }}</td></tr>
                  <tr class="border-t border-white/10 font-semibold"><td class="py-2 pr-3 font-sans">{{ t('Total') }}</td><td class="pr-3">{{ check.counts?.total ?? 0 }}</td><td class="pr-3">{{ check.boxes?.total ?? 0 }}</td><td /><td /></tr></tbody></table></div>
              <p class="text-sm leading-relaxed text-muted-foreground">{{ t('Classes') }} {{ Object.values(check.classes ?? {}).join(', ') || '–' }} · {{ t('labels per class:') }} <span v-for="(count, name) in check.labels_per_class_total" :key="name" class="mr-2 font-mono text-foreground">{{ name }} {{ count }}</span></p>
              <ul v-if="check.errors.length" class="space-y-1.5 text-sm text-red-300"><li v-for="item in check.errors" :key="item" class="flex gap-2"><Ban class="mt-0.5 size-4 shrink-0" />{{ t(item) }}</li></ul>
              <div v-if="check.limits.length" class="flex gap-2 rounded-xl border border-amber-300/20 bg-amber-300/5 p-3 text-sm leading-relaxed text-amber-200"><AlertTriangle class="mt-0.5 size-4 shrink-0" /><p><template v-for="(item, index) in check.limits" :key="item"><br v-if="index">{{ t(item) }}</template></p></div>
              <ul v-if="check.notes.length" class="space-y-1 text-sm leading-relaxed text-muted-foreground"><li v-for="item in check.notes" :key="item">{{ t(item) }}</li></ul>
              <label v-if="check.readiness === 'limited'" class="flex items-start gap-2 rounded-xl border border-amber-300/20 bg-amber-300/5 p-3 text-sm leading-relaxed text-amber-100"><input v-model="form.acknowledge" type="checkbox" class="mt-1 size-4 shrink-0 accent-amber-300"> {{ t('I understand these limitations. Results from this dataset are a trial run and will not be shown as a confirmed improvement.') }}</label>
              <p v-if="trainAll" class="rounded-xl border border-sky-400/20 bg-sky-400/5 p-3 text-sm leading-relaxed text-sky-200">{{ t('Train All: all {total} images (train {train}, val {val}, test {test}) train the weights. A separate training copy is made; the export and its split assignment stay unchanged.', { total: check.counts?.total ?? 0, train: check.counts?.train ?? 0, val: check.counts?.val ?? 0, test: check.counts?.test ?? 0 }) }}</p>
              <p class="text-xs leading-relaxed text-muted-foreground">{{ t('Training uses a frozen copy of this export (sha256 recorded with the job), so later labeling does not change the run.') }}</p>
            </template>
          </section>

          <!-- Settings -->
          <section class="relative min-w-0 space-y-4 rounded-2xl border border-white/[0.12] bg-white/[0.04] p-5 sm:p-6" :inert="!unlocked" :aria-label="t('Fine-tuning settings')">
            <LockedOverlay v-if="!unlocked" />
            <h2 class="flex items-center gap-2.5 text-base font-semibold"><span class="inline-grid size-6 shrink-0 place-items-center rounded-full bg-primary/15 text-xs font-semibold text-primary" aria-hidden="true">2</span>{{ t('Fine-tuning settings') }}</h2>
            <fieldset class="space-y-2"><legend class="mb-2 text-sm font-medium text-foreground/90">{{ t('Training mode') }}</legend>
              <div class="grid gap-2 sm:grid-cols-2">
                <label v-for="mode in (['standard', 'all'] as const)" :key="mode" :class="['flex cursor-pointer gap-3 rounded-xl border p-3 transition-colors', form.trainMode === mode ? 'border-primary/40 bg-primary/5' : 'border-white/10 hover:border-white/20']">
                  <input v-model="form.trainMode" type="radio" name="train-mode" :value="mode" class="mt-1 accent-primary">
                  <span class="space-y-0.5"><span class="block text-sm font-medium">{{ t(TRAIN_MODE_LABELS[mode]) }}<span v-if="mode === 'standard'" class="ml-2 text-xs font-normal text-primary">{{ t('default') }}</span></span>
                    <span class="block text-[0.8125rem] leading-relaxed text-muted-foreground">{{ mode === 'standard' ? t('Train trains the weights, Val picks the best epoch and stops early, Test is compared with the current YOLO11m.') : t('Train, Val and Test all train the weights. Fixed epochs, no early stopping, final-epoch weights. No independent score in this run.') }}</span></span>
                </label>
              </div>
            </fieldset>
            <div class="grid gap-4 sm:grid-cols-2">
              <div class="space-y-1.5"><span class="block text-sm font-medium text-foreground/90">{{ t('Start from') }}</span><DatasetSelect v-model="form.baseModelId" :options="baseOptions" :label="t('Base model')" class="w-full" /></div>
              <label class="space-y-1.5"><span class="block text-sm font-medium text-foreground/90">{{ t('Model name') }}</span><Input v-model="form.name" maxlength="48" :aria-invalid="!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,47}$/.test(form.name)" /></label>
              <label class="space-y-1.5"><span class="block text-sm font-medium text-foreground/90">{{ t('Epochs') }}</span><Input v-model="form.epochs" type="number" inputmode="numeric" :min="options?.limits.epochs[0]" :max="options?.limits.epochs[1]" :disabled="form.smoke" />
                <span v-if="trainAll && !form.smoke" class="block text-xs leading-relaxed text-muted-foreground">{{ epochHint ? t('Standard run {name} peaked at epoch {epoch}.', { name: epochHint.name, epoch: epochHint.epoch }) : t('No early stopping: all epochs run. A standard run on the same data shows a good count.') }} <button v-if="epochHint && toInteger(form.epochs) !== epochHint.epoch" type="button" class="text-primary underline-offset-4 hover:underline" @click="form.epochs = epochHint.epoch">{{ t('Use {count}', { count: epochHint.epoch }) }}</button></span></label>
              <div class="space-y-1.5"><span class="block text-sm font-medium text-foreground/90">{{ t('Image size') }}</span>
                <div class="flex gap-1 rounded-lg bg-white/[0.05] p-1" role="radiogroup" :aria-label="t('Image size')"><button v-for="size in [640, 960] as const" :key="size" type="button" role="radio" :aria-checked="form.imgsz === size" :class="['h-8 flex-1 rounded-md text-sm font-medium transition-colors', form.imgsz === size ? 'bg-primary/15 text-primary' : 'text-muted-foreground hover:text-foreground']" @click="form.imgsz = size">{{ size }}</button></div></div>
            </div>
            <div class="space-y-1.5"><span class="block text-sm font-medium text-foreground/90">{{ t('Classes') }}</span>
              <div class="rounded-xl border border-white/10 p-3"><span class="block text-sm font-medium">{{ t(CLASS_MODE_LABELS.vehicle3) }}</span><span class="block text-[0.8125rem] text-muted-foreground">{{ options?.class_modes.vehicle3 ? t(options.class_modes.vehicle3) : '' }}</span></div>
            </div>
            <details class="rounded-xl border border-white/10" :open="advanced" @toggle="advanced = ($event.target as HTMLDetailsElement).open">
              <summary class="cursor-pointer px-3 py-2.5 text-sm font-medium text-foreground/90">{{ t('Advanced (batch, seed, early stopping, smoke test)') }}</summary>
              <div class="grid gap-3 px-3 pb-3 sm:grid-cols-3">
                <label class="space-y-1.5"><span class="block text-sm font-medium text-foreground/90">{{ t('Batch size') }}</span><Input v-model="form.batch" type="number" inputmode="numeric" min="1" max="32" /></label>
                <label class="space-y-1.5"><span class="block text-sm font-medium text-foreground/90">{{ t('Seed') }}</span><Input v-model="form.seed" type="number" inputmode="numeric" min="0" /></label>
                <label class="space-y-1.5"><span class="block text-sm font-medium text-foreground/90">{{ t('Early-stop patience') }}</span><Input v-model="form.patience" type="number" inputmode="numeric" min="1" max="300" :disabled="trainAll" /><span v-if="trainAll" class="block text-xs text-muted-foreground">{{ t('Off in Train All') }}</span></label>
                <p class="text-xs leading-relaxed text-muted-foreground sm:col-span-3">{{ t('Device: GPU only (training never falls back to CPU). If the first epoch runs out of memory, Ultralytics halves the batch automatically; the final batch is recorded.') }}</p>
                <label class="flex items-start gap-2 text-sm sm:col-span-3"><input v-model="form.smoke" type="checkbox" class="mt-1 size-4 shrink-0 accent-primary"> {{ t('Smoke test: 1 epoch to check the pipeline and GPU memory (not registered as a model)') }}</label>
              </div>
            </details>
            <p class="rounded-xl bg-black/15 p-3 text-sm leading-relaxed text-muted-foreground">{{ t('Estimated training memory {estimate} · needs {needed} free (incl. DeepStream reserve)', { estimate: formatMib(estimate), needed: formatMib(needed) }) }}<template v-if="suggestedBatch !== null"> · {{ t('largest safe batch now:') }} <span :class="suggestedBatch < batchNumber ? 'text-amber-200' : ''">{{ suggestedBatch || t('none') }}</span></template>. {{ t('Estimates come from the 2026-10-13 server measurement.') }}</p>
            <ul v-if="formErrors.length" class="space-y-1 text-sm text-red-300"><li v-for="item in formErrors" :key="item">{{ t(item) }}</li></ul>
          </section>
        </div>

        <!-- Start -->
        <section class="relative space-y-4 rounded-2xl border border-white/[0.12] bg-white/[0.04] p-5 sm:p-6" :inert="!unlocked" :aria-label="t('Start training')">
          <LockedOverlay v-if="!unlocked" />
          <h2 class="flex items-center gap-2.5 text-base font-semibold"><span class="inline-grid size-6 shrink-0 place-items-center rounded-full bg-primary/15 text-xs font-semibold text-primary" aria-hidden="true">3</span>{{ t('Checks before training') }}</h2>
          <ul class="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
            <li v-for="item in checks" :key="item.key" class="flex gap-2.5 rounded-xl border border-white/10 bg-black/10 p-3 text-sm"><component :is="stateIcon[item.state]" :class="['mt-0.5 size-4 shrink-0', stateClass[item.state], item.state === 'pending' ? 'animate-pulse' : '']" /><span class="min-w-0"><span class="block font-medium text-foreground">{{ item.label }}</span><span class="mt-0.5 block text-[0.8125rem] leading-snug text-muted-foreground">{{ item.detail }}</span></span></li>
          </ul>
          <div class="flex flex-wrap items-center gap-3">
            <Button size="lg" :disabled="!canStart" @click="start"><LoaderCircle v-if="starting" class="animate-spin" /><Play v-else /> {{ form.smoke ? t('Start smoke test') : t('Start training') }}</Button>
            <p v-if="startError" class="text-sm text-red-300" role="alert">{{ t(startError) }}</p>
            <p v-else-if="notice" class="text-sm text-primary" role="status">{{ t(notice) }}</p>
            <p v-else class="text-sm text-muted-foreground">{{ t('Runs in the background on the GPU server; closing this page does not stop it.') }}</p>
          </div>
        </section>
      </TabsContent>

      <TabsContent value="progress">
        <!-- Progress / results of the selected job -->
        <section class="space-y-4 rounded-2xl border border-white/[0.12] bg-white/[0.04] p-5 sm:p-6" :aria-label="t('Training progress')">
          <h2 class="text-base font-semibold">{{ t('Progress and results') }}</h2>
          <TrainingJobPanel v-if="detail" :job="detail" :can-control="unlocked" :stopping="stopping" @stop="stop" @download="download" @evaluate="evaluateModel" />
          <p v-else-if="selectedJobId" class="py-6 text-center text-sm text-muted-foreground" role="status"><LoaderCircle class="mr-2 inline size-4 animate-spin" />{{ t('Loading job…') }}</p>
          <p v-else class="rounded-xl border border-dashed border-white/15 py-10 text-center text-sm text-muted-foreground">{{ t('No training has been run yet.') }}</p>
        </section>
      </TabsContent>

      <TabsContent value="evaluate">
        <!-- Independent check on new CCTV images (required after Train All) -->
        <TrainingNewImageEval v-model:model-id="evalModelId" :models="models" :datasets="datasets" :can-control="unlocked" :busy="!!activeJob" :gpu-ready="!!status?.gpu_ready" @created="evaluationCreated" />
      </TabsContent>

      <TabsContent value="history" class="space-y-6">
        <!-- History -->
        <section class="min-w-0 space-y-3 rounded-2xl border border-white/[0.12] bg-white/[0.04] p-5 sm:p-6" :aria-label="t('Training history')">
          <h2 class="text-base font-semibold">{{ t('Training history') }} <span class="ml-1 font-normal text-muted-foreground">{{ jobs.length }}</span></h2>
          <p v-if="!jobs.length" class="py-6 text-center text-sm text-muted-foreground">{{ t('No jobs yet.') }}</p>
          <div v-else class="overflow-x-auto"><table class="w-full min-w-[44rem] text-left text-sm">
            <thead class="text-xs text-muted-foreground"><tr class="whitespace-nowrap"><th class="py-2 pr-4 font-medium">{{ t('Job') }}</th><th class="pr-4 font-medium">{{ t('Status') }}</th><th class="pr-4 font-medium">{{ t('Epochs') }}</th><th class="pr-4 font-medium">{{ t('Best mAP50-95') }}</th><th class="pr-4 font-medium">{{ t('Duration') }}</th><th class="font-medium">{{ t('Created') }}</th></tr></thead>
            <tbody><tr v-for="job in jobs" :key="job.id" :class="['cursor-pointer border-t border-white/5 hover:bg-white/[0.03]', job.id === selectedJobId ? 'bg-primary/5' : '']" tabindex="0" @click="openJob(job.id)" @keydown.enter="openJob(job.id)">
              <td class="py-2.5 pr-4"><span class="font-medium">{{ job.name }}</span> <span :class="['ml-1 rounded px-1.5 py-0.5 text-xs whitespace-nowrap', jobKind(job.config) === 'standard' ? 'bg-white/5 text-muted-foreground' : jobKind(job.config) === 'all' ? 'bg-sky-400/10 text-sky-300' : jobKind(job.config) === 'deploy' ? 'bg-amber-300/10 text-amber-200' : 'bg-violet-400/10 text-violet-300']">{{ t(KIND_LABELS[jobKind(job.config)]) }}</span><span class="block text-xs text-muted-foreground">{{ jobTag(job) }}</span></td>
              <td :class="['pr-4 font-medium whitespace-nowrap', jobStatusClass(job.status)]">{{ t(JOB_STATUS_LABELS[job.status]) }}</td>
              <td class="pr-4 font-mono tabular-nums whitespace-nowrap">{{ ['evaluate', 'deploy'].includes(job.config.kind ?? '') ? '–' : `${job.epoch}/${job.epochs}` }}</td><td class="pr-4 font-mono tabular-nums">{{ formatMetric(job.best?.map50_95) }}</td>
              <td class="pr-4 tabular-nums whitespace-nowrap">{{ formatDuration(job.elapsed_seconds) }}</td><td class="whitespace-nowrap text-muted-foreground">{{ time(job.created_at) }}</td></tr></tbody></table></div>
        </section>

        <!-- Models -->
        <section class="min-w-0 space-y-3 rounded-2xl border border-white/[0.12] bg-white/[0.04] p-5 sm:p-6" :aria-label="t('Models')">
          <h2 class="text-base font-semibold">{{ t('Models') }}</h2>
          <p class="max-w-3xl text-sm leading-relaxed text-muted-foreground">{{ t('Deploy a compatible trained model after export and engine validation. Failed verification restores the previous model. Training data and older models are retained.') }}</p>
          <div class="space-y-2 border-t border-white/10 pt-4 text-sm" role="status" aria-live="polite">
            <p class="text-base">{{ t('Active model') }}: <strong>{{ modelLabel(deployment?.active) }}</strong></p>
            <p v-if="deployment?.active_class_map" class="text-sm text-muted-foreground">{{ t('Class map') }}: <span v-for="(target, source) in deployment.active_class_map" :key="source" class="font-mono">{{ t('model {source} → runtime {target} (car)', { source, target }) }}</span></p>
            <p v-if="!deployment?.confirmed" class="text-amber-200">{{ t('Runtime status is unavailable; the active model is unconfirmed.') }}</p>
            <p>{{ t(DEPLOY_PHASES[deployment?.phase ?? ''] ?? 'Deployment unavailable') }}</p>
            <p v-if="deployment?.error" class="text-amber-200">{{ deployment.error }}</p>
            <DeploymentReport v-if="deployment" :report="deployment" />
            <p v-if="!deployment?.available" class="text-sm text-muted-foreground">{{ t('Deployment must be enabled on the DeepStream runtime and the training worker must be online.') }}</p>
            <Button size="sm" variant="outline" :disabled="!unlocked || !deployment?.available || !deployment?.previous || !!activeJob || deploying" @click="startDeployment()">{{ t('Roll back model') }} · {{ modelLabel(deployment?.previous) }}</Button>
          </div>
          <div class="divide-y divide-white/10 border-t border-white/10">
          <article v-for="model in models" :key="model.id" class="space-y-2.5 py-5 last:pb-0">
            <div class="flex flex-wrap items-center gap-2"><p class="mr-auto text-base font-semibold">{{ model.label }}</p>
              <Badge variant="outline" :class="['px-2 py-0.5', model.kind === 'base' ? 'border-sky-400/25 bg-sky-400/10 text-sky-300' : 'border-primary/25 bg-primary/10 text-primary']">{{ model.kind === 'base' ? t('Production base') : t('Fine-tuned') }}</Badge>
              <Badge v-if="model.kind === 'finetuned'" variant="outline" class="px-2 py-0.5 text-muted-foreground">{{ deployment?.confirmed && deployment.active === model.id ? t('Active model') : t('Not active') }}</Badge></div>
            <p v-if="model.kind === 'base'" class="text-sm text-muted-foreground">{{ t('COCO 80 classes · weights on the training host') }}{{ model.files['yolo11m.pt']?.sha256 ? ` · sha256 ${model.files['yolo11m.pt']!.sha256!.slice(0, 12)}…` : '' }}</p>
            <template v-else>
              <p class="text-sm text-muted-foreground">{{ t('From') }} {{ models.find(item => item.id === model.base_model_id)?.label ?? model.base_model_id }} · dataset_{{ model.export_id?.slice(0, 8) }} · {{ time(model.created_at) }}</p>
              <p class="text-sm text-muted-foreground">{{ model.config?.epochs_run }}/{{ model.config?.epochs }} {{ t('epochs') }} · {{ model.config?.imgsz }} px · {{ t('batch') }} {{ model.config?.final_batch ?? model.config?.batch }} · {{ model.class_mode === 'single' ? t('single class') : 'car/truck/bus' }}</p>
              <p v-if="model.config?.train_mode === 'all'" class="text-sm text-sky-200">{{ t('Train All · {count} images · final-epoch weights · no validation or test in this run', { count: model.config?.train_images ?? '–' }) }}</p>
              <p v-else class="rounded-lg bg-white/[0.04] px-2.5 py-1.5 font-mono text-[0.8125rem] tabular-nums">val mAP50 {{ formatMetric(model.metrics?.val?.map50) }} · mAP50-95 {{ formatMetric(model.metrics?.val?.map50_95) }} · test mAP50 {{ formatMetric(model.metrics?.test?.vehicle?.mAP50) }} <span class="text-muted-foreground">(YOLO11m {{ formatMetric(model.metrics?.test_baseline?.vehicle?.mAP50) }})</span></p>
              <p v-if="model.metrics?.new_images?.length" class="rounded-lg bg-white/[0.04] px-2.5 py-1.5 font-mono text-[0.8125rem] tabular-nums">{{ t('New images') }}: mAP50 {{ formatMetric(model.metrics.new_images.at(-1)?.new?.vehicle?.mAP50) }} <span class="text-muted-foreground">(YOLO11m {{ formatMetric(model.metrics.new_images.at(-1)?.baseline?.vehicle?.mAP50) }}) · {{ t('{count} images', { count: model.metrics.new_images.at(-1)?.images ?? 0 }) }}</span></p>
              <p v-if="model.config?.train_mode === 'all' && !model.metrics?.new_images?.length" class="text-sm text-amber-200">{{ t('Not evaluated yet: score it on new CCTV images before relying on it.') }}</p>
              <p v-else-if="!model.evaluation_conclusive && !model.metrics?.new_images?.some(item => item.conclusive)" class="text-sm text-amber-200">{{ t('Test evidence is insufficient; improvement is not confirmed.') }}</p>
              <div class="flex flex-wrap items-center gap-2 pt-1">
                <Button v-for="name in ['best.pt', 'last.pt']" :key="name" size="sm" variant="outline" :disabled="!unlocked || !model.files[name]?.download_url" :title="unlocked ? fileSize(model.files[name]?.size) : t('Unlock training control to download')" @click="download(`${model.name}-v${model.version}-${name}`, model.files[name]!.download_url!)"><Download /> {{ name }}</Button>
                <Button size="sm" variant="outline" :disabled="!unlocked || deploying || !!deployBlocker(model, deployment, !!activeJob)" :title="unlocked ? (deployBlocker(model, deployment, !!activeJob) ? t(deployBlocker(model, deployment, !!activeJob)!) : undefined) : t('Unlock training control first')" @click="startDeployment(model.id)">{{ t('Deploy to DeepStream') }}</Button>
                <span class="text-xs text-muted-foreground">{{ t(deployClasses(model.class_mode)) }}</span>
                <Button size="sm" variant="outline" :disabled="model.status !== 'available'" @click="evaluateModel(model.id)">{{ t('Evaluate on new images') }}</Button>
                <button v-if="model.job_id" type="button" class="text-xs text-primary underline-offset-4 hover:underline" @click="openJob(model.job_id)">{{ t('Open training run') }}</button>
              </div>
            </template>
          </article>
          </div>
        </section>
      </TabsContent>
    </Tabs>
  </main>
</template>
