<script setup lang="ts">
import { ArrowLeft, Check, MousePointer2, Trash2, ZoomIn, ZoomOut } from '@lucide/vue'
import { Button } from '~/components/ui/button'
import { Badge } from '~/components/ui/badge'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '~/components/ui/dialog'
import DatasetBoxEditor from '~/components/DatasetBoxEditor.vue'
import { datasetRequest } from '~/composables/datasetApi'
import { useI18n } from '~/composables/useI18n'
import type { DatasetDetail, VehicleBox } from '~/types/dataset'
import { DATASET_CLASSES, DATASET_STATUS_LABELS } from '~/types/dataset'

definePageMeta({ viewportWorkspace: true })
const { t, dateLocale } = useI18n()
useHead({ title: () => `ReTrace · ${t('Label vehicles')}` })
const route = useRoute()
const navigation = useDatasetNavigation()
const navigationError = ref<string | null>(null)
const neighbors = computed(() => navigation.neighbors(imageId.value))
async function loadNavigation() {
  navigationError.value = null
  try { await navigation.ensure(imageId.value) } catch { navigationError.value = 'Could not load the image list order.' }
}
const imageId = computed(() => String(route.params.imageId))
const image = ref<DatasetDetail | null>(null)
const boxes = ref<VehicleBox[]>([]), selectedId = ref<string | null>(null)
const tool = ref<'draw' | 'select'>('draw'), zoom = ref(1)
const loading = ref(true), saving = ref(false), ready = ref(false), reviewedEmpty = ref(false)
const error = ref<string | null>(null), message = ref<string | null>(null)
const baseline = ref('')
const deleteBoxId = ref<string | null>(null)
const selected = computed(() => boxes.value.find(box => box.id === selectedId.value))
const snapshot = () => JSON.stringify([boxes.value, reviewedEmpty.value])
const dirty = computed(() => !!image.value && snapshot() !== baseline.value)
const { confirm } = useConfirmDialog()
const confirmLeave = () => !dirty.value || confirm({ title: t('Discard label changes?'), description: t('Your unsaved label changes will be lost.'), confirmLabel: t('Discard changes'), destructive: true })
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
async function save() {
  if (!image.value || saving.value || !ready.value) return
  saving.value = true; error.value = null; message.value = null
  try {
    const result = await datasetRequest<DatasetDetail>('/images/' + image.value.id + '/annotations', { method: 'PUT', body: { revision: image.value.revision, boxes: boxes.value, status: 'reviewed', reviewed_empty: reviewedEmpty.value } })
    image.value = { ...image.value, ...result }; baseline.value = snapshot()
    message.value = 'Saved. This image is ready for dataset export.'
  } catch (cause) { error.value = (cause as Error).message }
  finally { saving.value = false }
}
function updateBoxes(next: VehicleBox[]) { boxes.value = next; if (next.length) reviewedEmpty.value = false }
function selectBox(id: string | null) { selectedId.value = id }
function removeBox() { if (!deleteBoxId.value || saving.value) return; boxes.value = boxes.value.filter(box => box.id !== deleteBoxId.value); if (selectedId.value === deleteBoxId.value) selectedId.value = null; deleteBoxId.value = null }

function beforeUnload(event: BeforeUnloadEvent) { if (dirty.value) event.preventDefault() }
onBeforeRouteLeave(() => !saving.value && confirmLeave())
onBeforeRouteUpdate(() => !saving.value && confirmLeave())
watch(imageId, () => { message.value = null; void load(); void loadNavigation() })
onMounted(() => { void load(); void loadNavigation(); window.addEventListener('beforeunload', beforeUnload) })
onUnmounted(() => { version++; window.removeEventListener('beforeunload', beforeUnload) })
</script>

<template>
  <main class="flex min-h-0 flex-1 flex-col gap-3 px-3 py-3 sm:px-6">
    <header class="flex shrink-0 flex-wrap items-center gap-3"><Button as-child size="icon-sm" variant="ghost" :aria-label="t('Back to datasets')"><NuxtLink to="/datasets"><ArrowLeft /></NuxtLink></Button><div class="min-w-0 flex-1"><h1 class="text-base font-semibold">{{ t('Label vehicles') }}</h1><p v-if="image" class="truncate text-xs text-muted-foreground">{{ image.camera_id }} · {{ new Date(image.captured_at).toLocaleString(dateLocale) }} · {{ image.width }} × {{ image.height }}</p></div></header>
    <p v-if="error" class="shrink-0 rounded-lg bg-red-400/10 px-3 py-2 text-sm text-red-300" role="alert">{{ t(error) }} <Button v-if="!image" size="sm" variant="ghost" @click="load">{{ t('Retry') }}</Button></p>
    <p class="h-5 shrink-0 truncate text-xs text-primary" role="status">{{ !dirty && message ? t(message) : '' }}</p>
    <p v-if="loading" class="py-16 text-center text-sm text-muted-foreground">{{ t('Loading image…') }}</p>
    <template v-else-if="image">
      <div class="shrink-0 sm:pr-[calc(15rem+0.75rem)]" role="toolbar" :aria-label="t('Bounding box tools')">
        <div class="grid grid-cols-[1fr_auto_1fr] items-center gap-1">
          <div class="editor-tools flex items-center justify-self-start">
            <Button :title="t('Select / resize')" size="icon" :variant="tool === 'select' ? 'secondary' : 'outline'" :disabled="saving" :aria-pressed="tool === 'select'" @click="tool = tool === 'draw' ? 'select' : 'draw'"><MousePointer2 /><span class="sr-only">{{ t('Select / resize') }}</span></Button>
          </div>
          <div class="editor-tools flex items-center justify-self-center">
            <Button size="icon" variant="ghost" :aria-label="t('Zoom out')" :title="t('Zoom out')" :disabled="zoom <= 1" @click="zoom = Math.max(1, zoom - .25)"><ZoomOut /></Button>
            <span class="w-10 text-center text-xs tabular-nums">{{ Math.round(zoom * 100) }}%</span>
            <Button size="icon" variant="ghost" :aria-label="t('Zoom in')" :title="t('Zoom in')" :disabled="zoom >= 3" @click="zoom = Math.min(3, zoom + .25)"><ZoomIn /></Button>
          </div>
          <div class="editor-tools flex items-center justify-self-end">
            <Button :title="t('Save labels')" size="icon" :disabled="saving || !ready || (!boxes.length && !reviewedEmpty)" @click="save()"><Check /><span class="sr-only">{{ saving ? t('Saving…') : t('Save labels') }}</span></Button>
          </div>
        </div>
      </div>
      <div class="flex min-h-0 flex-1 basis-0 flex-col gap-3 sm:flex-row">
        <div class="relative flex min-h-0 min-w-0 flex-1 flex-col">
          <DatasetBoxEditor :key="image.id" class="min-h-0 min-w-0 flex-1" :image-url="image.image_url" :width="image.width" :height="image.height" :boxes="boxes" :selected-id="selectedId" :class-id="0" :tool="tool" :zoom="zoom" :disabled="saving" @update="updateBoxes" @select="selectBox" @delete="deleteBoxId = $event" @ready="ready = $event" />
          <Badge variant="outline" role="status" class="pointer-events-none absolute top-3 left-3 z-20 h-auto min-h-7 border px-3 py-1 text-xs font-semibold shadow-lg backdrop-blur-md" :class="dirty ? 'border-amber-300/40 bg-black/80 text-amber-200' : 'border-primary/40 bg-black/80 text-primary'">{{ dirty ? t('Unsaved changes') : t(DATASET_STATUS_LABELS[image.status]) }}</Badge>
        </div>
        <aside class="flex max-h-40 shrink-0 flex-col gap-3 overflow-y-auto rounded-xl border border-white/10 bg-white/[0.03] p-3 sm:max-h-none sm:w-60" :aria-label="t('Vehicle labels')">
          <div class="flex items-center justify-between"><h2 class="text-sm font-semibold">{{ t('Boxes') }} <span class="ml-1 text-muted-foreground">{{ boxes.length }}</span></h2><Button size="icon-sm" variant="destructive" :aria-label="t('Delete selected box')" :title="t('Delete selected box')" :disabled="saving || !selected" @click="deleteBoxId = selectedId"><Trash2 /></Button></div>

          <p v-if="!boxes.length" class="text-xs text-muted-foreground">{{ t('No boxes yet. Draw each visible vehicle, or confirm that this image has no vehicles.') }}</p>
          <label v-if="!boxes.length" class="flex items-start gap-2 text-xs leading-relaxed"><input v-model="reviewedEmpty" type="checkbox" class="mt-0.5 accent-primary" :disabled="saving">{{ t('I reviewed this image. There are no vehicles.') }}</label>
          <ul class="space-y-1"><li v-for="(box, index) in boxes" :key="box.id" class="flex items-center gap-1 rounded-lg border" :class="box.id === selectedId ? 'border-primary/50 bg-primary/10' : 'border-transparent hover:bg-white/5'">
            <button type="button" class="min-w-0 flex-1 rounded-lg px-3 py-2 text-left text-sm outline-none focus-visible:ring-2 focus-visible:ring-primary/40" :disabled="saving" :aria-pressed="box.id === selectedId" @click="selectBox(box.id); tool = 'select'">{{ index + 1 }} · {{ DATASET_CLASSES[box.class_id] }}</button>
            <Button size="icon-sm" variant="ghost" :aria-label="t('Delete box {number}', { number: index + 1 })" :disabled="saving" @click="deleteBoxId = box.id"><Trash2 /></Button>
          </li></ul>
        </aside>
      </div>
      <p v-if="navigationError" class="shrink-0 text-xs text-red-300" role="alert">{{ t(navigationError) }} <Button size="sm" variant="ghost" @click="loadNavigation">{{ t('Retry') }}</Button></p>
      <nav class="flex shrink-0 items-center justify-between gap-3 text-xs text-muted-foreground" :aria-label="t('Adjacent captured images')"><Button v-if="neighbors.previous" as-child size="sm" variant="ghost" :disabled="saving"><NuxtLink :to="`/datasets/images/${neighbors.previous}`">← {{ t('Previous image') }}</NuxtLink></Button><span v-else /><Button v-if="neighbors.next" as-child size="sm" variant="ghost" :disabled="saving"><NuxtLink :to="`/datasets/images/${neighbors.next}`">{{ t('Next image') }} →</NuxtLink></Button></nav>
    </template>
    <Dialog :open="!!deleteBoxId" @update:open="!$event && (deleteBoxId = null)">
      <DialogContent class="border border-white/10 bg-popover/95 shadow-xl backdrop-blur-xl" @keydown.stop>
        <DialogHeader><DialogTitle>{{ t('Delete bounding box?') }}</DialogTitle><DialogDescription>{{ t('This removes the box from your draft. Save your changes to update the image labels.') }}</DialogDescription></DialogHeader>
        <DialogFooter><Button variant="outline" @click="deleteBoxId = null">{{ t('Cancel') }}</Button><Button variant="destructive" @click="removeBox">{{ t('Delete box') }}</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  </main>
</template>
