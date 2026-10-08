<script setup lang="ts">
import type { ParkingStatus } from '~/types/parking'
import { parkingStatusLabel } from '~/lib/parking'

/** Status as a colored dot plus text (never color alone), optionally with the detection score. */
withDefaults(defineProps<{
  status: ParkingStatus
  score?: number | null
  size?: 'sm' | 'md'
  dot?: boolean
}>(), { score: null, size: 'sm', dot: true })

const dotClass: Record<ParkingStatus, string> = {
  occupied: 'bg-red-500 shadow-[0_0_6px_var(--color-red-500)]',
  empty: 'bg-green-500 shadow-[0_0_6px_var(--color-green-500)]',
  unknown: 'bg-zinc-400',
}

const text: Record<ParkingStatus, string> = {
  occupied: 'text-red-300',
  empty: 'text-green-300',
  unknown: 'text-muted-foreground',
}
</script>

<template>
  <span :class="['inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap', size === 'md' ? 'text-sm font-medium' : 'text-xs']">
    <span v-if="dot" :class="['size-1.5 shrink-0 rounded-full', dotClass[status]]" aria-hidden="true" />
    <span :class="text[status]">{{ parkingStatusLabel[status] }}</span>
    <span v-if="score !== null" class="font-mono text-muted-foreground tabular-nums" :aria-label="'score ' + score.toFixed(2)">{{ score.toFixed(2) }}</span>
  </span>
</template>
