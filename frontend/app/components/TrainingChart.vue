<script setup lang="ts">
import { chartDomain, formatMetric, linePath, type ChartSeries } from '~/lib/training'
import { useI18n } from '~/composables/useI18n'

const props = defineProps<{ title: string; points: Record<string, number | null>[]; series: ChartSeries[] }>()
const { t } = useI18n()
const layout = { width: 320, height: 150, padding: 12 }
const domain = computed(() => chartDomain(props.points, props.series.map(item => item.key)))
const paths = computed(() => domain.value ? props.series.map(item => ({ ...item, d: linePath(props.points, item.key, domain.value!, layout) })) : [])
const last = computed(() => props.points.at(-1))
</script>

<template>
  <figure class="min-w-0 space-y-2 rounded-xl border border-white/10 bg-black/10 p-3">
    <figcaption class="flex flex-wrap items-center justify-between gap-2 text-xs">
      <span class="font-semibold">{{ title }}</span>
      <span class="flex flex-wrap gap-3">
        <span v-for="item in series" :key="item.key" class="flex items-center gap-1.5 text-muted-foreground">
          <span :class="['h-0.5 w-3 rounded-full', item.dotClass]" />{{ item.label }}
          <span class="font-mono tabular-nums text-foreground">{{ formatMetric(last?.[item.key]) }}</span>
        </span>
      </span>
    </figcaption>
    <svg v-if="domain && points.length" :viewBox="`0 0 ${layout.width} ${layout.height}`" class="h-36 w-full" role="img" :aria-label="t('{title} by epoch', { title })" preserveAspectRatio="none">
      <line v-for="n in 3" :key="n" :x1="layout.padding" :x2="layout.width - layout.padding" :y1="layout.padding + (n - 1) * (layout.height - 2 * layout.padding) / 2" :y2="layout.padding + (n - 1) * (layout.height - 2 * layout.padding) / 2" class="stroke-white/10" stroke-width="1" vector-effect="non-scaling-stroke" />
      <path v-for="item in paths" :key="item.key" :d="item.d" fill="none" :class="item.className" stroke-width="2" stroke-linejoin="round" vector-effect="non-scaling-stroke" />
    </svg>
    <p v-else class="grid h-36 place-items-center text-xs text-muted-foreground">{{ t('Values appear after the first epoch.') }}</p>
    <p v-if="domain && points.length" class="flex justify-between font-mono text-[0.7rem] tabular-nums text-muted-foreground">
      <span>{{ t('epoch') }} {{ points[0]?.epoch }}</span><span>{{ formatMetric(domain.min) }} – {{ formatMetric(domain.max) }}</span><span>{{ t('epoch') }} {{ last?.epoch }}</span>
    </p>
  </figure>
</template>
