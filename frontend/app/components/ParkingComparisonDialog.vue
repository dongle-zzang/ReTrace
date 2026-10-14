<script setup lang="ts">
import type { Camera } from '~/types/camera'
import type { ParkingSpaceSummary, ParkingZone, ParkingStatus } from '~/types/parking'
import type { ConnectionState, RealtimePreviewClient } from '~/composables/realtimePreviewClient'
import type { RealtimeCameraStatus } from '~/types/realtime'
import CameraZoomStage from '~/components/CameraZoomStage.vue'
import ParkingStatusBadge from '~/components/ParkingStatusBadge.vue'
import { Dialog, DialogContent, DialogTitle, DialogDescription } from '~/components/ui/dialog'
import { Button } from '~/components/ui/button'
const open = defineModel<boolean>('open', { default: false })
const props = defineProps<{
  summary: ParkingSpaceSummary | null; cameras: Camera[]; zones: Record<string, ParkingZone[]>
  labels: Record<string, string>; zoneStatuses: Record<string, ParkingStatus>
  realtimeClient: RealtimePreviewClient | null; connection: ConnectionState
  statuses: Record<string, RealtimeCameraStatus>; reason?: string | null
}>()
const relevant = computed(() => props.cameras.filter(camera => props.summary?.zones.some(zone => zone.cameraId === camera.camera_id)))
const cameraZones = (id: string) => (props.zones[id] ?? []).filter(zone => props.summary?.spaceIds.includes(zone.parkingSpaceId))
function keepFullscreen(event: Event) { if (document.fullscreenElement) event.preventDefault() }
</script>

<template>
  <Dialog v-model:open="open">
    <DialogContent class="max-h-[88dvh] w-[min(94vw,1400px)] overflow-y-auto sm:max-w-[min(94vw,1400px)]" @escape-key-down="keepFullscreen">
      <DialogTitle>{{ summary?.space.label }} · Camera comparison</DialogTitle>
      <DialogDescription>Compare each camera's view of this parking space.</DialogDescription>
      <div v-if="summary" class="flex items-center gap-3 text-sm"><ParkingStatusBadge :status="summary.status" /><span v-if="summary.conflict" class="text-amber-300">Cameras disagree</span><span v-if="reason" class="text-muted-foreground">{{ reason }}</span></div>
      <p v-if="!relevant.length" class="py-8 text-sm text-muted-foreground">No camera zone assigned. Open a parking camera to draw this space.</p>
      <div class="grid gap-4" :class="relevant.length > 1 ? 'lg:grid-cols-2' : ''">
        <article v-for="camera in relevant" :key="camera.camera_id" class="min-w-0 space-y-2">
          <div class="flex items-center justify-between gap-2"><h3 class="text-sm font-semibold">{{ camera.name }}</h3><Button as-child size="sm" variant="outline"><NuxtLink :to="`/cameras/${encodeURIComponent(camera.camera_id)}/parking`">Edit zones</NuxtLink></Button></div>
          <CameraZoomStage :camera="camera" :realtime-client="realtimeClient" :connection="connection" :live-status="statuses[camera.camera_id]" :parking-zones="cameraZones(camera.camera_id)" :parking-labels="labels" :parking-statuses="zoneStatuses" :selected-zone-id="cameraZones(camera.camera_id)[0]?.id" />
          <div v-for="zone in summary?.zones.filter(zone => zone.cameraId === camera.camera_id)" :key="zone.zoneId" class="flex items-center gap-3 text-xs text-muted-foreground"><ParkingStatusBadge :status="zone.status" :score="zone.score" /><span>{{ zone.updatedAt ? `Updated ${new Date(zone.updatedAt).toLocaleTimeString()}` : 'Waiting for a reading' }}</span></div>
        </article>
      </div>
    </DialogContent>
  </Dialog>
</template>
