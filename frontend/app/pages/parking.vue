<script setup lang="ts">
import { CircleAlert, RefreshCw, SquareParking } from '@lucide/vue'
import CameraCard from '~/components/CameraCard.vue'
import ParkingSpaceBoard from '~/components/ParkingSpaceBoard.vue'
import ParkingSummary from '~/components/ParkingSummary.vue'
import ParkingComparisonDialog from '~/components/ParkingComparisonDialog.vue'
import ParkingLabelManager from '~/components/ParkingLabelManager.vue'
import ParkingCalibrationManager from '~/components/ParkingCalibrationManager.vue'
import { Input } from '~/components/ui/input'
import { filterParkingSpaces, unknownReason, type SpaceFilter } from '~/lib/parkingOperations'
import { Alert, AlertAction, AlertDescription, AlertTitle } from '~/components/ui/alert'
import { Badge } from '~/components/ui/badge'
import { Button } from '~/components/ui/button'
import { Card, CardContent } from '~/components/ui/card'
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from '~/components/ui/empty'
import { Skeleton } from '~/components/ui/skeleton'
import { useCameras } from '~/composables/useCameras'
import { useI18n } from '~/composables/useI18n'
import { useParking } from '~/composables/useParking'
import { useRealtimePreview } from '~/composables/useRealtimePreview'
import { compareCameras } from '~/lib/cameraSort'
import { isParkingCamera } from '~/lib/parking'

const { t } = useI18n()
useHead({ title: () => `ReTrace · ${t('Parking')}` })

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
const conflictSummaries = computed(() => parking.summaries.value.filter((summary) => summary.conflict))
const conflictSpaceIds = computed(() => conflictSummaries.value.flatMap((summary) => summary.spaceIds))
const filter = ref<SpaceFilter>('all'), search = ref('')
const allZones = computed(() => Object.values(parking.zones.value).flat())
const boardSummaries = computed(() => filterParkingSpaces(parking.summaries.value, search.value, filter.value, allZones.value))
const offlineCameraIds = computed(() => new Set(cameras.value.filter(camera => !camera.enabled || !['online', 'degraded'].includes(String(statuses.value[camera.camera_id]?.state ?? camera.status.state)) || (!statuses.value[camera.camera_id] && camera.status.stale)).map(camera => camera.camera_id)))
const reasons = computed(() => Object.fromEntries(parking.summaries.value.map(row => [row.space.id, unknownReason(row, parking.disconnected.value, parking.delayed.value || row.zones.some(zone => parking.staleZoneIds.value.has(zone.zoneId)), offlineCameraIds.value)])))
const comparisonOpen = ref(false), labelManagerOpen = ref(false), calibrationOpen = ref(false)
const selectedSpaceId = ref<string | null>(null)
const selectedSummary = computed(() => selectedSpaceId.value ? parking.summaryBySpace.value[selectedSpaceId.value] ?? null : null)
function compare(id: string) { selectedSpaceId.value = id; comparisonOpen.value = true }
const missingBaselines = computed(() => allZones.value.filter(zone => zone.calibrated !== true).length)
const freshnessText = computed(() => {
  if (parking.disconnected.value) return t('Server disconnected · readings unavailable')
  if (parking.delayed.value) return t('Waiting for fresh parking status')
  return parking.lastReportAt.value ? t('Status received {seconds}s ago', { seconds: Math.max(0, Math.floor((parking.now.value - parking.lastReportAt.value) / 1000)) }) : t('Waiting for parking status')
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
          <h1 class="text-3xl font-semibold tracking-tight sm:text-4xl">{{ t('Parking') }}</h1>
          <!-- div, not p: Badge renders a div. -->
          <div class="mt-2 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
            {{ t('Each label is one real spot; cameras that see the same label are combined.') }}
            <Badge v-if="parking.api.kind === 'mock'" variant="outline" class="text-muted-foreground">{{ t('Mock data') }}</Badge>
          </div>
        </div>
        <div class="w-full lg:w-[34rem]">
          <ParkingSummary :counts="parking.totals.value" interactive :selected="filter" @select="filter = $event" />
        </div>
      </div>
    </div>

    <section :aria-label="t('Parking')" class="mx-auto max-w-[1500px] px-5 pt-8 pb-16 sm:px-8 lg:px-10">
      <div class="mb-6 flex flex-wrap items-center gap-2">
        <p class="mr-auto text-xs" :class="parking.disconnected.value || parking.delayed.value ? 'text-amber-300' : 'text-muted-foreground'" role="status">{{ freshnessText }}</p>
        <Button size="sm" variant="outline" @click="calibrationOpen = true">{{ t('Empty baselines') }} <span class="tabular-nums text-muted-foreground">{{ missingBaselines }}</span></Button>
        <Button size="sm" variant="outline" @click="labelManagerOpen = true">{{ t('Manage labels') }}</Button>
        <Button size="sm" variant="outline" :disabled="parking.loading.value" @click="parking.reload()">{{ t('Refresh status') }}</Button>
      </div>
      <Alert v-if="error" variant="destructive" class="motion-safe:animate-fade-up mb-8 border-destructive/30 bg-destructive/10 px-4 py-3">
        <CircleAlert />
        <AlertTitle>{{ t("Couldn't load cameras") }}</AlertTitle>
        <AlertDescription>{{ t(error) }}</AlertDescription>
        <AlertAction class="top-1/2 -translate-y-1/2">
          <Button size="sm" variant="outline" :disabled="refreshing" @click="refresh">{{ t('Retry') }}</Button>
        </AlertAction>
      </Alert>

      <div
        v-if="parking.error.value || parking.statusError.value"
        class="mb-6 flex items-center gap-2 rounded-lg bg-destructive/10 py-1.5 pr-1.5 pl-3 text-xs text-red-300 motion-safe:animate-fade-up"
        role="alert"
      >
        <CircleAlert class="size-3.5 shrink-0" />
        <span class="min-w-0 flex-1">{{ t(parking.error.value ?? parking.statusError.value ?? '') }}</span>
        <Button size="sm" variant="ghost" class="h-6 shrink-0 px-2 text-xs text-red-200 hover:bg-red-400/10 hover:text-red-100" @click="parking.reload()">
          {{ t('Retry') }}
        </Button>
      </div>

      <div v-if="loading" class="grid gap-5 sm:grid-cols-2 xl:grid-cols-3" :aria-label="t('Loading parking cameras')">
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
          <EmptyTitle class="text-base">{{ t('No parking cameras') }}</EmptyTitle>
          <EmptyDescription>{{ t("Cameras with '주차장' in their name show up here.") }}</EmptyDescription>
        </EmptyHeader>
        <EmptyContent>
          <Button variant="outline" :disabled="refreshing" @click="refresh">
            <RefreshCw :class="{ 'animate-spin': refreshing }" />
            {{ t('Refresh') }}
          </Button>
        </EmptyContent>
      </Empty>

      <div v-else-if="parkingCameras.length" class="grid gap-6 xl:grid-cols-[minmax(0,1fr)_24rem]">
        <div class="min-w-0">
          <div class="mb-4 flex items-center gap-3 motion-safe:animate-fade-up">
            <h2 class="text-lg font-semibold tracking-tight">{{ t('Parking cameras') }}</h2>
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
        <aside class="min-w-0 motion-safe:animate-fade-up xl:sticky xl:top-24 xl:self-start" :aria-label="t('Spaces')">
          <div class="mb-4 flex items-center gap-3">
            <h2 class="text-lg font-semibold tracking-tight">{{ t('Spaces') }}</h2>
            <Badge variant="secondary" class="tabular-nums">{{ parking.summaries.value.length }}</Badge>
            <Button
              v-if="conflictSpaceIds.length"
              size="sm"
              :variant="filter === 'check' ? 'secondary' : 'ghost'"
              class="ml-auto h-7 gap-1.5 text-amber-300"
              :aria-pressed="filter === 'check'"
              @click="filter = filter === 'check' ? 'all' : 'check'"
            >
              {{ t('{count} to check', { count: conflictSummaries.length }) }}
            </Button>
          </div>
          <div class="mb-3 space-y-2">
            <Input v-model="search" type="search" :aria-label="t('Search parking labels')" :placeholder="t('Search labels')" />
            <div class="flex flex-wrap gap-1.5"><Button v-for="option in ([['all', 'All'], ['occupied', 'Occupied'], ['empty', 'Empty'], ['unknown', 'Unknown'], ['unused', 'Unused'], ['uncalibrated', 'No baseline']] as const)" :key="option[0]" size="sm" :variant="filter === option[0] ? 'secondary' : 'ghost'" class="h-7 px-2 text-xs" :aria-pressed="filter === option[0]" @click="filter = option[0]">{{ t(option[1]) }}</Button></div>
            <p class="text-xs text-muted-foreground">{{ t('{shown} of {total} spaces', { shown: boardSummaries.length, total: parking.summaries.value.length }) }}</p>
          </div>
          <div v-if="parking.loading.value" class="space-y-2">
            <Skeleton v-for="item in 4" :key="item" class="h-14 w-full" />
          </div>
          <p v-else-if="!parking.summaries.value.length" class="border-t border-white/10 py-6 text-sm text-muted-foreground">
            {{ t('No labelled zones yet. Open a camera with the P button to draw zones.') }}
          </p>
          <div v-else class="parking-scroll max-h-[max(12rem,calc(100dvh-23rem))] overflow-y-auto pr-2">
            <ParkingSpaceBoard :summaries="boardSummaries" :camera-names="cameraNames" :reasons="reasons" selectable @select="compare" />
            <p v-if="!boardSummaries.length" class="py-6 text-sm text-muted-foreground">{{ t('No spaces match this search or filter.') }}</p>
          </div>
        </aside>
      </div>
    </section>
    <ParkingComparisonDialog v-model:open="comparisonOpen" :summary="selectedSummary" :cameras="cameras" :zones="parking.zones.value" :labels="parking.spaceLabels.value" :zone-statuses="parking.zoneStatuses.value" :realtime-client="realtimeClient" :connection="connection" :statuses="statuses" :reason="selectedSummary ? reasons[selectedSummary.space.id] : null" />
    <ParkingLabelManager v-model:open="labelManagerOpen" :spaces="parking.spaces.value" :zones="allZones" :camera-names="cameraNames" :rename="parking.renameSpace" :remove="parking.deleteSpace" />
    <ParkingCalibrationManager v-model:open="calibrationOpen" :zones="allZones" :labels="parking.spaceLabels.value" :camera-names="cameraNames" :statuses="parking.zoneStatuses.value" :calibrate="parking.calibrate" @compare="compare" />
  </main>
</template>

<style scoped>
.parking-scroll {
  scrollbar-width: thin;
  scrollbar-color: rgb(255 255 255 / 0.22) transparent;
  scrollbar-gutter: stable;
}
.parking-scroll:hover { scrollbar-color: rgb(255 255 255 / 0.38) transparent; }
.parking-scroll::-webkit-scrollbar { width: 6px; }
.parking-scroll::-webkit-scrollbar-track { background: transparent; }
.parking-scroll::-webkit-scrollbar-thumb { border-radius: 999px; background: rgb(255 255 255 / 0.22); }
.parking-scroll::-webkit-scrollbar-thumb:hover { background: rgb(255 255 255 / 0.38); }
</style>
