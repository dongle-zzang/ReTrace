<script setup lang="ts">
import type { ParkingCounts } from '~/types/parking'
import { useI18n } from '~/composables/useI18n'

const props = withDefaults(defineProps<{
  counts: ParkingCounts
  compact?: boolean
  interactive?: boolean
  selected?: string
}>(), { compact: false, interactive: false, selected: 'all' })
const emit = defineEmits<{ select: [status: 'all' | 'occupied' | 'empty' | 'unknown'] }>()

const { t } = useI18n()
const items = computed(() => [
  { key: 'total', label: t('Total'), value: props.counts.total, dot: 'ring-1 ring-inset ring-white/40' },
  { key: 'occupied', label: t('Occupied'), value: props.counts.occupied, dot: 'bg-red-500 shadow-[0_0_8px_var(--color-red-500)]' },
  { key: 'empty', label: t('Empty'), value: props.counts.empty, dot: 'bg-green-500 shadow-[0_0_8px_var(--color-green-500)]' },
  { key: 'unknown', label: t('Unknown'), value: props.counts.unknown, dot: 'bg-zinc-400' },
])

const description = computed(() => items.value.map((item) => `${item.label} ${item.value}`).join(', '))
</script>

<template>
  <p v-if="compact" class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground" :aria-label="t('Parking spaces: {summary}', { summary: description })">
    <span v-for="item in items" :key="item.key" class="flex items-center gap-1.5" aria-hidden="true">
      <span :class="['size-1.5 rounded-full', item.dot]" />
      {{ item.label }}
      <span class="font-mono font-semibold tabular-nums text-foreground">{{ item.value }}</span>
    </span>
  </p>
  <!-- Columns follow the container width: the side panel is about as narrow as a phone. -->
  <div v-else class="@container">
    <dl class="grid grid-cols-2 gap-2 @sm:grid-cols-4" :aria-label="t('Parking spaces: {summary}', { summary: description })">
      <div v-for="item in items" :key="item.key" class="glass-panel rounded-xl px-3 py-2.5">
        <button v-if="interactive" type="button" class="-mx-3 -my-2.5 block w-[calc(100%+1.5rem)] rounded-xl px-3 py-2.5 text-left transition-colors hover:bg-white/5 focus-visible:outline-2 focus-visible:outline-ring" :class="selected === (item.key === 'total' ? 'all' : item.key) ? 'bg-primary/10 ring-1 ring-primary/40' : ''" :aria-pressed="selected === (item.key === 'total' ? 'all' : item.key)" :aria-label="t(item.key === 'total' ? 'Show all spaces ({count})' : 'Show {status} spaces ({count})', { status: item.label.toLowerCase(), count: item.value })" @click="emit('select', item.key === 'total' ? 'all' : item.key as 'occupied' | 'empty' | 'unknown')">
          <span class="flex items-center gap-1.5 text-xs text-muted-foreground"><span :class="['size-1.5 shrink-0 rounded-full', item.dot]" />{{ item.label }}</span>
          <span class="mt-1 block font-mono text-xl font-semibold tabular-nums">{{ item.value }}</span>
        </button>
        <dt v-if="!interactive" class="flex items-center gap-1.5 text-xs whitespace-nowrap text-muted-foreground">
          <span :class="['size-1.5 shrink-0 rounded-full', item.dot]" />
          {{ item.label }}
        </dt>
        <dd v-if="!interactive" class="mt-1 font-mono text-xl font-semibold tabular-nums">{{ item.value }}</dd>
      </div>
    </dl>
  </div>
</template>
