<script setup lang="ts">
import { Camera as CameraIcon, LoaderCircle, Plus, RefreshCw, Trash2 } from '@lucide/vue'
import DatasetSelect from '~/components/DatasetSelect.vue'
import { Button } from '~/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '~/components/ui/dialog'
import { Badge } from '~/components/ui/badge'
import CameraVideo from '~/components/CameraVideo.vue'
import { useCameras } from '~/composables/useCameras'
import { useRealtimePreview } from '~/composables/useRealtimePreview'
import { datasetRequest } from '~/composables/datasetApi'
import type { DatasetImage, DatasetPage, DatasetExport } from '~/types/dataset'
import { DATASET_STATUS_LABELS } from '~/types/dataset'

useHead({ title: 'ReTrace · AI datasets' })
const { cameras, loading: camerasLoading, error: cameraError } = useCameras()
const { client, connection, statuses } = useRealtimePreview(useRuntimeConfig().public.previewVideoFps)
const parkingCameras = computed(() => cameras.value.filter(item => item.name.includes('주차장')))
const cameraId = ref('')
const camera = computed(() => parkingCameras.value.find(c => c.camera_id === cameraId.value))
watch(parkingCameras, rows => { if (!rows.some(c => c.camera_id === cameraId.value && c.enabled)) cameraId.value = rows.find(c => c.enabled)?.camera_id ?? '' }, { immediate: true })
const navigation = useDatasetNavigation()
const { listCamera: filterCamera, listStatus: filterStatus, listPage: page } = navigation
const openingImage = ref(false)
let viewAlive = true
async function openImage(event: MouseEvent, id: string) {
  if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return
  event.preventDefault()
  if (openingImage.value) return
  openingImage.value = true
  try {
    await navigation.prepare(images.value, filterCamera.value, filterStatus.value, id)
    if (viewAlive) await navigateTo(`/datasets/images/${id}`)
  } catch (cause) { error.value = (cause as Error).message }
  finally { openingImage.value = false }
}
const images = ref<DatasetPage>({ items: [], total: 0, page: 1, page_size: 24 })
const exports = ref<DatasetExport[]>([])
const loading = ref(false), capturing = ref(false), exporting = ref(false), deleting = ref<string | null>(null)
const error = ref<string | null>(null), message = ref<string | null>(null)
const exportDeleting = ref<string | null>(null)
const { confirm } = useConfirmDialog()
async function removeExport(job: DatasetExport) {
  if (exportDeleting.value || job.status === 'building') return
  if (!await confirm({ title: 'Delete dataset export?', description: 'Delete this export record and its ZIP file? Captured images and labels will be kept.', confirmLabel: 'Delete export', destructive: true })) return
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
let requestVersion = 0
let timer: ReturnType<typeof setInterval> | undefined
const cameraOptions = computed(() => parkingCameras.value.map(item => ({ value: item.camera_id, label: item.name, disabled: !item.enabled })))
const cameraFilterOptions = computed(() => [{ value: 'all', label: 'All cameras' }, ...parkingCameras.value.map(item => ({ value: item.camera_id, label: item.name }))])
const statusOptions = [{ value: 'all', label: 'All states' }, ...Object.entries(DATASET_STATUS_LABELS).map(([value, label]) => ({ value, label }))]
async function refresh() {
  const version = ++requestVersion
  loading.value = true; error.value = null
  try {
    const query = new URLSearchParams({ page: String(page.value), page_size: '24' })
    if (filterCamera.value) query.set('camera_id', filterCamera.value)
    if (filterStatus.value) query.set('status', filterStatus.value)
    const data = await datasetRequest<DatasetPage>('/images?' + query)
    if (version === requestVersion) images.value = data
  } catch (cause) { if (version === requestVersion) error.value = (cause as Error).message }
  finally { if (version === requestVersion) loading.value = false }
}
async function refreshExports() {
  try { exports.value = await datasetRequest<DatasetExport[]>('/exports') } catch (cause) { error.value = (cause as Error).message }
}
watch([filterCamera, filterStatus], () => { page.value = 1; void refresh() })
watch(page, () => void refresh())
async function capture() {
  if (!camera.value || capturing.value) return
  capturing.value = true; error.value = null; message.value = null
  try {
    captureResult.value = await datasetRequest<DatasetImage>('/capture', { method: 'POST', body: { camera_id: cameraId.value } })
    message.value = 'Clean frame captured. Open it to label vehicles.'
    page.value = 1; await refresh()
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
const cameraName = (id: string) => cameras.value.find(c => c.camera_id === id)?.name ?? id
const time = (date: string) => new Date(date).toLocaleString()
onMounted(() => { void refresh(); void refreshExports(); timer = setInterval(() => { if (exports.value.some(job => job.status === 'building')) void refreshExports() }, 2500) })
onUnmounted(() => { viewAlive = false; requestVersion++; if (timer) clearInterval(timer) })
</script>

<template>
  <main class="relative mx-auto max-w-[1600px] space-y-6 px-3 py-6 sm:px-6">
    <header class="space-y-2"><p class="text-xs font-semibold uppercase tracking-[0.2em] text-primary">AI dataset workspace</p><h1 class="text-2xl font-semibold tracking-tight">Capture. Label. Review.</h1><p class="text-sm text-muted-foreground">Build a vehicle detection dataset from clean camera frames.</p></header>
    <div v-if="error || cameraError" class="flex flex-wrap items-center gap-3 rounded-xl border border-red-400/20 bg-red-400/5 p-4 text-sm text-red-300" role="alert"><span class="min-w-0 flex-1">{{ error || cameraError }}</span><Button size="sm" variant="ghost" @click="refresh(); refreshExports()"><RefreshCw /> Retry</Button></div>

    <div class="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(20rem,0.65fr)]">
      <section class="overflow-hidden rounded-2xl border border-white/10 bg-white/[0.03]">
        <div class="flex flex-wrap items-center justify-between gap-3 p-4">
          <h2 class="flex items-center gap-2 text-sm font-semibold"><CameraIcon class="size-4 text-primary" /> Collect images</h2>
          <DatasetSelect v-model="cameraId" :options="cameraOptions" label="Camera to capture" :placeholder="camerasLoading ? 'Loading cameras…' : 'No enabled cameras'" :disabled="camerasLoading || capturing || !parkingCameras.some(item => item.enabled)" />
        </div>
        <div class="bg-black/60"><div class="relative mx-auto aspect-video w-full max-w-[calc(28rem*16/9)]"><CameraVideo v-if="camera" :key="camera.camera_id" :camera="camera" :realtime-client="client" :connection="connection" :camera-state="statuses[camera.camera_id]?.state ?? camera.status.state" :active="true" /><p v-else class="absolute inset-0 grid place-items-center text-sm text-muted-foreground">Select a camera to preview</p></div></div>
        <div class="flex flex-wrap items-center justify-between gap-3 p-4"><div class="flex h-10 min-w-0 flex-1 basis-full flex-col sm:basis-0 justify-center text-xs leading-relaxed" role="status"><p v-if="!message" class="text-muted-foreground">The live preview includes detection overlays. Captures are saved separately without overlays.</p><template v-else><p class="truncate text-primary">{{ message }}</p><NuxtLink v-if="captureResult" :to="`/datasets/images/${captureResult.id}`" class="w-fit text-primary underline underline-offset-4" @click="openImage($event, captureResult.id)">Label image</NuxtLink></template></div><Button :disabled="!camera || capturing || camerasLoading" @click="capture"><LoaderCircle v-if="capturing" class="animate-spin" /><CameraIcon v-else /> {{ capturing ? 'Capturing…' : 'Capture current frame' }}</Button></div>
      </section>
      <section class="relative min-w-0 rounded-2xl border border-white/10 bg-white/[0.03] p-5">
        <div class="flex min-h-0 flex-col gap-4 lg:absolute lg:inset-5">
        <div><h2 class="text-sm font-semibold">YOLO dataset exports</h2><p class="mt-2 text-xs leading-relaxed text-muted-foreground">Only reviewed images are included. Capture sessions stay together across train / val / test.</p></div>
        <div class="flex items-center justify-between gap-3"><span class="text-xs text-muted-foreground">Car detection · 0 car</span><Button size="sm" :disabled="exporting || exports.some(job => job.status === 'building')" @click="exportDataset"><LoaderCircle v-if="exporting" class="animate-spin" /><Plus v-else /> Create dataset</Button></div>
        <div class="min-h-0 max-h-72 space-y-3 overflow-y-auto lg:max-h-none lg:flex-1">
          <p v-if="!exports.length" class="py-6 text-sm text-muted-foreground">Review your labels, then create a dataset ZIP.</p>
          <article v-for="job in exports" :key="job.id" class="space-y-2 rounded-xl border border-white/10 bg-black/10 p-3">
            <div class="flex items-center justify-between gap-2"><time class="text-xs text-muted-foreground">{{ time(job.created_at) }}</time><Badge variant="outline" class="px-2 py-0.5 capitalize" :class="job.status === 'ready' ? 'border-primary/25 bg-primary/10 text-primary' : job.status === 'failed' ? 'border-red-400/25 bg-red-400/10 text-red-300' : 'border-amber-300/25 bg-amber-300/10 text-amber-200'">{{ job.status === 'ready' ? 'Ready' : job.status === 'building' ? 'Building' : 'Failed' }}</Badge></div>
            <p v-if="job.manifest?.classes && Object.keys(job.manifest.classes).length > 1" class="text-xs text-muted-foreground">Legacy export · {{ Object.values(job.manifest.classes).join(', ') }}</p>
            <p v-if="job.manifest" class="text-xs">Train {{ job.manifest.counts.train }} · Val {{ job.manifest.counts.val }} · Test {{ job.manifest.counts.test }}</p>
            <p v-if="job.status === 'building'" class="text-xs text-muted-foreground" role="status">Building archive…</p><p v-if="job.error" class="text-xs text-red-300">{{ job.error }}</p>
            <p v-for="warning in job.manifest?.warnings ?? []" :key="warning" class="text-xs text-amber-200/80">{{ warning }}</p>
            <div class="flex items-center justify-between gap-2"><Button v-if="job.download_url" as-child size="sm" variant="outline" class="h-8 px-3"><a :href="job.download_url" download>Download ZIP</a></Button><span v-else /><Button size="icon-sm" variant="destructive" :aria-label="`Delete export from ${time(job.created_at)}`" :disabled="job.status === 'building' || !!exportDeleting" @click="removeExport(job)"><LoaderCircle v-if="exportDeleting === job.id" class="animate-spin" /><Trash2 v-else /></Button></div>
          </article>
        </div>
        </div>
      </section>
    </div>
    <section class="space-y-4 [overflow-anchor:none]" aria-label="Captured images" :aria-busy="loading || openingImage">
      <div class="flex flex-wrap items-center gap-3"><h2 class="mr-auto text-base font-semibold">Captured images <span class="ml-2 text-sm font-normal text-muted-foreground">{{ images.total }}</span></h2><DatasetSelect :model-value="filterCamera || 'all'" :options="cameraFilterOptions" label="Filter images by camera" @update:model-value="filterCamera = $event === 'all' ? '' : $event" /><DatasetSelect :model-value="filterStatus || 'all'" :options="statusOptions" label="Filter images by status" @update:model-value="filterStatus = $event === 'all' ? '' : $event" /><Button size="icon-sm" variant="ghost" aria-label="Refresh images" :disabled="loading" @click="refresh"><RefreshCw :class="loading ? 'animate-spin' : ''" /></Button></div>
      <p v-if="loading && !images.items.length" class="py-8 text-center text-sm text-muted-foreground" role="status">Loading images…</p>
      <p v-else-if="!loading && !images.items.length" class="rounded-2xl border border-dashed border-white/15 py-16 text-center text-sm text-muted-foreground">No captured images here yet.</p>
      <div v-else class="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <article v-for="image in images.items" :key="image.id" class="overflow-hidden rounded-xl border border-white/10 bg-white/[0.03] transition-colors hover:border-primary/40">
          <NuxtLink :to="`/datasets/images/${image.id}`" @click="openImage($event, image.id)" class="block"><img :src="image.thumbnail_url" :alt="`Captured frame from ${cameraName(image.camera_id)}`" loading="lazy" class="aspect-video w-full bg-black/30 object-cover"></NuxtLink>
          <div class="space-y-3 p-3"><div class="flex min-w-0 items-center justify-between gap-2"><p class="truncate text-sm font-medium">{{ cameraName(image.camera_id) }}</p><Badge variant="outline" :class="image.status === 'reviewed' ? 'text-primary' : 'text-muted-foreground'">{{ DATASET_STATUS_LABELS[image.status] }}</Badge></div><time class="block text-xs text-muted-foreground">{{ time(image.captured_at) }}</time><div class="flex items-center justify-between gap-2"><Button as-child size="sm" variant="outline"><NuxtLink :to="`/datasets/images/${image.id}`" @click="openImage($event, image.id)">{{ image.status === 'pending' ? 'Start labeling' : 'Open image' }}</NuxtLink></Button><Button size="icon-sm" variant="ghost" :disabled="!!deleting" :aria-label="`Delete image captured at ${time(image.captured_at)}`" @click="requestDelete(image)"><Trash2 class="text-muted-foreground" /></Button></div></div>
        </article>
      </div>
      <nav class="flex items-center justify-center gap-4" aria-label="Image pagination"><Button size="sm" variant="outline" :disabled="page <= 1 || loading" @click="page--">Previous</Button><span class="text-xs tabular-nums text-muted-foreground">{{ page }} / {{ Math.max(1, Math.ceil(images.total / 24)) }}</span><Button size="sm" variant="outline" :disabled="page * 24 >= images.total || loading" @click="page++">Next</Button></nav>
    </section>
    <Dialog :open="deleteDialogOpen" @update:open="setDeleteOpen">
      <DialogContent :show-close-button="!deleteBusy" class="border border-white/10 bg-popover/95 shadow-xl backdrop-blur-xl" @escape-key-down="deleting && $event.preventDefault()" @pointer-down-outside="deleting && $event.preventDefault()">
        <DialogHeader>
          <DialogTitle>Delete captured image?</DialogTitle>
          <DialogDescription>The original image, thumbnail and all its labels will be permanently deleted. Existing dataset ZIPs keep their exported copy.</DialogDescription>
        </DialogHeader>
        <p v-if="deleteTarget" class="text-xs text-muted-foreground">{{ cameraName(deleteTarget.camera_id) }} · {{ time(deleteTarget.captured_at) }}</p>
        <p v-if="deleteError" role="alert" class="text-sm text-red-300">{{ deleteError }}</p>
        <DialogFooter>
          <Button variant="outline" :disabled="deleteBusy" @click="setDeleteOpen(false)">Cancel</Button>
          <Button variant="destructive" :disabled="deleteBusy" @click="deleteTarget && remove(deleteTarget)">{{ deleteBusy ? 'Deleting…' : 'Delete image' }}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </main>
</template>
