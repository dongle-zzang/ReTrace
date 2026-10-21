<script setup lang="ts">
import { FolderDown, LoaderCircle, Plus, RefreshCw, Settings2, Sparkles, Trash2, X } from '@lucide/vue'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '~/components/ui/tabs'
import DatasetAutoLabelDialog from '~/components/DatasetAutoLabelDialog.vue'
import DatasetSelect from '~/components/DatasetSelect.vue'
import DatasetThumbnail from '~/components/DatasetThumbnail.vue'
import { Button } from '~/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '~/components/ui/dialog'
import { Badge } from '~/components/ui/badge'
import CameraVideo from '~/components/CameraVideo.vue'
import { useCameras } from '~/composables/useCameras'
import { useRealtimePreview } from '~/composables/useRealtimePreview'
import { useI18n } from '~/composables/useI18n'
import { datasetRequest } from '~/composables/datasetApi'
import { trainingRequest, useTrainingToken } from '~/composables/trainingApi'
import { autoLabelErrorMessage, startAutoLabel } from '~/composables/autolabelApi'
import { autoLabelPercent, isAutoLabelActive, readAutoLabelSettings } from '~/lib/datasetAutoLabel'
import { AUTO_LABEL_DEMO_QUERY, demoJob, isAutoLabelDemo } from '~/lib/autolabelDemo'
import { remapDatasetPage } from '~/lib/datasetNavigation'
import type { AutoLabelJob, DatasetImage, DatasetPage, DatasetExport } from '~/types/dataset'
import { DATASET_STATUS_LABELS } from '~/types/dataset'

const { t, dateLocale } = useI18n()
const route = useRoute()
const demo = isAutoLabelDemo(route.query)
// Collect and label are separate tabs; the choice lives in the URL so returning from the editor keeps it.
const tab = computed({
  get: () => route.query.tab === 'collect' ? 'collect' : 'label',
  set: (value: string | number) => { void navigateTo({ query: { ...route.query, tab: value === 'collect' ? 'collect' : undefined } }, { replace: true }) },
})
const exportOpen = ref(false)
const imageLink = (id: string) => demo ? { path: `/datasets/images/${id}`, query: AUTO_LABEL_DEMO_QUERY } : `/datasets/images/${id}`
useHead({ title: () => `ReTrace · ${t('AI datasets')}` })
const { cameras, loading: camerasLoading, error: cameraError } = useCameras()
const { client, connection, statuses } = useRealtimePreview(useRuntimeConfig().public.previewVideoFps)
const parkingCameras = computed(() => cameras.value.filter(item => item.name.includes('주차장')))
const cameraId = ref('')
const camera = computed(() => parkingCameras.value.find(c => c.camera_id === cameraId.value))
watch(parkingCameras, rows => { if (!rows.some(c => c.camera_id === cameraId.value && c.enabled)) cameraId.value = rows.find(c => c.enabled)?.camera_id ?? '' }, { immediate: true })
const navigation = useDatasetNavigation()
const { listCamera: filterCamera, listStatus: filterStatus, listPage: page, listPageSize: pageSize } = navigation
let listReady = false
// Infinite scroll: `page` counts the loaded batches, so returning from the editor restores the same list length.
const PAGE_SIZE = 24
const visibleImages = computed(() => images.value.items)
const hasMore = computed(() => images.value.items.length < images.value.total)
const loadingMore = ref(false)
const sentinel = ref<HTMLElement | null>(null)
let sentinelVisible = false
let observer: IntersectionObserver | undefined
watch(sentinel, (element, previous) => {
  if (previous) observer?.unobserve(previous)
  if (element) observer?.observe(element)
})
const openingImage = ref(false)
let viewAlive = true
async function openImage(event: MouseEvent, id: string) {
  if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return
  event.preventDefault()
  if (openingImage.value || loading.value) return
  openingImage.value = true
  try {
    await navigation.prepare(images.value, filterCamera.value, filterStatus.value, id)
    if (viewAlive) await navigateTo(imageLink(id))
  } catch (cause) { error.value = (cause as Error).message }
  finally { openingImage.value = false }
}
const images = ref<DatasetPage>({ items: [], total: 0, page: 1, page_size: PAGE_SIZE })
const exports = ref<DatasetExport[]>([])
const loading = ref(false), capturing = ref(false), exporting = ref(false), deleting = ref<string | null>(null)
const error = ref<string | null>(null), message = ref<string | null>(null)
const exportDeleting = ref<string | null>(null)
const { confirm } = useConfirmDialog()
async function removeExport(job: DatasetExport) {
  if (exportDeleting.value || job.status === 'building') return
  if (!await confirm({ title: t('Delete dataset export?'), description: t('Delete this export record and its ZIP file? Captured images and labels will be kept.'), confirmLabel: t('Delete export'), destructive: true })) return
  exportDeleting.value = job.id
  try { await datasetRequest('/exports/' + job.id, { method: 'DELETE' }); await refreshExports() }
  catch (cause) { error.value = (cause as Error).message }
  finally { exportDeleting.value = null }
}
const deleteDialogOpen = ref(false)
const closingDeleteBusy = ref(false)
const deleteBusy = computed(() => deleteDialogOpen.value ? !!deleting.value : closingDeleteBusy.value)
const deleteTarget = ref<DatasetImage | null>(null)
const deleteError = ref<string | null>(null)
function closeDeleteDialog() { closingDeleteBusy.value = !!deleting.value; deleteDialogOpen.value = false }
function setDeleteOpen(open: boolean) { if (!open && !deleting.value) closeDeleteDialog() }
function requestDelete(image: DatasetImage) { deleteError.value = null; closingDeleteBusy.value = false; deleteTarget.value = image; deleteDialogOpen.value = true }
const captureResult = ref<DatasetImage | null>(null)
// The capture notice belongs to the camera it was taken from.
watch(cameraId, () => { captureResult.value = null; message.value = null })
let requestVersion = 0
let timer: ReturnType<typeof setInterval> | undefined
const cameraFilterOptions = computed(() => [{ value: 'all', label: t('All cameras') }, ...parkingCameras.value.map(item => ({ value: item.camera_id, label: item.name }))])
const statusOptions = computed(() => [{ value: 'all', label: t('All states') }, ...Object.entries(DATASET_STATUS_LABELS).map(([value, label]) => ({ value, label: t(label) }))])
function fetchPage(number: number) {
  const query = new URLSearchParams({ page: String(number), page_size: String(PAGE_SIZE) })
  if (filterCamera.value) query.set('camera_id', filterCamera.value)
  if (filterStatus.value) query.set('status', filterStatus.value)
  return datasetRequest<DatasetPage>('/images?' + query)
}
function uniqueImages(items: DatasetImage[]) {
  const seen = new Set<string>()
  return items.filter(image => !seen.has(image.id) && !!seen.add(image.id))
}
function setLoadedPages(count: number) { page.value = count }
// Reloads every batch already on screen, dropping batches that no longer exist after deletes.
async function refresh() {
  const version = ++requestVersion
  loading.value = true; error.value = null
  try {
    const count = Math.max(1, page.value)
    const pages = await Promise.all(Array.from({ length: count }, (_, index) => fetchPage(index + 1)))
    if (version !== requestVersion) return
    const total = pages.at(-1)!.total
    const loaded = Math.max(1, Math.min(count, Math.ceil(total / PAGE_SIZE)))
    setLoadedPages(loaded)
    images.value = { items: uniqueImages(pages.slice(0, loaded).flatMap(data => data.items)), total, page: 1, page_size: PAGE_SIZE }
  } catch (cause) { if (version === requestVersion) error.value = (cause as Error).message }
  finally { if (version === requestVersion) { loading.value = false; fillViewport() } }
}
async function loadMore() {
  if (loading.value || loadingMore.value || !hasMore.value) return
  const version = requestVersion
  loadingMore.value = true
  try {
    const data = await fetchPage(page.value + 1)
    if (version !== requestVersion) return
    setLoadedPages(page.value + 1)
    images.value = { ...images.value, items: uniqueImages([...images.value.items, ...data.items]), total: data.total }
  } catch (cause) { if (version === requestVersion) error.value = (cause as Error).message }
  finally { loadingMore.value = false; if (version === requestVersion) fillViewport() }
}
// The observer only fires on changes, so keep loading while the end of the list is still on screen.
function fillViewport() { if (sentinelVisible && hasMore.value && !error.value) void nextTick(loadMore) }
async function refreshExports() {
  try { exports.value = await datasetRequest<DatasetExport[]>('/exports') } catch (cause) { error.value = (cause as Error).message }
}
watch([filterCamera, filterStatus], () => {
  setLoadedPages(1)
  if (listReady) void refresh()
}, { flush: 'sync' })
async function capture() {
  if (!camera.value || capturing.value) return
  capturing.value = true; error.value = null; message.value = null
  try {
    const capturedFrom = cameraId.value
    const result = await datasetRequest<DatasetImage>('/capture', { method: 'POST', body: { camera_id: capturedFrom } })
    if (cameraId.value === capturedFrom) {
      captureResult.value = result
      message.value = 'Clean frame captured. Open it to label vehicles.'
    }
    await refresh()
  } catch (cause) { error.value = (cause as Error).message }
  finally { capturing.value = false }
}
async function remove(image: DatasetImage) {
  if (deleting.value) return
  deleteError.value = null
  deleting.value = image.id
  try { await datasetRequest('/images/' + image.id, { method: 'DELETE' }); if (captureResult.value?.id === image.id) { captureResult.value = null; message.value = null }; closeDeleteDialog(); await refresh() }
  catch (cause) { deleteError.value = (cause as Error).message }
  finally { deleting.value = null }
}
async function exportDataset() {
  if (exporting.value) return
  exporting.value = true; error.value = null
  try { await datasetRequest('/exports', { method: 'POST', body: { seed: 42 } }); await refreshExports() }
  catch (cause) { error.value = (cause as Error).message }
  finally { exporting.value = false }
}
// Auto-label job: runs on the training host GPU; the list refreshes when it finishes.
const AUTO_LABEL_JOB_KEY = 'retrace.autolabelJob'
const autoLabelOpen = ref(false)
const autoJob = ref<AutoLabelJob | null>(null)
const autoActive = computed(() => !!autoJob.value && isAutoLabelActive(autoJob.value.status))
let autoTimer: ReturnType<typeof setTimeout> | undefined
function rememberAutoJob(id: string | null) {
  try { if (id) sessionStorage.setItem(AUTO_LABEL_JOB_KEY, id); else sessionStorage.removeItem(AUTO_LABEL_JOB_KEY) } catch { /* private mode */ }
}
let demoState: ReturnType<typeof demoJob> | null = null
async function pollAutoJob(id: string) {
  clearTimeout(autoTimer)
  if (demoState) {
    autoJob.value = demoState()
    if (isAutoLabelActive(autoJob.value.status)) autoTimer = setTimeout(() => pollAutoJob(id), 500)
    return
  }
  try {
    const job = await trainingRequest<AutoLabelJob>('/jobs/' + id)
    if (!viewAlive) return
    const wasActive = autoActive.value
    autoJob.value = job
    if (isAutoLabelActive(job.status)) { autoTimer = setTimeout(() => pollAutoJob(id), 2000); if ((job.dataset_summary?.labeled ?? 0) > 0) void refresh() }
    else if (wasActive) void refresh()
  } catch { if (viewAlive && autoActive.value) autoTimer = setTimeout(() => pollAutoJob(id), 5000) }
}
// One click: reuse the last model/confidence for every unlabeled image of the current camera filter.
// The settings dialog opens instead the first time, or when the admin token is missing or rejected.
const { token: trainingToken, setToken: setTrainingToken } = useTrainingToken()
const quickStarting = ref(false)
async function quickAutoLabel() {
  const settings = readAutoLabelSettings()
  if (!settings || (!demo && !trainingToken.value)) { autoLabelOpen.value = true; return }
  if (demo) { autoLabelStarted({ id: 'demo-job', status: 'queued', phase: 'queued', message: null, progress: 0, error: null, config: { kind: 'autolabel', conf: settings.conf, images: Math.max(images.value.total, 6) }, dataset_summary: null, created_at: new Date().toISOString() }); return }
  quickStarting.value = true; error.value = null
  try { autoLabelStarted(await startAutoLabel({ model_id: settings.model_id, conf: settings.conf, camera_id: filterCamera.value || null, include_auto_drafts: false })) }
  catch (cause) {
    if ((cause as { status?: number }).status === 401) { setTrainingToken(''); autoLabelOpen.value = true }
    else error.value = autoLabelErrorMessage(cause)
  } finally { quickStarting.value = false }
}
function autoLabelStarted(job: AutoLabelJob) {
  if (demo) { demoState = demoJob(job.config.images ?? 0, job.config.conf ?? 0.35); void pollAutoJob(job.id); return }
  autoJob.value = job; rememberAutoJob(job.id); void pollAutoJob(job.id)
}
function dismissAutoJob() { autoJob.value = null; rememberAutoJob(null) }
async function resumeAutoJob() {
  if (demo) return
  let id: string | null = null
  try { id = sessionStorage.getItem(AUTO_LABEL_JOB_KEY) } catch { /* private mode */ }
  try {
    // A job started in another tab still shows up while it runs.
    const status = await trainingRequest<{ active_job: AutoLabelJob | null }>('/status')
    if (status.active_job?.config.kind === 'autolabel') id = status.active_job.id
  } catch { /* training API unavailable: auto-labeling is simply not offered */ }
  if (id && viewAlive) void pollAutoJob(id)
}
const cameraLive = (item: { camera_id: string; status: { state: string } }) => ['online', 'degraded'].includes(statuses.value[item.camera_id]?.state ?? item.status.state)
const cameraName = (id: string) => cameras.value.find(c => c.camera_id === id)?.name ?? id
const time = (date: string) => new Date(date).toLocaleString(dateLocale.value)
// Same staggered entrance as the monitoring and training pages; tab panels remount, so they replay on tab switch.
function enterDelay(index: number, step = 70, base = 0) {
  return { animationDelay: `${base + Math.min(index, 12) * step}ms` }
}
onMounted(() => {
  // Older sessions stored smaller pages; keep the first image they showed within the loaded batches.
  if (pageSize.value !== PAGE_SIZE) { setLoadedPages(remapDatasetPage(page.value, pageSize.value, PAGE_SIZE)); pageSize.value = PAGE_SIZE }
  if (typeof IntersectionObserver !== 'undefined') {
    observer = new IntersectionObserver(([entry]) => { sentinelVisible = !!entry?.isIntersecting; fillViewport() }, { rootMargin: '600px 0px' })
    if (sentinel.value) observer.observe(sentinel.value)
  }
  listReady = true; void refresh(); void refreshExports(); timer = setInterval(() => { if (exports.value.some(job => job.status === 'building')) void refreshExports() }, 2500); void resumeAutoJob() })
onUnmounted(() => { listReady = false; observer?.disconnect(); viewAlive = false; requestVersion++; if (timer) clearInterval(timer); clearTimeout(autoTimer) })
</script>

<template>
  <main class="relative mx-auto max-w-[1500px] space-y-6 px-5 py-6 sm:px-8 lg:px-10">
    <h1 class="sr-only">{{ t('AI datasets') }}</h1>
    <div v-if="error || cameraError" class="flex flex-wrap items-center gap-3 rounded-xl border border-red-400/20 bg-red-400/5 p-4 text-sm text-red-300" role="alert"><span class="min-w-0 flex-1">{{ t(error || cameraError || '') }}</span><Button size="sm" variant="ghost" @click="refresh(); refreshExports()"><RefreshCw /> {{ t('Retry') }}</Button></div>

    <Tabs v-model="tab" class="flex-col gap-6">
      <div class="flex flex-wrap items-center gap-3">
        <TabsList class="grid h-auto w-full grid-cols-2 gap-1 rounded-xl border border-white/10 bg-white/[0.04] p-1 sm:inline-flex sm:w-fit sm:justify-start" :aria-label="t('Dataset sections')">
          <TabsTrigger value="collect" :style="enterDelay(0, 50)" class="h-9 flex-none cursor-pointer rounded-lg px-4 motion-safe:animate-fade-up data-active:bg-primary/15 data-active:text-primary dark:data-active:border-primary/30 dark:data-active:bg-primary/15 dark:data-active:text-primary">{{ t('Collect images') }}</TabsTrigger>
          <TabsTrigger value="label" :style="enterDelay(1, 50)" class="h-9 flex-none cursor-pointer rounded-lg px-4 motion-safe:animate-fade-up data-active:bg-primary/15 data-active:text-primary dark:data-active:border-primary/30 dark:data-active:bg-primary/15 dark:data-active:text-primary">{{ t('Label & export') }}</TabsTrigger>
        </TabsList>
        <!-- Dataset-wide actions sit with the tabs, apart from the image filters they do not depend on. -->
        <div v-if="tab === 'label'" class="ml-auto flex items-center gap-2">
          <div role="group" :aria-label="t('Auto-label')" :style="enterDelay(2, 50)" class="motion-safe:animate-fade-up flex h-9 items-center gap-0.5 rounded-lg border border-white/10 bg-white/[0.04] p-0.5 shadow-sm"><Button size="sm" variant="ghost" class="h-8 rounded-md px-3 text-sm" :disabled="autoActive || quickStarting" :title="t('Auto-label every unlabeled image in this filter')" @click="quickAutoLabel"><LoaderCircle v-if="quickStarting" class="animate-spin" /><Sparkles v-else /> {{ t('Auto-label') }}</Button><span class="h-5 w-px bg-white/10" aria-hidden="true" /><Button size="icon" variant="ghost" class="rounded-md" :disabled="autoActive" :aria-label="t('Auto-label settings')" :title="t('Auto-label settings')" @click="autoLabelOpen = true"><Settings2 /></Button></div>
          <Button size="icon-lg" variant="outline" :style="enterDelay(3, 50)" class="motion-safe:animate-fade-up rounded-lg border-white/10 bg-white/[0.04] shadow-sm" :aria-label="t('Export dataset')" :title="t('Export dataset')" @click="exportOpen = true"><FolderDown /></Button>
        </div>
      </div>
      <TabsContent value="collect">
        <!-- Like the label editor, the preview fills the rest of the viewport (letterboxed) on large screens. -->
        <div class="flex flex-col gap-4 lg:h-[calc(100dvh-14.5rem)] lg:min-h-[28rem] lg:flex-row">
          <section :style="enterDelay(0, 70, 120)" class="motion-safe:animate-fade-up flex min-w-0 flex-col overflow-hidden rounded-2xl border border-white/10 bg-white/[0.03] lg:flex-1">
            <div class="flex items-center gap-2 px-4 py-3"><h2 class="min-w-0 truncate text-sm font-semibold">{{ camera?.name ?? t('Collect images') }}</h2><span v-if="camera" class="size-2 shrink-0 rounded-full" :class="cameraLive(camera) ? 'bg-primary' : 'bg-muted-foreground/50'" aria-hidden="true" /></div>
            <div class="relative aspect-video w-full bg-black/60 lg:aspect-auto lg:min-h-0 lg:flex-1"><CameraVideo v-if="camera" :key="camera.camera_id" :camera="camera" :realtime-client="client" :connection="connection" :camera-state="statuses[camera.camera_id]?.state ?? camera.status.state" :active="true" /><p v-else class="absolute inset-0 grid place-items-center text-sm text-muted-foreground">{{ camerasLoading ? t('Loading cameras…') : t('No enabled cameras') }}</p></div>
            <div class="flex shrink-0 flex-wrap items-center justify-between gap-3 p-4"><div class="flex h-10 min-w-0 flex-1 basis-full flex-col sm:basis-0 justify-center text-xs leading-relaxed" role="status"><p v-if="!message" class="text-muted-foreground">{{ t('The live preview includes detection overlays. Captures are saved separately without overlays.') }}</p><template v-else><p class="truncate text-primary">{{ t(message) }}</p><NuxtLink v-if="captureResult" :to="imageLink(captureResult.id)" class="w-fit text-primary underline underline-offset-4" @click="openImage($event, captureResult.id)">{{ t('Label image') }}</NuxtLink></template></div><Button :disabled="!camera || capturing || camerasLoading" @click="capture"><LoaderCircle v-if="capturing" class="animate-spin" /> {{ capturing ? t('Capturing…') : t('Capture current frame') }}</Button></div>
          </section>
          <aside :style="enterDelay(1, 70, 120)" class="motion-safe:animate-fade-up relative min-w-0 lg:w-60 lg:flex-none xl:w-72" :aria-label="t('Parking cameras')">
            <ul class="flex gap-3 overflow-x-auto pb-1 lg:absolute lg:inset-0 lg:flex-col lg:justify-center-safe lg:overflow-x-visible lg:overflow-y-auto lg:pb-0 lg:pr-1">
              <li v-for="item in parkingCameras" :key="item.camera_id" class="w-44 shrink-0 lg:w-auto">
                <button type="button" class="group block w-full overflow-hidden rounded-xl border text-left outline-none transition-colors focus-visible:ring-2 focus-visible:ring-primary/40 disabled:cursor-not-allowed disabled:opacity-40" :class="item.camera_id === cameraId ? 'border-primary/60 bg-primary/10' : 'border-white/10 bg-white/[0.03] hover:border-white/25'" :disabled="!item.enabled || capturing" :aria-pressed="item.camera_id === cameraId" @click="cameraId = item.camera_id">
                  <div class="pointer-events-none relative aspect-video bg-black/60"><CameraVideo v-if="item.enabled && item.camera_id !== cameraId" :camera="item" :realtime-client="client" :connection="connection" :camera-state="statuses[item.camera_id]?.state ?? item.status.state" :active="true" /><p v-else-if="item.camera_id === cameraId" class="absolute inset-0 grid place-items-center text-[11px] font-medium text-primary">{{ t('Showing') }}</p></div>
                  <p class="flex items-center gap-1.5 px-2.5 py-2 text-xs font-medium"><span class="size-1.5 shrink-0 rounded-full" :class="cameraLive(item) ? 'bg-primary' : 'bg-muted-foreground/50'" aria-hidden="true" /><span class="truncate">{{ item.name }}</span></p>
                </button>
              </li>
            </ul>
          </aside>
        </div>
      </TabsContent>
      <TabsContent value="label" class="space-y-6">
        <p v-if="demo" class="motion-safe:animate-fade-up rounded-xl border border-amber-300/30 bg-amber-300/10 px-4 py-2 text-xs text-amber-100" role="note">{{ t('Auto-label demo (dev only): the model, progress and draft boxes are fake. Nothing is sent to the server or saved.') }}</p>
        <section class="space-y-4 [overflow-anchor:none]" :aria-label="t('Captured images')" :aria-busy="loading || openingImage">
          <div :style="enterDelay(0, 70, 120)" class="motion-safe:animate-fade-up flex flex-wrap items-center gap-3">
            <div class="mr-auto flex items-center gap-1.5"><h2 class="text-base font-semibold">{{ t('Captured images') }} <span class="ml-1.5 text-sm font-normal text-muted-foreground">{{ images.total }}</span></h2><Button size="icon-sm" variant="ghost" :aria-label="t('Refresh images')" :title="t('Refresh images')" :disabled="loading" @click="refresh"><RefreshCw :class="loading ? 'animate-spin' : ''" /></Button></div>
            <DatasetSelect :model-value="filterCamera || 'all'" :options="cameraFilterOptions" :label="t('Filter images by camera')" @update:model-value="filterCamera = $event === 'all' ? '' : $event" /><DatasetSelect :model-value="filterStatus || 'all'" :options="statusOptions" :label="t('Filter images by status')" @update:model-value="filterStatus = $event === 'all' ? '' : $event" />
          </div>
          <div v-if="autoJob" class="motion-safe:animate-fade-up flex flex-wrap items-center gap-3 rounded-xl border px-4 py-3 text-xs" :class="autoJob.status === 'failed' ? 'border-red-400/20 bg-red-400/5' : 'border-primary/20 bg-primary/5'" role="status">
            <LoaderCircle v-if="autoActive" class="size-4 animate-spin text-primary" /><Sparkles v-else class="size-4" :class="autoJob.status === 'failed' ? 'text-red-300' : 'text-primary'" />
            <div class="min-w-0 flex-1 space-y-1.5">
              <p class="font-medium">
                <template v-if="autoActive">{{ autoJob.status === 'queued' ? t('Auto-labeling is waiting for the training GPU…') : t('Auto-labeling {processed} / {total}', { processed: autoJob.dataset_summary?.processed ?? 0, total: autoJob.dataset_summary?.total ?? autoJob.config.images ?? 0 }) }}</template>
                <template v-else-if="autoJob.status === 'completed'">{{ t('Auto-labeling finished: {labeled} images got {boxes} draft boxes.', { labeled: autoJob.dataset_summary?.labeled ?? 0, boxes: autoJob.dataset_summary?.boxes ?? 0 }) }}</template>
                <template v-else-if="autoJob.status === 'stopped'">{{ t('Auto-labeling was stopped.') }}</template>
                <template v-else>{{ t('Auto-labeling failed.') }}</template>
              </p>
              <div v-if="autoActive" class="h-1.5 overflow-hidden rounded-full bg-white/10"><div class="h-full rounded-full bg-primary transition-[width] duration-500" :style="{ width: `${autoLabelPercent(autoJob)}%` }" /></div>
              <p v-else-if="autoJob.status === 'completed'" class="text-muted-foreground">{{ t('Open each image, fix or delete the drafts, then save to mark it reviewed.') }}<template v-if="autoJob.dataset_summary?.no_detections"> · {{ t('{count} images had no vehicles found', { count: autoJob.dataset_summary.no_detections }) }}</template><template v-if="autoJob.dataset_summary?.skipped_changed"> · {{ t('{count} skipped because they were edited meanwhile', { count: autoJob.dataset_summary.skipped_changed }) }}</template></p>
              <p v-else-if="autoJob.error" class="text-red-300">{{ autoJob.error }}</p>
            </div>
            <Button v-if="autoJob.status === 'completed'" size="sm" variant="ghost" @click="filterStatus = 'in_progress'">{{ t('Show drafts') }}</Button>
            <Button v-if="!autoActive" size="icon-sm" variant="ghost" :aria-label="t('Dismiss')" @click="dismissAutoJob"><X /></Button>
          </div>
          <p v-if="loading && !images.items.length" class="py-8 text-center text-sm text-muted-foreground" role="status">{{ t('Loading images…') }}</p>
          <p v-else-if="!loading && !images.items.length" class="motion-safe:animate-fade-up rounded-2xl border border-dashed border-white/15 py-16 text-center text-sm text-muted-foreground">{{ t('No captured images here yet.') }}</p>
          <div v-else class="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <article v-for="(image, index) in visibleImages" :key="image.id" :style="enterDelay(index % PAGE_SIZE, 50, 180)" class="motion-safe:animate-fade-up overflow-hidden rounded-xl border border-white/10 bg-white/[0.03] transition-colors hover:border-primary/40">
              <NuxtLink :to="imageLink(image.id)" @click="openImage($event, image.id)" class="block"><DatasetThumbnail :image="image" :alt="t('Captured frame from {camera}', { camera: cameraName(image.camera_id) })" /></NuxtLink>
              <div class="space-y-3 p-3"><div class="flex min-w-0 items-center justify-between gap-2"><p class="truncate text-sm font-medium">{{ cameraName(image.camera_id) }}</p><Badge variant="outline" :class="image.status === 'reviewed' ? 'text-primary' : 'text-muted-foreground'"><Sparkles v-if="image.auto_box_count" class="size-3 text-amber-200" :aria-label="t('Has auto-label drafts')" /><template v-if="image.box_count"><span class="tabular-nums">{{ image.box_count }}</span><span aria-hidden="true">·</span></template>{{ t(DATASET_STATUS_LABELS[image.status]) }}</Badge></div><time class="block text-xs text-muted-foreground">{{ time(image.captured_at) }}</time><div class="flex items-center justify-between gap-2"><Button as-child size="sm" variant="outline"><NuxtLink :to="imageLink(image.id)" @click="openImage($event, image.id)">{{ image.status === 'pending' ? t('Start labeling') : t('Open image') }}</NuxtLink></Button><Button size="icon-sm" variant="ghost" :disabled="!!deleting" :aria-label="t('Delete image captured at {time}', { time: time(image.captured_at) })" @click="requestDelete(image)"><Trash2 class="text-muted-foreground" /></Button></div></div>
            </article>
          </div>
          <div v-if="images.items.length" ref="sentinel" class="flex justify-center py-2">
            <p v-if="loadingMore" class="flex items-center gap-2 text-xs text-muted-foreground" role="status"><LoaderCircle class="size-3.5 animate-spin" /> {{ t('Loading more images…') }}</p>
            <Button v-else-if="hasMore" size="sm" variant="ghost" :disabled="loading" @click="loadMore">{{ t('Load more images') }}</Button>
          </div>
        </section>
      </TabsContent>
    </Tabs>
    <DatasetAutoLabelDialog v-model:open="autoLabelOpen" :demo="demo" :pending="images.total" :camera-id="filterCamera" :camera-options="parkingCameras.map(item => ({ value: item.camera_id, label: item.name }))" @started="autoLabelStarted" />
    <Dialog v-model:open="exportOpen">
      <DialogContent class="border border-white/10 bg-popover/95 shadow-xl backdrop-blur-xl sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>{{ t('YOLO dataset exports') }}</DialogTitle>
          <DialogDescription>{{ t('Only reviewed images are included. Capture sessions stay together across train / val / test.') }} · {{ t('Car detection · 0 car') }}</DialogDescription>
        </DialogHeader>
        <p v-if="!exports.length" class="text-xs text-muted-foreground">{{ t('Review your labels, then create a dataset ZIP.') }}</p>
        <div v-else class="-mx-1 max-h-[50vh] space-y-3 overflow-y-auto px-1">
          <article v-for="job in exports" :key="job.id" class="space-y-2 rounded-xl border border-white/10 bg-black/10 p-3">
            <div class="flex items-center justify-between gap-2"><time class="text-xs text-muted-foreground">{{ time(job.created_at) }}</time><Badge variant="outline" class="px-2 py-0.5 capitalize" :class="job.status === 'ready' ? 'border-primary/25 bg-primary/10 text-primary' : job.status === 'failed' ? 'border-red-400/25 bg-red-400/10 text-red-300' : 'border-amber-300/25 bg-amber-300/10 text-amber-200'">{{ job.status === 'ready' ? t('Ready') : job.status === 'building' ? t('Building') : t('Failed') }}</Badge></div>
            <p v-if="job.manifest?.classes && Object.keys(job.manifest.classes).length > 1" class="text-xs text-muted-foreground">{{ t('Legacy export') }} · {{ Object.values(job.manifest.classes).join(', ') }}</p>
            <p v-if="job.manifest" class="text-xs">{{ t('Train {train} · Val {val} · Test {test}', { train: job.manifest.counts.train ?? 0, val: job.manifest.counts.val ?? 0, test: job.manifest.counts.test ?? 0 }) }}</p>
            <p v-if="job.status === 'building'" class="text-xs text-muted-foreground" role="status">{{ t('Building archive…') }}</p><p v-if="job.error" class="text-xs text-red-300">{{ job.error }}</p>
            <p v-for="warning in job.manifest?.warnings ?? []" :key="warning" class="text-xs text-amber-200/80">{{ warning }}</p>
            <div class="flex items-center justify-between gap-2"><Button v-if="job.download_url" as-child size="sm" variant="outline" class="h-8 px-3"><a :href="job.download_url" download>{{ t('Download ZIP') }}</a></Button><span v-else /><Button size="icon-sm" variant="destructive" :aria-label="t('Delete export from {time}', { time: time(job.created_at) })" :disabled="job.status === 'building' || !!exportDeleting" @click="removeExport(job)"><LoaderCircle v-if="exportDeleting === job.id" class="animate-spin" /><Trash2 v-else /></Button></div>
          </article>
        </div>
        <p v-if="error" class="text-sm text-red-300" role="alert">{{ t(error) }}</p>
        <DialogFooter>
          <Button :disabled="exporting || exports.some(job => job.status === 'building')" @click="exportDataset"><LoaderCircle v-if="exporting" class="animate-spin" /><Plus v-else /> {{ t('Create dataset') }}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
    <Dialog :open="deleteDialogOpen" @update:open="setDeleteOpen">
      <DialogContent :show-close-button="!deleteBusy" class="border border-white/10 bg-popover/95 shadow-xl backdrop-blur-xl" @escape-key-down="deleting && $event.preventDefault()" @pointer-down-outside="deleting && $event.preventDefault()">
        <DialogHeader>
          <DialogTitle>{{ t('Delete captured image?') }}</DialogTitle>
          <DialogDescription>{{ t('The original image, thumbnail and all its labels will be permanently deleted. Existing dataset ZIPs keep their exported copy.') }}</DialogDescription>
        </DialogHeader>
        <p v-if="deleteTarget" class="text-xs text-muted-foreground">{{ cameraName(deleteTarget.camera_id) }} · {{ time(deleteTarget.captured_at) }}</p>
        <p v-if="deleteError" role="alert" class="text-sm text-red-300">{{ t(deleteError) }}</p>
        <DialogFooter>
          <Button variant="outline" :disabled="deleteBusy" @click="setDeleteOpen(false)">{{ t('Cancel') }}</Button>
          <Button variant="destructive" :disabled="deleteBusy" @click="deleteTarget && remove(deleteTarget)">{{ deleteBusy ? t('Deleting…') : t('Delete image') }}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </main>
</template>
