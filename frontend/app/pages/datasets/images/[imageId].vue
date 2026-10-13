<script setup lang="ts">
import { ArrowLeft, Check, MousePointer2, Scan, Trash2, ZoomIn, ZoomOut } from '@lucide/vue'
import { Button } from '~/components/ui/button'
import { Badge } from '~/components/ui/badge'
import DatasetBoxEditor from '~/components/DatasetBoxEditor.vue'
import { datasetRequest } from '~/composables/datasetApi'
import type { DatasetDetail, VehicleBox } from '~/types/dataset'
import { DATASET_CLASSES, DATASET_STATUS_LABELS } from '~/types/dataset'

definePageMeta({ viewportWorkspace: true })
useHead({ title: 'ReTrace · Label vehicles' })
const route = useRoute()
const imageId = computed(() => String(route.params.imageId))
const image = ref<DatasetDetail | null>(null)
const boxes = ref<VehicleBox[]>([]), selectedId = ref<string | null>(null)
const tool = ref<'draw' | 'select'>('draw'), classId = ref(0), zoom = ref(1)
const loading = ref(true), saving = ref(false), ready = ref(false), reviewedEmpty = ref(false)
const error = ref<string | null>(null), message = ref<string | null>(null)
const baseline = ref('')
const selected = computed(() => boxes.value.find(box => box.id === selectedId.value))
const snapshot = () => JSON.stringify([boxes.value, reviewedEmpty.value])
const dirty = computed(() => !!image.value && snapshot() !== baseline.value)
const confirmLeave = () => !dirty.value || window.confirm('Leave without saving your label changes?')
let version = 0
async function load() {
  const current = ++version
  loading.value = true; ready.value = false; error.value = null; image.value = null
  try {
    const data = await datasetRequest<DatasetDetail>('/images/' + imageId.value)
    if (current !== version) return
    image.value = data; boxes.value = structuredClone(data.boxes); reviewedEmpty.value = data.reviewed_empty
    selectedId.value = null; zoom.value = 1; tool.value = 'draw'; baseline.value = snapshot()
  } catch (cause) { if (current === version) error.value = (cause as Error).message }
  finally { if (current === version) loading.value = false }
}
async function save(review: boolean) {
  if (!image.value || saving.value || !ready.value) return
  saving.value = true; error.value = null; message.value = null
  try {
    const result = await datasetRequest<DatasetDetail>('/images/' + image.value.id + '/annotations', { method: 'PUT', body: { revision: image.value.revision, boxes: boxes.value, status: review ? 'reviewed' : 'in_progress', reviewed_empty: reviewedEmpty.value } })
    image.value = { ...image.value, ...result }; baseline.value = snapshot()
    message.value = review ? 'Reviewed. This image is ready for dataset export.' : 'Draft saved.'
  } catch (cause) { error.value = (cause as Error).message }
  finally { saving.value = false }
}
function updateBoxes(next: VehicleBox[]) { boxes.value = next; if (next.length) reviewedEmpty.value = false }
function selectBox(id: string | null) { selectedId.value = id; if (id) tool.value = 'select' }
function removeSelected() { if (!selectedId.value || saving.value) return; boxes.value = boxes.value.filter(box => box.id !== selectedId.value); selectedId.value = null }
function changeClass(event: Event) { if (!selected.value || saving.value) return; const id = Number((event.target as HTMLSelectElement).value); boxes.value = boxes.value.map(box => box.id === selectedId.value ? { ...box, class_id: id } : box) }
function beforeUnload(event: BeforeUnloadEvent) { if (dirty.value) event.preventDefault() }
onBeforeRouteLeave(() => !saving.value && confirmLeave())
onBeforeRouteUpdate(() => !saving.value && confirmLeave())
watch(imageId, () => { void load() })
onMounted(() => { void load(); window.addEventListener('beforeunload', beforeUnload) })
onUnmounted(() => { version++; window.removeEventListener('beforeunload', beforeUnload) })
</script>

<template>
  <main class="flex min-h-0 flex-1 flex-col gap-3 px-3 py-3 sm:px-6">
    <header class="flex shrink-0 flex-wrap items-center gap-3"><Button as-child size="icon-sm" variant="ghost" aria-label="Back to datasets"><NuxtLink to="/datasets"><ArrowLeft /></NuxtLink></Button><div class="min-w-0 flex-1"><h1 class="text-base font-semibold">Label vehicles</h1><p v-if="image" class="truncate text-xs text-muted-foreground">{{ image.camera_id }} · {{ new Date(image.captured_at).toLocaleString() }} · {{ image.width }} × {{ image.height }}</p></div><Badge v-if="image" variant="outline" :class="dirty ? 'text-amber-200' : 'text-primary'">{{ dirty ? 'Unsaved changes' : DATASET_STATUS_LABELS[image.status] }}</Badge></header>
    <p v-if="error" class="shrink-0 rounded-lg bg-red-400/10 px-3 py-2 text-sm text-red-300" role="alert">{{ error }} <Button v-if="!image" size="sm" variant="ghost" @click="load">Retry</Button></p>
    <p v-if="message && !dirty" class="shrink-0 text-xs text-primary" role="status">{{ message }}</p>
    <p v-if="loading" class="py-16 text-center text-sm text-muted-foreground">Loading image…</p>
    <template v-else-if="image">
      <div class="flex shrink-0 flex-wrap items-center gap-2" role="toolbar" aria-label="Bounding box tools">
        <Button size="sm" :variant="tool === 'draw' ? 'secondary' : 'ghost'" :disabled="saving" @click="tool = 'draw'; selectedId = null"><Scan /> Draw box</Button><Button size="sm" :variant="tool === 'select' ? 'secondary' : 'ghost'" :disabled="saving" @click="tool = 'select'"><MousePointer2 /> Select / resize</Button>
        <label class="sr-only" for="new-box-class">Class for new boxes</label><select id="new-box-class" v-model.number="classId" class="h-8 rounded-lg border border-white/10 bg-background px-2 text-sm" :disabled="saving"><option v-for="(name, id) in DATASET_CLASSES" :key="id" :value="id">{{ name }}</option></select>
        <Button size="icon-sm" variant="ghost" aria-label="Zoom out" :disabled="zoom <= 1" @click="zoom = Math.max(1, zoom - .25)"><ZoomOut /></Button><span class="w-10 text-center text-xs tabular-nums">{{ Math.round(zoom * 100) }}%</span><Button size="icon-sm" variant="ghost" aria-label="Zoom in" :disabled="zoom >= 3" @click="zoom = Math.min(3, zoom + .25)"><ZoomIn /></Button>
        <div class="ml-auto flex flex-wrap gap-2"><Button size="sm" variant="outline" :disabled="saving || !ready" @click="save(false)">{{ saving ? 'Saving…' : 'Save draft' }}</Button><Button size="sm" :disabled="saving || !ready || (!boxes.length && !reviewedEmpty)" @click="save(true)"><Check /> Mark reviewed</Button></div>
      </div>
      <p class="shrink-0 text-xs text-muted-foreground">{{ tool === 'draw' ? 'Drag on the image to draw a vehicle box.' : 'Drag a box to move it. Drag its corners to resize. Delete removes the selected box.' }}</p>
      <div class="flex min-h-0 flex-1 flex-col gap-3 sm:flex-row">
        <DatasetBoxEditor :key="image.id" class="min-h-0 min-w-0 flex-1" :image-url="image.image_url" :width="image.width" :height="image.height" :boxes="boxes" :selected-id="selectedId" :class-id="classId" :tool="tool" :zoom="zoom" :disabled="saving" @update="updateBoxes" @select="selectBox" @ready="ready = $event" />
        <aside class="flex max-h-40 shrink-0 flex-col gap-3 overflow-y-auto rounded-xl border border-white/10 bg-white/[0.03] p-3 sm:max-h-none sm:w-60" aria-label="Vehicle labels">
          <div class="flex items-center justify-between"><h2 class="text-sm font-semibold">Boxes <span class="ml-1 text-muted-foreground">{{ boxes.length }}</span></h2><Button v-if="selected" size="icon-sm" variant="ghost" aria-label="Delete selected box" :disabled="saving" @click="removeSelected"><Trash2 /></Button></div>
          <label v-if="selected" class="space-y-1 text-xs text-muted-foreground">Selected vehicle class<select :value="selected.class_id" class="block h-8 w-full rounded-lg border border-white/10 bg-background px-2 text-sm text-foreground" :disabled="saving" @change="changeClass"><option v-for="(name, id) in DATASET_CLASSES" :key="id" :value="id">{{ name }}</option></select></label>
          <p v-if="!boxes.length" class="text-xs text-muted-foreground">No boxes yet. Draw each visible vehicle, or confirm that this image has no vehicles.</p>
          <label v-if="!boxes.length" class="flex items-start gap-2 text-xs leading-relaxed"><input v-model="reviewedEmpty" type="checkbox" class="mt-0.5 accent-primary" :disabled="saving">I reviewed this image. There are no vehicles.</label>
          <ul class="space-y-1"><li v-for="(box, index) in boxes" :key="box.id"><button type="button" class="flex w-full items-center justify-between rounded-lg border px-3 py-2 text-left text-sm" :class="box.id === selectedId ? 'border-primary/50 bg-primary/10' : 'border-transparent hover:bg-white/5'" :disabled="saving" :aria-pressed="box.id === selectedId" @click="selectBox(box.id)"><span>{{ index + 1 }} · {{ DATASET_CLASSES[box.class_id] }}</span><span class="text-[10px] text-muted-foreground">Manual</span></button></li></ul>
        </aside>
      </div>
      <nav class="flex shrink-0 items-center justify-between gap-3 text-xs text-muted-foreground" aria-label="Adjacent captured images"><Button v-if="image.previous_id" as-child size="sm" variant="ghost" :disabled="saving"><NuxtLink :to="`/datasets/images/${image.previous_id}`">← Previous image</NuxtLink></Button><span v-else /><Button v-if="image.next_id" as-child size="sm" variant="ghost" :disabled="saving"><NuxtLink :to="`/datasets/images/${image.next_id}`">Next image →</NuxtLink></Button></nav>
    </template>
  </main>
</template>
