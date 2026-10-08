<script setup lang="ts">
import { ArrowLeft, Check, CircleAlert, Maximize2, Minimize2, Pencil, Plus, SquareParking, Undo2, X } from '@lucide/vue'
import CameraVideo from '~/components/CameraVideo.vue'
import ParkingCalibrationPanel from '~/components/ParkingCalibrationPanel.vue'
import ParkingLabelList from '~/components/ParkingLabelList.vue'
import ParkingOverlay from '~/components/ParkingOverlay.vue'
import ParkingSpaceBoard from '~/components/ParkingSpaceBoard.vue'
import ParkingSummary from '~/components/ParkingSummary.vue'
import { Badge } from '~/components/ui/badge'
import { Button } from '~/components/ui/button'
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from '~/components/ui/empty'
import { Skeleton } from '~/components/ui/skeleton'
import { Tooltip, TooltipContent, TooltipTrigger } from '~/components/ui/tooltip'
import { ParkingApiError } from '~/composables/parkingApi'
import { useCameras } from '~/composables/useCameras'
import { useParking } from '~/composables/useParking'
import { useParkingEditor } from '~/composables/useParkingEditor'
import { useRealtimePreview } from '~/composables/useRealtimePreview'
import { useVideoStage } from '~/composables/useVideoStage'
import { floorLabel } from '~/lib/cameraSort'
import { countStatuses } from '~/lib/parking'

const route = useRoute()
const cameraId = computed(() => String(route.params.cameraId ?? ''))

const config = useRuntimeConfig()
const { cameras, loading: camerasLoading } = useCameras()
const { client: realtimeClient, connection, statuses: liveStatuses, onMessage } = useRealtimePreview(config.public.previewVideoFps)
const parking = useParking({ realtime: { connection, onMessage }, cameraIds: computed(() => [cameraId.value]) })
const editor = useParkingEditor(cameraId, parking.spaces)

const camera = computed(() => cameras.value.find((item) => item.camera_id === cameraId.value) ?? null)
const liveStatus = computed(() => liveStatuses.value[cameraId.value])
const savedZones = computed(() => parking.zones.value[cameraId.value])
const zones = computed(() => editor.draft.value ?? savedZones.value ?? [])
const counts = computed(() => countStatuses(zones.value.map((zone) => zone.id), parking.zoneStatuses.value))
const problemByZone = computed(() => Object.fromEntries(editor.problems.value.map((problem) => [problem.zoneId, problem.message])))
const invalidIds = computed(() => Object.keys(problemByZone.value))
const conflictSpaceIds = computed(() => parking.summaries.value.filter((summary) => summary.conflict).map((summary) => summary.space.id))
const cameraNames = computed(() => Object.fromEntries(cameras.value.map((item) => [item.camera_id, item.name])))
const zoneScores = computed(() => Object.fromEntries(Object.values(parking.zoneStates.value).map((state) => [state.zoneId, state.score])))

useHead({ title: () => `ReTrace · ${camera.value?.name ?? cameraId.value} parking zones` })

const viewerElement = ref<HTMLElement | null>(null)
const stageElement = ref<HTMLElement | null>(null)
const mediaSize = ref({ width: 1280, height: 720 })
// The toolbar sits above the video (never over it) and goes fullscreen with it.
const { content, contentStyle, isFullscreen, toggleFullscreen } = useVideoStage(stageElement, mediaSize, 'contain', viewerElement)

const saving = ref(false)
const saveError = ref<string | null>(null)

/** One notice for the whole page: load, status or save failure. */
const pageError = computed(() => saveError.value ?? parking.error.value ?? parking.statusError.value)

function retry() {
  saveError.value = null
  void parking.reload()
}

// ---- Selection ---------------------------------------------------------------------------------

/** View mode: the zone whose details (status, calibration) are shown. */
const focusedZoneId = ref<string | null>(null)
/** Label picked for the next shape, when no zone is selected. */
const pendingSpaceId = ref<string | null>(null)

const selectedZoneId = computed(() => (editor.editing.value ? editor.selectedId.value : focusedZoneId.value))
const selectedZone = computed(() => zones.value.find((zone) => zone.id === selectedZoneId.value) ?? null)
const activeSpaceId = computed(() => selectedZone.value?.parkingSpaceId || pendingSpaceId.value)
const focusedZone = computed(() => (editor.editing.value ? null : (savedZones.value ?? []).find((zone) => zone.id === focusedZoneId.value) ?? null))
const focusedSummary = computed(() => (focusedZone.value ? parking.summaryBySpace.value[focusedZone.value.parkingSpaceId] ?? null : null))

function zoneOfSpace(spaceId: string) {
  return zones.value.find((zone) => zone.parkingSpaceId === spaceId) ?? null
}

/**
 * A label row was clicked. Drawn on this camera → select that zone. Otherwise: give it to the
 * selected unlabelled/relabelled zone, or start drawing a new zone with this label.
 */
function pickLabel(spaceId: string) {
  const existing = zoneOfSpace(spaceId)
  const selected = editor.editing.value ? selectedZone.value : null
  if (selected && (!existing || existing.id === selected.id)) {
    editor.assign(selected.id, spaceId)
    editor.notice.value = null
    return
  }
  if (existing) {
    if (editor.editing.value) editor.select(existing.id)
    else focusedZoneId.value = focusedZoneId.value === existing.id ? null : existing.id
    return
  }
  pendingSpaceId.value = spaceId
  if (!editor.editing.value) startEditing()
  editor.select(null)
  editor.startDrawing()
}

function selectZone(zoneId: string | null) {
  pendingSpaceId.value = null
  if (editor.editing.value) editor.select(zoneId)
  else focusedZoneId.value = zoneId
}

function finishShape() {
  const spaceId = pendingSpaceId.value && !zoneOfSpace(pendingSpaceId.value) ? pendingSpaceId.value : ''
  if (!editor.finishDrawing(spaceId)) return
  pendingSpaceId.value = null
  if (!spaceId) editor.notice.value = 'Pick a label for the new zone in the list'
}

function cancelShape() {
  editor.cancelDrawing()
  pendingSpaceId.value = null
}

/** Starts drawing a new zone, entering edit mode first when needed. */
function addZone() {
  if (!editor.editing.value) startEditing()
  pendingSpaceId.value = null
  editor.startDrawing()
}

// ---- Edit session ------------------------------------------------------------------------------

function startEditing() {
  saveError.value = null
  focusedZoneId.value = null
  editor.begin(savedZones.value)
}

function confirmDiscard() {
  return !editor.dirty.value || window.confirm('Discard unsaved parking zone changes?')
}

function cancelEditing() {
  if (!confirmDiscard()) return
  editor.end()
  pendingSpaceId.value = null
  saveError.value = null
}

async function save() {
  if (editor.drawing.value) {
    finishShape()
    if (editor.drawing.value) return
  }
  const firstProblem = editor.problems.value[0]
  if (firstProblem) {
    editor.select(firstProblem.zoneId)
    const zone = zones.value.find((item) => item.id === firstProblem.zoneId)
    editor.notice.value = `${(zone && parking.spaceLabels.value[zone.parkingSpaceId]) || 'New zone'}: ${firstProblem.message}`
    return
  }
  saving.value = true
  saveError.value = null
  try {
    parking.applyZones(cameraId.value, await parking.api.saveZones(cameraId.value, editor.payload()))
    editor.end()
  } catch (error) {
    const apiError = error instanceof ParkingApiError ? error : null
    if (apiError?.zones) {
      // Only part of the edit was saved: show what the server holds now instead of the draft, so a
      // retry starts from the server state (re-sending the old draft would duplicate new zones).
      parking.applyZones(cameraId.value, apiError.zones)
      editor.end()
    }
    saveError.value = apiError?.message ?? "Couldn't save parking zones."
  } finally {
    saving.value = false
  }
}

/** Tool group above the video; which tools show depends on the mode. */
const tools = computed(() => {
  if (!editor.editing.value) {
    return [
      { label: 'Edit zones', icon: Pencil, run: startEditing, disabled: parking.loading.value, primary: false },
      { label: 'Add zone', icon: Plus, run: addZone, disabled: parking.loading.value, primary: false },
    ]
  }
  if (editor.drawing.value) {
    return [
      { label: 'Undo point', icon: Undo2, run: editor.undoPoint, disabled: !editor.drawing.value.length, primary: false },
      { label: 'Finish shape', icon: Check, run: finishShape, disabled: editor.drawing.value.length < 3, primary: true },
      { label: 'Cancel shape', icon: X, run: cancelShape, disabled: false, primary: false },
    ]
  }
  return [
    { label: 'Add zone', icon: Plus, run: addZone, disabled: false, primary: false },
    { label: saving.value ? 'Saving…' : 'Save', icon: Check, run: save, disabled: saving.value, primary: true },
    { label: 'Cancel', icon: X, run: cancelEditing, disabled: saving.value, primary: false },
  ]
})

const hint = computed(() => {
  if (!editor.editing.value) return null
  if (editor.notice.value) return editor.notice.value
  if (editor.drawing.value) {
    const label = pendingSpaceId.value ? parking.spaceLabels.value[pendingSpaceId.value] + ': ' : ''
    return (editor.drawing.value.length ?? 0) < 3
      ? `${label}click the video to place points (${editor.drawing.value.length}/3)`
      : `${label}click the first point or press Enter to finish · Backspace undoes · Esc cancels`
  }
  return null
})

// ---- Keyboard / navigation ---------------------------------------------------------------------

function isTextInput(target: EventTarget | null) {
  return target instanceof HTMLElement && (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName))
}

function onKeydown(event: KeyboardEvent) {
  if (!editor.editing.value || isTextInput(event.target)) return
  if (editor.drawing.value) {
    if (event.key === 'Enter') {
      event.preventDefault()
      finishShape()
    } else if (event.key === 'Escape') {
      cancelShape()
    } else if (event.key === 'Backspace') {
      event.preventDefault()
      editor.undoPoint()
    }
    return
  }
  if (event.key === 'Escape') editor.select(null)
  else if ((event.key === 'Delete' || event.key === 'Backspace') && editor.selectedId.value) {
    event.preventDefault()
    editor.remove(editor.selectedId.value)
  }
}

function onBeforeUnload(event: BeforeUnloadEvent) {
  if (editor.dirty.value) event.preventDefault()
}

onBeforeRouteLeave(() => confirmDiscard())

watch(cameraId, () => {
  focusedZoneId.value = null
  pendingSpaceId.value = null
  editor.end()
  saveError.value = null
})

onMounted(() => {
  window.addEventListener('keydown', onKeydown)
  window.addEventListener('beforeunload', onBeforeUnload)
})

onUnmounted(() => {
  window.removeEventListener('keydown', onKeydown)
  window.removeEventListener('beforeunload', onBeforeUnload)
})
</script>

<template>
  <main class="text-foreground">
    <div class="sticky top-(--app-nav-height) z-30 border-b border-border/60 bg-background/75 backdrop-blur-xl">
      <div class="mx-auto flex max-w-[1500px] items-center gap-3 px-5 py-3 sm:px-8 lg:px-10">
        <Button as-child variant="ghost" size="icon-sm" aria-label="Back to Parking">
          <NuxtLink to="/parking"><ArrowLeft /></NuxtLink>
        </Button>
        <SquareParking class="size-4 shrink-0 text-muted-foreground" />
        <h1 class="min-w-0 truncate text-base font-semibold tracking-tight">
          <Skeleton v-if="camerasLoading && !camera" class="h-5 w-40" />
          <template v-else>{{ camera?.name ?? cameraId }} <span class="font-normal text-muted-foreground">Parking zones</span></template>
        </h1>
        <Badge v-if="camera" variant="secondary" class="tabular-nums">{{ floorLabel(camera.floor) }}</Badge>
        <Badge v-if="parking.api.kind === 'mock'" variant="outline" class="ml-auto text-muted-foreground">Mock data</Badge>
      </div>
    </div>

    <Empty v-if="!camerasLoading && !camera" class="mx-auto mt-16 max-w-xl border border-border bg-card/40 py-16">
      <EmptyHeader>
        <EmptyMedia variant="icon" class="size-12 rounded-xl">
          <SquareParking class="size-6" />
        </EmptyMedia>
        <EmptyTitle class="text-base">Camera not found</EmptyTitle>
        <EmptyDescription>{{ cameraId }} isn't in the camera list.</EmptyDescription>
      </EmptyHeader>
      <EmptyContent>
        <Button as-child variant="outline"><NuxtLink to="/parking">Back to Parking</NuxtLink></Button>
      </EmptyContent>
    </Empty>

    <div v-else class="mx-auto grid max-w-[1500px] gap-6 px-5 pt-6 pb-16 sm:px-8 lg:grid-cols-[minmax(0,1fr)_22rem] lg:px-10">
      <div class="min-w-0">
        <div ref="viewerElement" :class="['flex flex-col', isFullscreen ? 'h-screen w-screen bg-black' : '']">
          <!-- Icon tool group above the video (never over it); it goes fullscreen with the video. -->
          <div :class="['flex items-center gap-2', isFullscreen ? 'p-3' : 'pb-3']">
            <div class="glass-panel flex items-center gap-1 rounded-xl p-1" role="toolbar" aria-label="Zone tools">
              <Tooltip v-for="tool in tools" :key="tool.label">
                <TooltipTrigger as-child>
                  <Button
                    type="button"
                    size="icon-sm"
                    :variant="tool.primary ? 'default' : 'ghost'"
                    class="size-8 rounded-lg"
                    :aria-label="tool.label"
                    :disabled="tool.disabled"
                    @click="tool.run"
                  >
                    <component :is="tool.icon" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>{{ tool.label }}</TooltipContent>
              </Tooltip>
            </div>

            <p v-if="hint" :class="['min-w-0 truncate px-1 text-xs', editor.notice.value ? 'text-amber-300' : 'text-muted-foreground']" role="status">
              {{ hint }}
            </p>

            <div class="glass-panel ml-auto flex items-center rounded-xl p-1">
              <Tooltip>
                <TooltipTrigger as-child>
                  <Button
                    type="button"
                    size="icon-sm"
                    variant="ghost"
                    class="size-8 rounded-lg"
                    :aria-label="isFullscreen ? 'Exit fullscreen' : 'Fullscreen'"
                    @click="toggleFullscreen"
                  >
                    <Minimize2 v-if="isFullscreen" />
                    <Maximize2 v-else />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>{{ isFullscreen ? 'Exit fullscreen' : 'Fullscreen' }}</TooltipContent>
              </Tooltip>
            </div>
          </div>

          <div ref="stageElement" :class="['relative overflow-hidden bg-black', isFullscreen ? 'min-h-0 flex-1' : 'aspect-video rounded-xl ring-1 ring-white/[0.08]']">
            <CameraVideo
              v-if="camera"
              :camera="camera"
              :realtime-client="realtimeClient"
              :connection="connection"
              :camera-state="liveStatus ? String(liveStatus.state) : undefined"
              :active="true"
              @dimensions="mediaSize = $event"
            />
            <ParkingOverlay
              class="absolute z-10"
              :style="contentStyle"
              :width="content.width"
              :height="content.height"
              :zones="zones"
              :space-labels="parking.spaceLabels.value"
              :statuses="parking.zoneStatuses.value"
              :conflict-space-ids="conflictSpaceIds"
              :editing="editor.editing.value"
              :selected-id="selectedZoneId"
              :drawing="editor.drawing.value"
              :invalid-ids="invalidIds"
              @select="selectZone"
              @add-point="editor.addPoint"
              @close-drawing="finishShape"
              @move-vertex="editor.moveVertex"
              @insert-vertex="editor.insertVertex"
              @remove-vertex="editor.removeVertex"
            />
            <!-- Mode tag only: no pointer events, so clicks near the corner still reach the video. -->
            <Badge
              v-if="editor.editing.value"
              class="pointer-events-none absolute top-3 left-3 z-20 border border-primary/40 bg-black/60 text-primary backdrop-blur-md"
            >
              {{ editor.drawing.value ? 'Drawing' : 'Editing' }}
            </Badge>
          </div>
        </div>

        <p class="mt-3 text-xs text-muted-foreground">
          <template v-if="editor.editing.value">
            Pick a label on the right, then click the video to draw its zone. Select a zone to drag its points; drag an edge midpoint to add a point, double-click a point to delete it.
          </template>
          <template v-else>
            Click a label to see its status or register its empty baseline. {{ parking.api.kind === 'mock' ? 'Mock status changes every 30 s.' : 'Status updates live.' }}
          </template>
        </p>
      </div>

      <!-- Plain column (no card), like a tool sidebar. -->
      <aside class="flex min-w-0 flex-col gap-3 self-start lg:sticky lg:top-[calc(var(--app-nav-height)+5rem)] lg:max-h-[calc(100dvh-var(--app-nav-height)-7rem)]">
        <div class="space-y-1.5 px-1">
          <h2 class="text-sm font-semibold tracking-tight">Labels</h2>
          <ParkingSummary :counts="counts" compact />
        </div>

        <div
          v-if="pageError"
          class="flex items-center gap-2 rounded-lg bg-destructive/10 py-1.5 pr-1.5 pl-3 text-xs text-red-300"
          role="alert"
        >
          <CircleAlert class="size-3.5 shrink-0" />
          <span class="min-w-0 flex-1">{{ pageError }}</span>
          <Button size="sm" variant="ghost" class="h-6 shrink-0 px-2 text-xs text-red-200 hover:bg-red-400/10 hover:text-red-100" @click="retry">
            Retry
          </Button>
        </div>

        <div v-if="parking.loading.value" class="space-y-1.5">
          <Skeleton v-for="item in 4" :key="item" class="h-11 w-full rounded-xl" />
        </div>
        <ParkingLabelList
          v-else
          class="min-h-0 flex-1"
          :spaces="parking.spaces.value"
          :zones="zones"
          :statuses="parking.zoneStatuses.value"
          :scores="zoneScores"
          :active-space-id="activeSpaceId"
          :selected-zone-id="selectedZoneId"
          :editing="editor.editing.value"
          :conflict-space-ids="conflictSpaceIds"
          :create-space="parking.createSpace"
          @pick="pickLabel"
          @select-zone="selectZone"
          @remove-zone="editor.remove"
        />

        <template v-if="focusedZone">
          <ParkingCalibrationPanel
            :label="parking.spaceLabels.value[focusedZone.parkingSpaceId] ?? '?'"
            :status="parking.zoneStatuses.value[focusedZone.id] ?? 'unknown'"
            :score="zoneScores[focusedZone.id] ?? null"
            :calibrated="parking.calibrated.value[focusedZone.id] ?? null"
            :calibrate="(register) => parking.calibrate(focusedZone!.id, register)"
            @close="focusedZoneId = null"
          />
          <ParkingSpaceBoard
            v-if="focusedSummary && focusedSummary.zones.length > 1"
            :summaries="[focusedSummary]"
            :camera-names="cameraNames"
            :current-camera-id="cameraId"
          />
        </template>
      </aside>
    </div>
  </main>
</template>
