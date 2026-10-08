<script setup lang="ts">
import type { Camera } from '~/types/camera'
import type { ConnectionState, RealtimePreviewClient } from '~/composables/realtimePreviewClient'
import type { RealtimeCameraStatus } from '~/types/realtime'
import type { ParkingCounts, ParkingStatus, ParkingZone } from '~/types/parking'
import CameraZoomStage from '~/components/CameraZoomStage.vue'
import ParkingSummary from '~/components/ParkingSummary.vue'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '~/components/ui/dialog'

defineProps<{
  camera: Camera
  realtimeClient: RealtimePreviewClient | null
  connection: ConnectionState
  liveStatus?: RealtimeCameraStatus
  parkingZones?: ParkingZone[]
  /** parkingSpaceId → label */
  parkingLabels?: Record<string, string>
  /** zoneId → status */
  parkingStatuses?: Record<string, ParkingStatus>
  /** parkingSpaceIds whose cameras disagree */
  parkingConflicts?: string[]
  parkingCounts?: ParkingCounts | null
  /** e.g. "B1 · Online · 15.2 FPS" */
  statusText: string
}>()

const open = defineModel<boolean>('open', { default: false })

function keepOpenWhileFullscreen(event: Event) {
  // Esc first leaves fullscreen; it should not also close the dialog underneath.
  if (document.fullscreenElement) event.preventDefault()
}
</script>

<template>
  <Dialog v-model:open="open">
    <DialogContent
      class="w-[min(94vw,1400px)] gap-0 overflow-hidden p-0 sm:max-w-[min(94vw,1400px)]"
      @escape-key-down="keepOpenWhileFullscreen"
    >
      <div class="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-1 py-3 pr-12 pl-4">
        <DialogTitle class="truncate text-[15px] font-semibold tracking-tight">{{ camera.name }}</DialogTitle>
        <DialogDescription class="text-xs tabular-nums">{{ statusText }}</DialogDescription>
        <ParkingSummary v-if="parkingCounts" :counts="parkingCounts" compact class="sm:ml-auto" />
      </div>
      <CameraZoomStage
        :camera="camera"
        :realtime-client="realtimeClient"
        :connection="connection"
        :live-status="liveStatus"
        :parking-zones="parkingZones"
        :parking-labels="parkingLabels"
        :parking-statuses="parkingStatuses"
        :parking-conflicts="parkingConflicts"
      />
    </DialogContent>
  </Dialog>
</template>
