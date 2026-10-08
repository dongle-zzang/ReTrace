<script setup lang="ts">
import { CircleAlert, RefreshCw, SquareParking, TriangleAlert } from '@lucide/vue'
import CameraCard from '~/components/CameraCard.vue'
import ParkingSpaceBoard from '~/components/ParkingSpaceBoard.vue'
import ParkingSummary from '~/components/ParkingSummary.vue'
import { Alert, AlertAction, AlertDescription, AlertTitle } from '~/components/ui/alert'
import { Badge } from '~/components/ui/badge'
import { Button } from '~/components/ui/button'
import { Card, CardContent } from '~/components/ui/card'
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from '~/components/ui/empty'
import { Skeleton } from '~/components/ui/skeleton'
import { useCameras } from '~/composables/useCameras'
import { useParking } from '~/composables/useParking'
import { useRealtimePreview } from '~/composables/useRealtimePreview'
import { compareCameras } from '~/lib/cameraSort'
import { isParkingCamera } from '~/lib/parking'

useHead({ title: 'ReTrace · Parking' })

const config = useRuntimeConfig()
const { cameras, loading, refreshing, error, refresh } = useCameras()
const { client: realtimeClient, connection, statuses, onMessage } = useRealtimePreview(config.public.previewVideoFps)
const namedParkingCameraIds = computed(() => cameras.value.filter((camera) => isParkingCamera(camera.name)).map((camera) => camera.camera_id))
const parking = useParking({ realtime: { connection, onMessage }, cameraIds: namedParkingCameraIds })

// Parking cameras are named '…주차장…'; a camera that already has zones is listed too.
const parkingCameras = computed(() => cameras.value
  .filter((camera) => isParkingCamera(camera.name) || !!parking.zones.value[camera.camera_id]?.length)
  .sort(compareCameras))

const cameraNames = computed(() => Object.fromEntries(cameras.value.map((camera) => [camera.camera_id, camera.name])))
const conflictSpaceIds = computed(() => parking.summaries.value.filter((summary) => summary.conflict).map((summary) => summary.space.id))
const showConflictsOnly = ref(false)
const boardSummaries = computed(() =>
  showConflictsOnly.value ? parking.summaries.value.filter((summary) => summary.conflict) : parking.summaries.value)

watch(conflictSpaceIds, (ids) => {
  if (!ids.length) showConflictsOnly.value = false
})

function enterDelay(index: number, step = 70, base = 0) {
  return { animationDelay: `${base + Math.min(index, 12) * step}ms` }
}
</script>

<template>
  <main class="text-foreground">
    <div class="mx-auto max-w-[1500px] px-5 pt-10 sm:px-8 sm:pt-12 lg:px-10">
      <div class="flex flex-col gap-6 lg:flex-row lg:items-end lg:justify-between motion-safe:animate-fade-up">
        <div class="min-w-0">
          <h1 class="text-3xl font-semibold tracking-tight sm:text-4xl">Parking</h1>
          <!-- div, not p: Badge renders a div. -->
          <div class="mt-2 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
            Each label is one real spot; cameras that see the same label are combined.
            <Badge v-if="parking.api.kind === 'mock'" variant="outline" class="text-muted-foreground">Mock data</Badge>
          </div>
        </div>
        <div class="w-full lg:w-[34rem]">
          <ParkingSummary :counts="parking.totals.value" />
        </div>
      </div>
    </div>

    <section aria-label="Parking" class="mx-auto max-w-[1500px] px-5 pt-8 pb-16 sm:px-8 lg:px-10">
      <Alert v-if="error" variant="destructive" class="motion-safe:animate-fade-up mb-8 border-destructive/30 bg-destructive/10 px-4 py-3">
        <CircleAlert />
        <AlertTitle>Couldn't load cameras</AlertTitle>
        <AlertDescription>{{ error }}</AlertDescription>
        <AlertAction class="top-1/2 -translate-y-1/2">
          <Button size="sm" variant="outline" :disabled="refreshing" @click="refresh">Retry</Button>
        </AlertAction>
      </Alert>

      <div
        v-if="parking.error.value || parking.statusError.value"
        class="mb-6 flex items-center gap-2 rounded-lg bg-destructive/10 py-1.5 pr-1.5 pl-3 text-xs text-red-300 motion-safe:animate-fade-up"
        role="alert"
      >
        <CircleAlert class="size-3.5 shrink-0" />
        <span class="min-w-0 flex-1">{{ parking.error.value ?? parking.statusError.value }}</span>
        <Button size="sm" variant="ghost" class="h-6 shrink-0 px-2 text-xs text-red-200 hover:bg-red-400/10 hover:text-red-100" @click="parking.reload()">
          Retry
        </Button>
      </div>

      <div v-if="loading" class="grid gap-5 sm:grid-cols-2 xl:grid-cols-3" aria-label="Loading parking cameras">
        <Card v-for="item in 3" :key="item" :style="enterDelay(item - 1)" class="gap-0 py-0 motion-safe:animate-fade-up">
          <Skeleton class="aspect-video w-full rounded-none" />
          <CardContent class="space-y-3 px-5 py-5">
            <Skeleton class="h-4 w-2/3" />
            <Skeleton class="h-3 w-1/3" />
          </CardContent>
        </Card>
      </div>

      <Empty v-else-if="!parkingCameras.length && !error" class="border border-border bg-card/40 py-16 motion-safe:animate-fade-up">
        <EmptyHeader>
          <EmptyMedia variant="icon" class="size-12 rounded-xl">
            <SquareParking class="size-6" />
          </EmptyMedia>
          <EmptyTitle class="text-base">No parking cameras</EmptyTitle>
          <EmptyDescription>Cameras with '주차장' in their name show up here.</EmptyDescription>
        </EmptyHeader>
        <EmptyContent>
          <Button variant="outline" :disabled="refreshing" @click="refresh">
            <RefreshCw :class="{ 'animate-spin': refreshing }" />
            Refresh
          </Button>
        </EmptyContent>
      </Empty>

      <div v-else-if="parkingCameras.length" class="grid gap-6 xl:grid-cols-[minmax(0,1fr)_24rem]">
        <div class="min-w-0">
          <div class="mb-4 flex items-center gap-3 motion-safe:animate-fade-up">
            <h2 class="text-lg font-semibold tracking-tight">Parking cameras</h2>
            <Badge variant="secondary" class="tabular-nums">{{ parkingCameras.length }}</Badge>
          </div>
          <div class="grid gap-5 sm:grid-cols-2">
            <div
              v-for="(camera, index) in parkingCameras"
              :key="camera.camera_id"
              :style="enterDelay(index, 70, 80)"
              class="motion-safe:animate-fade-up"
            >
              <CameraCard
                :camera="camera"
                :realtime-client="realtimeClient"
                :connection="connection"
                :live-status="statuses[camera.camera_id]"
                :parking-zones="parking.zones.value[camera.camera_id]"
                :parking-labels="parking.spaceLabels.value"
                :parking-statuses="parking.zoneStatuses.value"
                :parking-conflicts="conflictSpaceIds"
                manage-parking
              />
            </div>
          </div>
        </div>

        <!-- Plain column split by lines: the camera cards on the left already carry the card look. -->
        <aside class="min-w-0 motion-safe:animate-fade-up xl:sticky xl:top-24 xl:self-start" aria-label="Spaces">
          <div class="mb-4 flex items-center gap-3">
            <h2 class="text-lg font-semibold tracking-tight">Spaces</h2>
            <Badge variant="secondary" class="tabular-nums">{{ parking.summaries.value.length }}</Badge>
            <Button
              v-if="conflictSpaceIds.length"
              size="sm"
              :variant="showConflictsOnly ? 'secondary' : 'ghost'"
              class="ml-auto h-7 gap-1.5 text-amber-300"
              :aria-pressed="showConflictsOnly"
              @click="showConflictsOnly = !showConflictsOnly"
            >
              <TriangleAlert /> {{ conflictSpaceIds.length }} to check
            </Button>
          </div>
          <div v-if="parking.loading.value" class="space-y-2">
            <Skeleton v-for="item in 4" :key="item" class="h-14 w-full" />
          </div>
          <p v-else-if="!parking.summaries.value.length" class="border-t border-white/10 py-6 text-sm text-muted-foreground">
            No labelled zones yet. Open a camera with the P button to draw zones.
          </p>
          <div v-else class="max-h-[calc(100dvh-12rem)] overflow-y-auto">
            <ParkingSpaceBoard :summaries="boardSummaries" :camera-names="cameraNames" />
          </div>
        </aside>
      </div>
    </section>
  </main>
</template>
