<script setup lang="ts">
import type { ParkingCounts } from '~/types/parking'

const props = withDefaults(defineProps<{
  counts: ParkingCounts
  compact?: boolean
}>(), { compact: false })

const items = computed(() => [
  { key: 'total', label: 'Total', value: props.counts.total, dot: 'ring-1 ring-inset ring-white/40' },
  { key: 'occupied', label: 'Occupied', value: props.counts.occupied, dot: 'bg-red-500 shadow-[0_0_8px_var(--color-red-500)]' },
  { key: 'empty', label: 'Empty', value: props.counts.empty, dot: 'bg-green-500 shadow-[0_0_8px_var(--color-green-500)]' },
  { key: 'unknown', label: 'Unknown', value: props.counts.unknown, dot: 'bg-zinc-400' },
])

const description = computed(() => items.value.map((item) => `${item.label} ${item.value}`).join(', '))
</script>

<template>
  <p v-if="compact" class="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground" :aria-label="'Parking spaces: ' + description">
    <span v-for="item in items" :key="item.key" class="flex items-center gap-1.5" aria-hidden="true">
      <span :class="['size-1.5 rounded-full', item.dot]" />
      {{ item.label }}
      <span class="font-mono font-semibold tabular-nums text-foreground">{{ item.value }}</span>
    </span>
  </p>
  <!-- Columns follow the container width: the side panel is about as narrow as a phone. -->
  <div v-else class="@container">
    <dl class="grid grid-cols-2 gap-2 @sm:grid-cols-4" :aria-label="'Parking spaces: ' + description">
      <div v-for="item in items" :key="item.key" class="glass-panel rounded-xl px-3 py-2.5">
        <dt class="flex items-center gap-1.5 text-xs whitespace-nowrap text-muted-foreground">
          <span :class="['size-1.5 shrink-0 rounded-full', item.dot]" />
          {{ item.label }}
        </dt>
        <dd class="mt-1 font-mono text-xl font-semibold tabular-nums">{{ item.value }}</dd>
      </div>
    </dl>
  </div>
</template>
