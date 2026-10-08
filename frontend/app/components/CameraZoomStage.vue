<script setup lang="ts">
import type { Camera } from '~/types/camera'
import type { ConnectionState, RealtimePreviewClient } from '~/composables/realtimePreviewClient'
import type { RealtimeCameraStatus } from '~/types/realtime'
import type { ParkingStatus, ParkingZone } from '~/types/parking'
import { Maximize2, Minimize2 } from '@lucide/vue'
import CameraVideo from '~/components/CameraVideo.vue'
import ParkingOverlay from '~/components/ParkingOverlay.vue'
import { Button } from '~/components/ui/button'
import { useVideoStage } from '~/composables/useVideoStage'

/** Enlarged video for the zoom dialog. Mounted only while the dialog is open. */
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
}>()

const stageElement = ref<HTMLElement | null>(null)
const mediaSize = ref({ width: 1280, height: 720 })
const { content, contentStyle, isFullscreen, toggleFullscreen } = useVideoStage(stageElement, mediaSize)
</script>

<template>
  <div
    ref="stageElement"
    :class="['group/zoom relative w-full overflow-hidden bg-black', isFullscreen ? 'h-screen' : 'aspect-video max-h-[calc(100dvh-9rem)]']"  >
    <CameraVideo
      :camera="camera"
      :realtime-client="realtimeClient"
      :connection="connection"
      :camera-state="liveStatus ? String(liveStatus.state) : undefined"
      :active="true"
      @dimensions="mediaSize = $event"
    />
    <ParkingOverlay
      v-if="parkingZones?.length"
      class="absolute z-10"
      :style="contentStyle"
      :width="content.width"
      :height="content.height"
      :zones="parkingZones"
      :space-labels="parkingLabels ?? {}"
      :statuses="parkingStatuses ?? {}"
      :conflict-space-ids="parkingConflicts"
    />
    <Button
      type="button"
      size="icon-sm"
      variant="ghost"
      class="absolute top-3 right-3 z-20 border border-white/15 bg-black/45 text-white opacity-80 backdrop-blur-md transition-opacity group-hover/zoom:opacity-100 hover:bg-black/70 hover:text-white"
      :aria-label="isFullscreen ? 'Exit fullscreen' : 'Fullscreen'"
      @click="toggleFullscreen"
    >
      <Minimize2 v-if="isFullscreen" />
      <Maximize2 v-else />
    </Button>
  </div>
</template>
