<script setup lang="ts">
import { Camera as CameraIcon, ChevronDown, Download, LoaderCircle, Plus, RefreshCw, Trash2 } from '@lucide/vue'
import { SelectContent, SelectItem, SelectItemText, SelectPortal, SelectRoot, SelectTrigger, SelectViewport } from 'reka-ui'
import { Button } from '~/components/ui/button'
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
const cameraId = ref('')
const camera = computed(() => cameras.value.find(c => c.camera_id === cameraId.value))
watch(cameras, rows => { if (!cameraId.value) cameraId.value = rows.find(c => c.enabled)?.camera_id ?? '' })
const filterCamera = ref(''), filterStatus = ref(''), page = ref(1)
const images = ref<DatasetPage>({ items: [], total: 0, page: 1, page_size: 24 })
const exports = ref<DatasetExport[]>([])
const loading = ref(false), capturing = ref(false), exporting = ref(false), deleting = ref<string | null>(null)
const error = ref<string | null>(null), message = ref<string | null>(null)
const captureResult = ref<DatasetImage | null>(null)
let requestVersion = 0
let timer: ReturnType<typeof setInterval> | undefined
const selectClass = 'h-9 rounded-lg border border-white/10 bg-background/80 px-3 text-sm outline-none focus:ring-2 focus:ring-primary/40'
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
  if (deleting.value || !window.confirm('Delete this captured image and all its labels?')) return
  deleting.value = image.id
  try { await datasetRequest('/images/' + image.id, { method: 'DELETE' }); if (captureResult.value?.id === image.id) captureResult.value = null; await refresh() }
  catch (cause) { error.value = (cause as Error).message }
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
onUnmounted(() => { requestVersion++; if (timer) clearInterval(timer) })
</script>

<template>
  <main class="relative mx-auto max-w-[1600px] space-y-6 px-3 py-6 sm:px-6">
    <header class="space-y-2"><p class="text-xs font-semibold uppercase tracking-[0.2em] text-primary">AI dataset workspace</p><h1 class="text-2xl font-semibold tracking-tight">Capture. Label. Review.</h1><p class="text-sm text-muted-foreground">Build a vehicle detection dataset from clean camera frames.</p></header>
    <div v-if="error || cameraError" class="flex flex-wrap items-center gap-3 rounded-xl border border-red-400/20 bg-red-400/5 p-4 text-sm text-red-300" role="alert"><span class="min-w-0 flex-1">{{ error || cameraError }}</span><Button size="sm" variant="ghost" @click="refresh(); refreshExports()"><RefreshCw /> Retry</Button></div>
    <p v-if="message" class="flex flex-wrap items-center gap-3 text-sm text-primary" role="status">{{ message }} <NuxtLink v-if="captureResult" :to="`/datasets/images/${captureResult.id}`" class="underline underline-offset-4">Label captured image</NuxtLink></p>
    <div class="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(20rem,0.65fr)]">
      <section class="overflow-hidden rounded-2xl border border-white/10 bg-white/[0.03]">
        <div class="flex flex-wrap items-center justify-between gap-3 p-4">
          <h2 class="flex items-center gap-2 text-sm font-semibold"><CameraIcon class="size-4 text-primary" /> Collect images</h2>
          <SelectRoot v-model="cameraId" :disabled="camerasLoading || capturing || !cameras.some(item => item.enabled)">
            <SelectTrigger
              aria-label="Camera to capture"
              class="group flex h-9 w-52 max-w-full min-w-0 items-center justify-between gap-2 rounded-lg border border-white/10 bg-white/[0.04] px-3 text-left text-sm shadow-sm outline-none transition-colors hover:border-white/20 hover:bg-white/[0.07] focus-visible:border-primary/50 focus-visible:ring-2 focus-visible:ring-primary/30 data-[state=open]:border-primary/40 data-[state=open]:bg-white/[0.07] disabled:cursor-not-allowed disabled:opacity-50"
            >
              <span class="truncate font-medium text-foreground">{{ camera?.name ?? (camerasLoading ? 'Loading cameras…' : cameras.some(item => item.enabled) ? 'Select a camera' : 'No enabled cameras') }}</span>
              <ChevronDown aria-hidden="true" class="size-4 shrink-0 text-muted-foreground transition-transform duration-150 group-data-[state=open]:rotate-180" />
            </SelectTrigger>
            <SelectPortal>
              <SelectContent
                :body-lock="false"
                position="popper"
                align="end"
                :side-offset="8"
                class="z-[60] max-h-[min(24rem,calc(100vh-2rem))] w-[var(--reka-select-trigger-width)] max-w-[calc(100vw-1.5rem)] min-w-[var(--reka-select-trigger-width)] overflow-hidden rounded-xl border border-white/10 bg-popover/95 p-1.5 text-popover-foreground shadow-xl shadow-black/30 ring-1 ring-white/10 backdrop-blur-xl outline-none data-open:animate-in data-closed:animate-out data-closed:fade-out-0 data-open:fade-in-0 data-closed:zoom-out-95 data-open:zoom-in-95 data-[side=bottom]:slide-in-from-top-1 data-[side=top]:slide-in-from-bottom-1 duration-150"
              >
                <SelectViewport class="max-h-[min(20rem,calc(100vh-5rem))] overflow-y-auto">
                  <SelectItem
                    v-for="item in cameras"
                    :key="item.camera_id"
                    :value="item.camera_id"
                    :text-value="item.name"
                    :disabled="!item.enabled"
                    :aria-label="item.name"
                    class="group flex min-h-10 cursor-default select-none items-center gap-3 rounded-lg px-2.5 py-2 text-sm outline-none transition-colors data-[highlighted]:bg-white/[0.07] data-[state=checked]:bg-primary/[0.08] data-[state=checked]:text-foreground data-[disabled]:pointer-events-none data-[disabled]:opacity-40"
                  >
                    <SelectItemText class="truncate font-medium">{{ item.name }}</SelectItemText>
                  </SelectItem>
                  <p v-if="!cameras.length && !camerasLoading" class="px-3 py-6 text-center text-xs text-muted-foreground">No cameras are available.</p>
                </SelectViewport>
              </SelectContent>
            </SelectPortal>
          </SelectRoot>
        </div>
        <div class="relative aspect-video max-h-[28rem] bg-black/60"><CameraVideo v-if="camera" :key="camera.camera_id" :camera="camera" :realtime-client="client" :connection="connection" :camera-state="statuses[camera.camera_id]?.state ?? camera.status.state" :active="true" /><p v-else class="absolute inset-0 grid place-items-center text-sm text-muted-foreground">Select a camera to preview</p></div>
        <div class="flex flex-wrap items-center justify-between gap-3 p-4"><p class="max-w-md text-xs leading-relaxed text-muted-foreground">The live preview includes detection overlays. Captures are saved separately without overlays.</p><Button :disabled="!camera || capturing || camerasLoading" @click="capture"><LoaderCircle v-if="capturing" class="animate-spin" /><CameraIcon v-else /> {{ capturing ? 'Capturing…' : 'Capture current frame' }}</Button></div>
      </section>
      <section class="flex min-w-0 flex-col gap-4 rounded-2xl border border-white/10 bg-white/[0.03] p-5">
        <div><h2 class="text-sm font-semibold">YOLO dataset exports</h2><p class="mt-2 text-xs leading-relaxed text-muted-foreground">Only reviewed images are included. Capture sessions stay together across train / val / test.</p></div>
        <div class="flex items-center justify-between gap-3"><span class="text-xs text-muted-foreground">0 car · 1 truck · 2 bus</span><Button size="sm" :disabled="exporting || exports.some(job => job.status === 'building')" @click="exportDataset"><LoaderCircle v-if="exporting" class="animate-spin" /><Plus v-else /> Create dataset</Button></div>
        <div class="max-h-72 space-y-3 overflow-y-auto">
          <p v-if="!exports.length" class="py-6 text-sm text-muted-foreground">Review your labels, then create a dataset ZIP.</p>
          <article v-for="job in exports" :key="job.id" class="space-y-2 rounded-xl border border-white/10 bg-black/10 p-3">
            <div class="flex items-center justify-between gap-2"><time class="text-xs text-muted-foreground">{{ time(job.created_at) }}</time><Badge variant="outline">{{ job.status }}</Badge></div>
            <p v-if="job.manifest" class="text-xs">Train {{ job.manifest.counts.train }} · Val {{ job.manifest.counts.val }} · Test {{ job.manifest.counts.test }}</p>
            <p v-if="job.status === 'building'" class="text-xs text-muted-foreground" role="status">Building archive…</p><p v-if="job.error" class="text-xs text-red-300">{{ job.error }}</p>
            <p v-for="warning in job.manifest?.warnings ?? []" :key="warning" class="text-xs text-amber-200/80">{{ warning }}</p>
            <Button v-if="job.download_url" as-child size="sm" variant="outline"><a :href="job.download_url" download><Download /> Download ZIP</a></Button>
          </article>
        </div>
      </section>
    </div>
    <section class="space-y-4" aria-label="Captured images">
      <div class="flex flex-wrap items-center gap-3"><h2 class="mr-auto text-base font-semibold">Captured images <span class="ml-2 text-sm font-normal text-muted-foreground">{{ images.total }}</span></h2><select v-model="filterCamera" aria-label="Filter images by camera" :class="[selectClass, 'max-w-full']"><option value="">All cameras</option><option v-for="c in cameras" :key="c.camera_id" :value="c.camera_id">{{ c.name }}</option></select><select v-model="filterStatus" aria-label="Filter images by status" :class="selectClass"><option value="">All states</option><option v-for="(label, status) in DATASET_STATUS_LABELS" :key="status" :value="status">{{ label }}</option></select><Button size="icon-sm" variant="ghost" aria-label="Refresh images" :disabled="loading" @click="refresh"><RefreshCw :class="loading ? 'animate-spin' : ''" /></Button></div>
      <p v-if="loading" class="py-8 text-center text-sm text-muted-foreground" role="status">Loading images…</p>
      <p v-else-if="!images.items.length" class="rounded-2xl border border-dashed border-white/15 py-16 text-center text-sm text-muted-foreground">No captured images here yet.</p>
      <div v-else class="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <article v-for="image in images.items" :key="image.id" class="overflow-hidden rounded-xl border border-white/10 bg-white/[0.03] transition-colors hover:border-primary/40">
          <NuxtLink :to="`/datasets/images/${image.id}`" class="block"><img :src="image.thumbnail_url" :alt="`Captured frame from ${cameraName(image.camera_id)}`" loading="lazy" class="aspect-video w-full bg-black/30 object-cover"></NuxtLink>
          <div class="space-y-3 p-3"><div class="flex min-w-0 items-center justify-between gap-2"><p class="truncate text-sm font-medium">{{ cameraName(image.camera_id) }}</p><Badge variant="outline" :class="image.status === 'reviewed' ? 'text-primary' : 'text-muted-foreground'">{{ DATASET_STATUS_LABELS[image.status] }}</Badge></div><time class="block text-xs text-muted-foreground">{{ time(image.captured_at) }}</time><div class="flex items-center justify-between gap-2"><Button as-child size="sm" variant="outline"><NuxtLink :to="`/datasets/images/${image.id}`">{{ image.status === 'pending' ? 'Start labeling' : 'Open image' }}</NuxtLink></Button><Button size="icon-sm" variant="ghost" :disabled="!!deleting" :aria-label="`Delete image captured at ${time(image.captured_at)}`" @click="remove(image)"><Trash2 class="text-muted-foreground" /></Button></div></div>
        </article>
      </div>
      <nav class="flex items-center justify-center gap-4" aria-label="Image pagination"><Button size="sm" variant="outline" :disabled="page <= 1 || loading" @click="page--">Previous</Button><span class="text-xs tabular-nums text-muted-foreground">{{ page }} / {{ Math.max(1, Math.ceil(images.total / 24)) }}</span><Button size="sm" variant="outline" :disabled="page * 24 >= images.total || loading" @click="page++">Next</Button></nav>
    </section>
  </main>
</template>
