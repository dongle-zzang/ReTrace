<script setup lang="ts">
import { ChevronLeft, ChevronRight, LoaderCircle } from '@lucide/vue'
import { Button } from '~/components/ui/button'
import { trainingRequest } from '~/composables/trainingApi'
import { boxStyle } from '~/lib/training'
import type { ComparisonPage } from '~/types/training'

const props = defineProps<{ jobId: string }>()
const PAGE = 4
const page = ref<ComparisonPage | null>(null)
const offset = ref(0)
const onlyErrors = ref(true)
const loading = ref(false)
const error = ref<string | null>(null)
let version = 0
const MODEL_LABELS: Record<string, string> = { baseline: 'Current YOLO11m', previous: 'Base of this run', new: 'New model' }
const order = (models: Record<string, unknown>) => ['baseline', 'previous', 'new'].filter(key => key in models)

async function load() {
  const current = ++version
  loading.value = true; error.value = null
  try {
    const data = await trainingRequest<ComparisonPage>(`/jobs/${props.jobId}/comparison?offset=${offset.value}&limit=${PAGE}&only_errors=${onlyErrors.value}`)
    if (current === version) page.value = data
  } catch (cause) { if (current === version) error.value = (cause as Error).message }
  finally { if (current === version) loading.value = false }
}
watch(() => props.jobId, () => { offset.value = 0; void load() })
watch(onlyErrors, () => { offset.value = 0; void load() })
watch(offset, () => void load())
onMounted(load)
onUnmounted(() => { version++ })
</script>

<template>
  <section class="space-y-3" aria-label="Detection comparison">
    <div class="flex flex-wrap items-center gap-3">
      <h3 class="mr-auto text-sm font-semibold">Same test images, both models</h3>
      <label class="flex items-center gap-2 text-xs text-muted-foreground"><input v-model="onlyErrors" type="checkbox" class="accent-primary"> Only images with missed or false detections</label>
    </div>
    <p class="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
      <span><span class="mr-1 inline-block size-2.5 rounded-sm border border-emerald-400" />Ground truth</span>
      <span><span class="mr-1 inline-block size-2.5 rounded-sm bg-sky-400" />Correct (TP)</span>
      <span><span class="mr-1 inline-block size-2.5 rounded-sm bg-amber-400" />False detection (FP)</span>
      <span><span class="mr-1 inline-block size-2.5 rounded-sm bg-red-500" />Missed vehicle (FN)</span>
      <span v-if="page">conf ≥ {{ page.conf }}, NMS IoU {{ page.nms_iou }}, imgsz {{ page.imgsz }}</span>
    </p>
    <p v-if="error" class="text-sm text-red-300" role="alert">{{ error }}</p>
    <p v-else-if="loading && !page" class="py-6 text-center text-sm text-muted-foreground" role="status"><LoaderCircle class="mr-2 inline size-4 animate-spin" />Loading comparison…</p>
    <p v-else-if="page && !page.images.length" class="rounded-xl border border-dashed border-white/15 py-8 text-center text-sm text-muted-foreground">{{ onlyErrors ? 'No test image has missed or false detections.' : 'No test images were evaluated.' }}</p>
    <article v-for="item in page?.images ?? []" :key="item.id" class="space-y-2 rounded-xl border border-white/10 bg-black/10 p-3">
      <p class="truncate font-mono text-[0.7rem] text-muted-foreground">{{ item.id }} · GT {{ item.gt.length }}</p>
      <div class="grid gap-2 sm:grid-cols-2 xl:grid-cols-[repeat(auto-fit,minmax(14rem,1fr))]">
        <figure class="space-y-1">
          <div class="relative overflow-hidden rounded-lg bg-black/40" :style="{ aspectRatio: `${item.width} / ${item.height}` }">
            <img v-if="item.image_url" :src="item.image_url" alt="Original CCTV test image" class="absolute inset-0 size-full object-fill" loading="lazy">
            <p v-else class="absolute inset-0 grid place-items-center p-3 text-center text-xs text-muted-foreground">The original image was deleted from the dataset workspace.</p>
          </div>
          <figcaption class="text-xs font-medium">Original</figcaption>
        </figure>
        <figure v-for="key in order(item.models)" :key="key" class="space-y-1">
          <div class="relative overflow-hidden rounded-lg bg-black/40" :style="{ aspectRatio: `${item.width} / ${item.height}` }">
            <img v-if="item.image_url" :src="item.image_url" alt="" aria-hidden="true" class="absolute inset-0 size-full object-fill" loading="lazy">
            <span v-for="(box, index) in item.gt" :key="`gt${index}`" class="absolute border border-dashed border-emerald-400/80" :style="boxStyle(box, item.width, item.height)" />
            <span v-for="(box, index) in item.models[key]?.pred_tp ?? []" :key="`tp${index}`" class="absolute border-2 border-sky-400" :style="boxStyle(box, item.width, item.height)" />
            <span v-for="(box, index) in item.models[key]?.pred_fp ?? []" :key="`fp${index}`" class="absolute border-2 border-amber-400" :style="boxStyle(box, item.width, item.height)" />
            <span v-for="(box, index) in item.models[key]?.gt_fn ?? []" :key="`fn${index}`" class="absolute border-[3px] border-red-500" :style="boxStyle(box, item.width, item.height)" />
          </div>
          <figcaption class="flex flex-wrap justify-between gap-2 text-xs"><span class="font-medium">{{ MODEL_LABELS[key] ?? key }}</span><span class="font-mono tabular-nums text-muted-foreground">TP {{ item.models[key]?.tp }} · FP {{ item.models[key]?.fp }} · FN {{ item.models[key]?.fn }}</span></figcaption>
        </figure>
      </div>
    </article>
    <nav v-if="page && page.total > PAGE" class="flex items-center justify-center gap-3" aria-label="Comparison pages">
      <Button size="icon-sm" variant="outline" aria-label="Previous images" :disabled="offset === 0 || loading" @click="offset = Math.max(0, offset - PAGE)"><ChevronLeft /></Button>
      <span class="text-xs tabular-nums text-muted-foreground">{{ offset + 1 }}–{{ Math.min(offset + PAGE, page.total) }} / {{ page.total }}</span>
      <Button size="icon-sm" variant="outline" aria-label="Next images" :disabled="offset + PAGE >= page.total || loading" @click="offset += PAGE"><ChevronRight /></Button>
    </nav>
  </section>
</template>
