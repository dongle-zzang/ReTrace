<script setup lang="ts">
import { datasetRequest } from '~/composables/datasetApi'
import type { DatasetDetail, DatasetImage, VehicleBox } from '~/types/dataset'

const props = defineProps<{ image: DatasetImage; alt: string }>()

// The list API only carries box_count, so boxes come from the image detail. Cache by revision
// so paging back and forth does not refetch unchanged labels.
const cache = useState<Record<string, VehicleBox[]>>('dataset-thumbnail-boxes', () => ({}))
const cacheKey = computed(() => `${props.image.id}:${props.image.revision}`)
const boxes = computed(() => cache.value[cacheKey.value] ?? [])
const colors = ['var(--primary)', '#fbbf24', '#7dd3fc']

watch(cacheKey, async (key) => {
  if (!props.image.box_count || cache.value[key]) return
  try {
    const detail = await datasetRequest<DatasetDetail>('/images/' + props.image.id)
    cache.value = { ...cache.value, [`${detail.id}:${detail.revision}`]: detail.boxes }
  } catch {
    // The thumbnail still works without its boxes; the editor reports load errors.
  }
}, { immediate: true })
</script>

<template>
  <div class="relative aspect-video w-full overflow-hidden bg-black/30">
    <img :src="image.thumbnail_url" :alt="alt" loading="lazy" class="absolute inset-0 h-full w-full object-cover">
    <svg v-if="boxes.length" class="pointer-events-none absolute inset-0 h-full w-full" :viewBox="`0 0 ${image.width} ${image.height}`" preserveAspectRatio="xMidYMid slice" aria-hidden="true">
      <rect v-for="box in boxes" :key="box.id" :x="box.x" :y="box.y" :width="box.width" :height="box.height" :style="{ color: colors[box.class_id] }" fill="none" stroke="currentColor" stroke-opacity="0.3" stroke-width="1.5" vector-effect="non-scaling-stroke" />
    </svg>
  </div>
</template>
