<script setup lang="ts">
import { LoaderCircle, RefreshCw } from '@lucide/vue'
import DatasetThumbnail from '~/components/DatasetThumbnail.vue'
import { Badge } from '~/components/ui/badge'
import { Button } from '~/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '~/components/ui/dialog'
import { datasetRequest } from '~/composables/datasetApi'
import { useI18n } from '~/composables/useI18n'
import type { DatasetImage, DatasetPage } from '~/types/dataset'

/**
 * Picks one of this camera's AI-dataset images as the zone editor's backdrop, so zones can be
 * drawn on a frame of the empty lot instead of the live view. Images without labelled vehicles
 * come first; the rest stay selectable.
 */
const open = defineModel<boolean>('open', { default: false })
const props = defineProps<{ cameraId: string; cameraName: string; selectedId: string | null }>()
const emit = defineEmits<{ select: [image: DatasetImage] }>()
const { t } = useI18n()

const PAGE_SIZE = 48
const images = ref<DatasetImage[]>([])
const total = ref(0)
const loading = ref(false)
const error = ref<string | null>(null)
const emptyOnly = ref(true)
let request = 0

// Reviewed images with no boxes are the frames someone confirmed as an empty lot.
const isEmptyLot = (image: DatasetImage) => image.box_count === 0 && image.status === 'reviewed'
const rows = computed(() => {
  const list = emptyOnly.value ? images.value.filter(image => image.box_count === 0) : images.value
  return [...list].sort((a, b) => Number(isEmptyLot(b)) - Number(isEmptyLot(a)) || a.box_count - b.box_count || b.captured_at.localeCompare(a.captured_at))
})

async function load() {
  const version = ++request
  loading.value = true
  error.value = null
  try {
    const query = new URLSearchParams({ camera_id: props.cameraId, page: '1', page_size: String(PAGE_SIZE) })
    const page = await datasetRequest<DatasetPage>('/images?' + query)
    if (version !== request) return
    images.value = page.items.filter(image => image.camera_id === props.cameraId)
    total.value = page.total
  } catch (cause) {
    if (version === request) error.value = (cause as Error).message
  } finally {
    if (version === request) loading.value = false
  }
}

watch(open, value => { if (value) void load() })
watch(() => props.cameraId, () => { images.value = []; total.value = 0 })

function choose(image: DatasetImage) {
  emit('select', image)
  open.value = false
}

const time = (value: string) => new Date(value).toLocaleString()
</script>

<template>
  <Dialog v-model:open="open">
    <DialogContent class="max-h-[85dvh] overflow-y-auto sm:max-w-4xl">
      <DialogTitle>{{ t('Draw on a dataset image') }}</DialogTitle>
      <DialogDescription>{{ t('Pick a frame of {camera} from AI datasets. Zones are drawn on it instead of the live view; status still comes from the live camera.', { camera: cameraName }) }}</DialogDescription>
      <div class="flex flex-wrap items-center gap-3">
        <label class="flex items-center gap-2 text-sm"><input v-model="emptyOnly" type="checkbox" class="accent-primary"> {{ t('Only images without labelled vehicles') }}</label>
        <Button size="icon-sm" variant="ghost" class="ml-auto" :aria-label="t('Refresh images')" :disabled="loading" @click="load"><RefreshCw :class="loading ? 'animate-spin' : ''" /></Button>
      </div>
      <p v-if="error" role="alert" class="text-sm text-red-300">{{ t(error) }}</p>
      <div v-if="loading && !images.length" class="grid place-items-center py-10 text-muted-foreground"><LoaderCircle class="animate-spin" /></div>
      <ul v-else-if="rows.length" class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <li v-for="image in rows" :key="image.id">
          <button
            type="button"
            :class="['w-full cursor-pointer overflow-hidden rounded-xl border text-left transition-colors', image.id === selectedId ? 'border-primary' : 'border-white/10 hover:border-white/30']"
            :aria-pressed="image.id === selectedId"
            @click="choose(image)"
          >
            <DatasetThumbnail :image="image" :alt="t('Captured frame from {camera}', { camera: cameraName })" />
            <span class="flex items-center justify-between gap-2 p-2.5 text-xs">
              <time class="text-muted-foreground">{{ time(image.captured_at) }}</time>
              <Badge v-if="isEmptyLot(image)" variant="outline" class="text-primary">{{ t('Empty lot') }}</Badge>
              <Badge v-else variant="outline" class="text-muted-foreground">{{ t('{count} vehicles', { count: image.box_count }) }}</Badge>
            </span>
          </button>
        </li>
      </ul>
      <p v-else-if="!loading && !error" class="py-6 text-sm text-muted-foreground">
        {{ images.length ? t('No images without labelled vehicles. Turn off the filter to see all images.') : t('No dataset images for this camera yet. Capture one in AI datasets.') }}
      </p>
      <p v-if="total > PAGE_SIZE" class="text-xs text-muted-foreground">{{ t('Showing the latest {count} images.', { count: PAGE_SIZE }) }}</p>
    </DialogContent>
  </Dialog>
</template>
