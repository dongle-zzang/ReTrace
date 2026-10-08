<script setup lang="ts">
import type { ParkingSpaceSummary } from '~/types/parking'
import { TriangleAlert } from '@lucide/vue'
import ParkingStatusBadge from '~/components/ParkingStatusBadge.vue'
import { Badge } from '~/components/ui/badge'

/**
 * Logical parking spaces with their final status and what each camera sees. One row per label:
 * zones of different cameras with the same label are the same spot.
 */
const props = withDefaults(defineProps<{
  summaries: ParkingSpaceSummary[]
  /** cameraId → display name */
  cameraNames: Record<string, string>
  /** Marks this camera's view (the editor's camera). */
  currentCameraId?: string | null
}>(), { currentCameraId: null })

const cameraName = (cameraId: string) => props.cameraNames[cameraId] ?? cameraId
</script>

<template>
  <ul class="divide-y divide-white/10 border-y border-white/10" aria-label="Parking spaces across cameras">
    <li
      v-for="summary in summaries"
      :key="summary.space.id"
      :class="['py-3 pr-1', summary.conflict ? 'border-l-2 border-l-amber-400/70 pl-3' : 'pl-1']"
    >
      <div class="flex items-center gap-2.5">
        <span class="min-w-0 truncate font-mono text-sm font-semibold">{{ summary.space.label }}</span>
        <Badge
          v-if="summary.conflict"
          variant="outline"
          class="gap-1 border-amber-400/40 text-amber-300"
          title="Cameras report different statuses for this spot. Check the video."
        >
          <TriangleAlert /> Check
        </Badge>
        <span class="ml-auto flex items-center gap-1.5 text-xs text-muted-foreground">
          Final
          <ParkingStatusBadge :status="summary.status" size="md" />
        </span>
      </div>
      <p v-if="summary.conflict" class="mt-1 text-xs text-amber-300">Cameras disagree on this spot. Check the video.</p>
      <ul class="mt-1.5 space-y-0.5 border-l border-white/10 pl-3" :aria-label="summary.space.label + ' by camera'">
        <li v-for="zone in summary.zones" :key="zone.zoneId" class="flex items-center gap-2 text-xs">
          <span :class="['min-w-0 flex-1 truncate', zone.cameraId === currentCameraId ? 'text-foreground' : 'text-muted-foreground']">
            {{ cameraName(zone.cameraId) }}<span v-if="zone.cameraId === currentCameraId" class="text-muted-foreground"> (this camera)</span>
          </span>
          <ParkingStatusBadge :status="zone.status" :score="zone.score" />
        </li>
      </ul>
    </li>
  </ul>
</template>
