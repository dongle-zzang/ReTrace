<script setup lang="ts">
import type { Camera } from '~/types/camera'
import type { ConnectionState, RealtimePreviewClient } from '~/composables/realtimePreviewClient'
import type { RealtimeCameraStatus } from '~/types/realtime'
import type { CameraOverlayData, OverlaySelection } from '~/types/overlay'
import type { ParkingStatus, ParkingZone } from '~/types/parking'
import { emptyCameraOverlay } from '~/types/overlay'
import CameraVideo from '~/components/CameraVideo.vue'
import CameraOverlay from '~/components/CameraOverlay.vue'
import ParkingOverlay from '~/components/ParkingOverlay.vue'
import ParkingSummary from '~/components/ParkingSummary.vue'
import CameraZoomDialog from '~/components/CameraZoomDialog.vue'
import { useVideoStage } from '~/composables/useVideoStage'
import { countStatuses } from '~/lib/parking'
import { floorLabel } from '~/lib/cameraSort'
import { SquareParking, ZoomIn } from '@lucide/vue'
import { Button } from '~/components/ui/button'
import { Card, CardHeader, CardTitle } from '~/components/ui/card'
import { Tooltip, TooltipContent, TooltipTrigger } from '~/components/ui/tooltip'

const props = defineProps<{
  camera: Camera
  realtimeClient: RealtimePreviewClient | null
  connection: ConnectionState
  liveStatus?: RealtimeCameraStatus
  annotations?: CameraOverlayData
  parkingZones?: ParkingZone[]
  /** parkingSpaceId → label */
  parkingLabels?: Record<string, string>
  /** zoneId → status */
  parkingStatuses?: Record<string, ParkingStatus>
  /** parkingSpaceIds whose cameras disagree */
  parkingConflicts?: string[]
  /** Show the link to this camera's parking space editor (parking page only). */
  manageParking?: boolean
}>()

const stageElement = ref<HTMLElement | null>(null)
const isVisible = ref(false)
const mediaSize = ref({ width: 1280, height: 720 })
const selection = ref<OverlaySelection>(null)
const emptyAnnotations = emptyCameraOverlay()
const displayAnnotations = computed(() => props.annotations ?? emptyAnnotations)
const { content, contentStyle: overlayStyle } = useVideoStage(stageElement, mediaSize)
const parkingCounts = computed(() =>
  props.parkingZones?.length ? countStatuses(props.parkingZones.map((zone) => zone.id), props.parkingStatuses ?? {}) : null,
)
let observer: IntersectionObserver | undefined

// Live /ws camera_status takes precedence over the 10-second API snapshot.
const status = computed(() => {
  const live = props.liveStatus
  if (!live) return props.camera.status
  return {
    ...props.camera.status,
    state: String(live.state),
    fps: Number(live.fps),
    last_error: live.last_error ?? props.camera.status.last_error,
    stale: false,
  }
})

const statusLabel = computed(() => {
  if (!props.camera.enabled) return 'Disabled'
  if (status.value.stale) return 'Stale'
  if (status.value.state === 'online') return 'Online'
  return status.value.state
})

const statusDotClass = computed(() => {
  if (!props.camera.enabled) return 'bg-muted-foreground/50'
  if (status.value.stale) return 'bg-amber-400 shadow-[0_0_8px_var(--color-amber-400)] animate-pulse'
  if (status.value.state === 'online') return 'bg-emerald-400 shadow-[0_0_8px_var(--color-emerald-400)]'
  return 'bg-rose-500 shadow-[0_0_8px_var(--color-rose-500)] animate-pulse'
})

const fpsLabel = computed(() =>
  Number.isFinite(status.value.fps) ? status.value.fps.toFixed(1) : '—',
)

const zoomOpen = ref(false)
const zoomStatusText = computed(() => `${floorLabel(props.camera.floor)} · ${statusLabel.value} · ${fpsLabel.value} FPS`)

watch(() => props.camera.camera_id, () => {
  selection.value = null
})

onMounted(() => {
  if (!stageElement.value) return
  if (typeof IntersectionObserver !== 'undefined') {
    observer = new IntersectionObserver(([entry]) => {
      isVisible.value = entry?.isIntersecting ?? false
    }, { rootMargin: '120px 0px' })
    observer.observe(stageElement.value)
  } else {
    isVisible.value = true
  }
})

onUnmounted(() => {
  observer?.disconnect()
})
</script>

<template>
  <Card class="glass-panel group gap-0 bg-white/[0.045] py-0 ring-0">
    <div
      ref="stageElement"
      class="relative aspect-video overflow-hidden bg-black"
    >
      <CameraVideo
        :camera="camera"
        :realtime-client="realtimeClient"
        :connection="connection"
        :camera-state="liveStatus ? String(liveStatus.state) : undefined"
        :active="isVisible"
        @dimensions="mediaSize = $event"
      />
      <CameraOverlay
        class="absolute z-10"
        :style="overlayStyle"
        :aspect-ratio="mediaSize.width / mediaSize.height"
        :annotations="displayAnnotations"
        :selection="selection"
        :editable="false"
        @select="selection = $event"
      />
      <ParkingOverlay
        v-if="parkingZones?.length"
        class="absolute z-10"
        :style="overlayStyle"
        :width="content.width"
        :height="content.height"
        :zones="parkingZones"
        :space-labels="parkingLabels ?? {}"
        :statuses="parkingStatuses ?? {}"
        :conflict-space-ids="parkingConflicts"
      />
      <div class="pointer-events-none absolute inset-x-0 top-0 z-20 h-16 bg-gradient-to-b from-black/50 to-transparent" />
      <Button
        v-if="manageParking"
        as-child
        size="icon-sm"
        variant="ghost"
        class="absolute top-3 right-12 z-20 border border-white/15 bg-black/45 text-white opacity-80 backdrop-blur-md transition-opacity group-hover:opacity-100 hover:bg-black/70 hover:text-white"
      >
        <NuxtLink :to="'/cameras/' + encodeURIComponent(camera.camera_id) + '/parking'" :aria-label="camera.name + ' parking zones'">
          <SquareParking />
        </NuxtLink>
      </Button>
      <!-- Fullscreen lives in the zoom dialog only. -->
      <Button
        type="button"
        size="icon-sm"
        variant="ghost"
        class="absolute top-3 right-3 z-20 border border-white/15 bg-black/45 text-white opacity-80 backdrop-blur-md transition-opacity group-hover:opacity-100 hover:bg-black/70 hover:text-white"
        :aria-label="'Zoom ' + camera.name"
        @click="zoomOpen = true"
      >
        <ZoomIn />
      </Button>
    </div>

    <CardHeader class="px-5 py-4">
      <div class="flex items-center justify-between gap-3">
        <CardTitle class="min-w-0 truncate text-[15px] font-semibold tracking-tight">{{ camera.name }}</CardTitle>
        <Tooltip :disabled="!status.last_error">
          <TooltipTrigger as-child>
            <div
              tabindex="0"
              :aria-label="'Status: ' + statusLabel + (status.last_error ? ', recent error' : '')"
              class="flex shrink-0 cursor-default items-center gap-2 rounded-md outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
            >
              <span :class="['size-2 rounded-full transition-colors duration-500', statusDotClass]" />
              <span class="font-mono text-sm font-semibold tabular-nums">
                {{ fpsLabel }}
                <span class="font-sans text-xs font-normal text-muted-foreground">FPS</span>
              </span>
            </div>
          </TooltipTrigger>
          <TooltipContent side="bottom" align="end" class="flex-col items-start gap-1">
            <span class="font-mono break-all">{{ status.last_error }}</span>
          </TooltipContent>
        </Tooltip>
      </div>
      <ParkingSummary v-if="parkingCounts" :counts="parkingCounts" compact class="mt-2" />
    </CardHeader>

    <CameraZoomDialog
      v-model:open="zoomOpen"
      :camera="camera"
      :realtime-client="realtimeClient"
      :connection="connection"
      :live-status="liveStatus"
      :parking-zones="parkingZones"
      :parking-labels="parkingLabels"
      :parking-statuses="parkingStatuses"
      :parking-conflicts="parkingConflicts"
      :parking-counts="parkingCounts"
      :status-text="zoomStatusText"
    />

  </Card>
</template>
