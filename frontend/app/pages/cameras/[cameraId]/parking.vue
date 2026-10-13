<script setup lang="ts">
import { ArrowLeft, Check, CircleAlert, Maximize2, Minimize2, PanelRight, Pencil, Plus, SquareParking, Undo2, X, Trash2 } from '@lucide/vue'
import CameraVideo from '~/components/CameraVideo.vue'
import ParkingCalibrationPanel from '~/components/ParkingCalibrationPanel.vue'
import ParkingZoneInspector from '~/components/ParkingZoneInspector.vue'
import ParkingStatusBadge from '~/components/ParkingStatusBadge.vue'
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
import { compareLabels, countStatuses } from '~/lib/parking'

// Let the layout reserve the actual navigation and footer heights.
definePageMeta({ viewportWorkspace: true })

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

const focusedZoneId = ref<string | null>(null)
const inspector = ref<InstanceType<typeof ParkingZoneInspector> | null>(null)
const zonesOpen = ref(false)
const zonesButton = ref<HTMLButtonElement | null>(null)
const labelButton = ref<HTMLButtonElement | null>(null)
const selectedZoneId = computed(() => (editor.editing.value ? editor.selectedId.value : focusedZoneId.value))
const selectedZone = computed(() => zones.value.find((zone) => zone.id === selectedZoneId.value) ?? null)
const focusedZone = computed(() => editor.editing.value ? null : selectedZone.value)
const focusedSummary = computed(() => focusedZone.value ? parking.summaryBySpace.value[focusedZone.value.parkingSpaceId] ?? null : null)
const zoneRows = computed(() => [...zones.value].sort((a, b) => compareLabels(parking.spaceLabels.value[a.parkingSpaceId] ?? '', parking.spaceLabels.value[b.parkingSpaceId] ?? '')))

function focusLabel() {
  void nextTick(() => inspector.value?.focus())
}

function closeZones() {
  zonesOpen.value = false
  zonesButton.value?.focus()
}

function selectZone(zoneId: string | null) {
  if (saving.value) return
  if (editor.editing.value) editor.select(zoneId)
  else focusedZoneId.value = zoneId
}

function assignLabel(zoneId: string, spaceId: string) {
  if (saving.value || !editor.editing.value || editor.selectedId.value !== zoneId) return
  if (zones.value.some((zone) => zone.id !== zoneId && zone.parkingSpaceId === spaceId)) return
  editor.assign(zoneId, spaceId)
  editor.notice.value = null
}

function finishShape() {
  if (saving.value || !editor.finishDrawing()) return
  focusLabel()
}

function cancelShape() {
  editor.cancelDrawing()
}

function removeZone(zoneId: string) {
  if (!saving.value) editor.remove(zoneId)
}

function addZone() {
  if (saving.value || editor.drawing.value) return
  if (!editor.editing.value) startEditing()
  const unlabeled = zones.value.find((zone) => !zone.parkingSpaceId)
  if (unlabeled) {
    editor.select(unlabeled.id)
    editor.notice.value = 'Choose a label or delete this zone before adding another.'
    focusLabel()
    return
  }
  zonesOpen.value = false
  editor.startDrawing()
}

// ---- Edit session ------------------------------------------------------------------------------

function startEditing() {
  saveError.value = null
  const previousSelection = focusedZoneId.value
  focusedZoneId.value = null
  editor.begin(savedZones.value)
  editor.select(previousSelection)
}

function confirmDiscard() {
  return !editor.dirty.value || window.confirm('Discard unsaved parking zone changes?')
}

function cancelEditing() {
  if (!confirmDiscard()) return
  editor.end()
  saveError.value = null
}

async function save() {
  if (saving.value) return
  if (editor.drawing.value) {
    finishShape()
    if (editor.drawing.value) return
  }
  const firstProblem = editor.problems.value[0]
  if (firstProblem) {
    editor.select(firstProblem.zoneId)
    const zone = zones.value.find((item) => item.id === firstProblem.zoneId)
    editor.notice.value = `${(zone && parking.spaceLabels.value[zone.parkingSpaceId]) || 'New zone'}: ${firstProblem.message}`
    if (zone && !zone.parkingSpaceId) focusLabel()
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

const hint = computed(() => {
  if (editor.notice.value) return editor.notice.value
  if (editor.drawing.value) {
    return editor.drawing.value.length < 3
      ? `Click the video to place points (${editor.drawing.value.length}/3)`
      : 'Click the first point or press Enter to finish · Backspace undoes · Esc cancels'
  }
  if (editor.editing.value) return 'Select a zone to adjust its points. Save changes when done.'
  return 'Select a zone to see its status and empty baseline, or add a new zone.'
})

// ---- Keyboard / navigation ---------------------------------------------------------------------

function isTextInput(target: EventTarget | null) {
  return target instanceof HTMLElement && (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName))
}

function onKeydown(event: KeyboardEvent) {
  if (event.defaultPrevented || (event.target instanceof HTMLElement && event.target.closest('[role="dialog"]')) || saving.value || !editor.editing.value || isTextInput(event.target)) return
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
    removeZone(editor.selectedId.value)
  }
}

function onBeforeUnload(event: BeforeUnloadEvent) {
  if (editor.dirty.value) event.preventDefault()
}

onBeforeRouteLeave(() => confirmDiscard())

watch(cameraId, () => {
  focusedZoneId.value = null
  zonesOpen.value = false
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
  <main class="flex min-h-0 flex-1 flex-col text-foreground">
    <header class="flex min-h-16 shrink-0 items-center gap-3 px-4 py-3 sm:px-6">
      <Button as-child variant="ghost" size="icon-sm" aria-label="Back to Parking"><NuxtLink to="/parking"><ArrowLeft /></NuxtLink></Button>
      <SquareParking class="hidden size-4 shrink-0 text-muted-foreground sm:block" />
      <h1 class="min-w-0 flex-1 truncate text-sm font-semibold tracking-tight sm:text-base">
        <Skeleton v-if="camerasLoading && !camera" class="h-5 w-40" />
        <template v-else>{{ camera?.name ?? cameraId }} <span class="font-normal text-muted-foreground">Parking zones</span></template>
      </h1>
      <Badge v-if="camera" variant="secondary" class="shrink-0 tabular-nums">{{ floorLabel(camera.floor) }}</Badge>
      <Badge v-if="parking.api.kind === 'mock'" variant="outline" class="shrink-0 text-muted-foreground">Mock data</Badge>
    </header>
    <Empty v-if="!camerasLoading && !camera" class="mx-auto mt-16 max-w-xl border border-border bg-card/40 py-16">
      <EmptyHeader>
        <EmptyMedia variant="icon" class="size-12 rounded-xl"><SquareParking class="size-6" /></EmptyMedia>
        <EmptyTitle class="text-base">Camera not found</EmptyTitle>
        <EmptyDescription>{{ cameraId }} isn't in the camera list.</EmptyDescription>
      </EmptyHeader>
      <EmptyContent><Button as-child variant="outline"><NuxtLink to="/parking">Back to Parking</NuxtLink></Button></EmptyContent>
    </Empty>
    <div v-else class="flex min-h-0 flex-1 flex-col px-3 pb-4 sm:px-6">
      <div ref="viewerElement" :class="['flex min-h-0 flex-1 flex-col gap-2', isFullscreen ? 'h-dvh w-full bg-background p-3' : '']" aria-label="Parking zone workspace">
        <div class="flex shrink-0 flex-wrap items-center gap-2" role="toolbar" aria-label="Zone tools">
          <template v-if="editor.drawing.value">
            <Button size="sm" variant="outline" :disabled="!editor.drawing.value.length" @click="editor.undoPoint"><Undo2 /> Undo point</Button>
            <Button size="sm" :disabled="editor.drawing.value.length < 3" @click="finishShape"><Check /> Finish shape</Button>
            <Button size="sm" variant="ghost" @click="cancelShape"><X /> Cancel shape</Button>
          </template>
          <template v-else>
            <Button v-if="!editor.editing.value" size="sm" variant="outline" :disabled="parking.loading.value" @click="startEditing"><Pencil /> Edit zones</Button>
            <Button size="sm" :variant="editor.editing.value ? 'outline' : 'default'" :disabled="saving || parking.loading.value" @click="addZone"><Plus /> Add zone</Button>
            <template v-if="editor.editing.value">
              <Button size="sm" :disabled="saving" @click="save"><Check /> {{ saving ? 'Saving…' : 'Save changes' }}</Button>
              <Button size="sm" variant="ghost" :disabled="saving" @click="cancelEditing"><X /> Cancel</Button>
            </template>
          </template>
          <template v-if="selectedZone && !editor.drawing.value">
            <span class="max-w-28 truncate px-1 text-xs font-medium text-muted-foreground">{{ parking.spaceLabels.value[selectedZone.parkingSpaceId] ?? 'New zone' }}</span>
            <template v-if="editor.editing.value">
              <Button as-child size="sm" variant="ghost" :disabled="saving">
                <button ref="labelButton" type="button" @click="focusLabel"><Pencil /> {{ selectedZone.parkingSpaceId ? 'Edit label' : 'Set label' }}</button>
              </Button>
              <Button size="icon-sm" variant="ghost" :disabled="saving" aria-label="Delete selected zone" @click="removeZone(selectedZone.id)"><Trash2 /></Button>
            </template>
          </template>
          <div class="ml-auto flex items-center gap-2">
            <Button as-child size="sm" :variant="zonesOpen ? 'secondary' : 'ghost'" :disabled="!!editor.drawing.value">
              <button ref="zonesButton" type="button" :aria-expanded="zonesOpen" aria-controls="camera-zone-panel" @click="zonesOpen = !zonesOpen"><PanelRight /> Zones {{ zones.length }}</button>
            </Button>
            <Badge v-if="editor.editing.value" variant="outline" class="hidden border-primary/40 text-primary sm:inline-flex">{{ editor.drawing.value ? 'Drawing' : 'Editing' }}</Badge>
            <Tooltip>
              <TooltipTrigger as-child>
                <Button type="button" size="icon-sm" variant="ghost" :aria-label="isFullscreen ? 'Exit fullscreen' : 'Fullscreen'" @click="toggleFullscreen"><Minimize2 v-if="isFullscreen" /><Maximize2 v-else /></Button>
              </TooltipTrigger>
              <TooltipContent>{{ isFullscreen ? 'Exit fullscreen' : 'Fullscreen' }}</TooltipContent>
            </Tooltip>
          </div>
        </div>
        <p :class="['shrink-0 px-1 text-xs', editor.notice.value ? 'text-amber-300' : 'text-muted-foreground']" role="status">{{ hint }}</p>
        <div v-if="pageError" class="flex shrink-0 items-center gap-2 rounded-lg bg-destructive/10 px-3 py-2 text-xs text-red-300" role="alert">
          <CircleAlert class="size-3.5 shrink-0" />
          <span class="min-w-0 flex-1">{{ pageError }}</span>
          <Button size="sm" variant="ghost" class="h-6 shrink-0 px-2 text-xs" :disabled="saving" @click="retry">Retry</Button>
        </div>
        <div class="flex min-h-0 flex-1 gap-3 overflow-hidden">
          <div
            ref="stageElement"
            :class="['relative min-h-0 min-w-0 flex-1 overflow-hidden rounded-xl bg-black ring-1 ring-white/[0.08]', zonesOpen ? 'hidden sm:block' : '']"
          >
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
              :editing="editor.editing.value && !saving"
              :interactive="!saving"
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
          </div>
          <Transition name="zone-panel">
            <aside
              v-show="zonesOpen"
              id="camera-zone-panel"
              :inert="!zonesOpen"
              class="flex w-full min-w-0 shrink-0 flex-col gap-4 overflow-y-auto rounded-xl border border-white/10 bg-background/75 p-4 backdrop-blur-xl sm:w-80 lg:w-88"
              aria-label="Parking zone list"
              @keydown.esc.stop="closeZones"
            >
              <div class="flex items-center justify-between gap-3">
                <h2 class="text-sm font-semibold">Zones <span class="ml-1 text-muted-foreground">{{ zones.length }}</span></h2>
                <Button size="icon-sm" variant="ghost" aria-label="Close zone list" @click="closeZones"><X /></Button>
              </div>
              <ParkingSummary :counts="counts" compact />
              <!-- Keep this mounted while the panel is hidden; its modal portals to the workspace. -->
              <ParkingZoneInspector
                v-if="editor.editing.value && selectedZone && !editor.drawing.value"
                :key="selectedZone.id"
                ref="inspector"
                :zone="selectedZone"
                :zones="zones"
                :spaces="parking.spaces.value"
                :disabled="saving"
                :create-space="parking.createSpace"
                :portal-target="viewerElement"
                :return-focus-target="labelButton ?? zonesButton"
                @assign="assignLabel"
                @remove="removeZone"
                @add-another="addZone"
              />
              <p v-if="parking.loading.value" class="text-xs text-muted-foreground">Loading zones…</p>
              <p v-else-if="!zones.length" class="text-xs text-muted-foreground">No zones yet. Add a zone on the video to get started.</p>
              <ul v-else class="space-y-2" aria-label="Zones on this camera">
                <li
                  v-for="zone in zoneRows"
                  :key="zone.id"
                  class="flex min-w-0 items-center gap-1 rounded-lg border"
                  :class="zone.id === selectedZoneId ? 'border-primary/50 bg-primary/10' : 'border-white/10 bg-white/[0.03]'"
                >
                  <button
                    type="button"
                    class="flex min-w-0 flex-1 flex-wrap items-center justify-between gap-2 p-2.5 text-left text-sm"
                    :disabled="!!editor.drawing.value || saving"
                    :aria-pressed="zone.id === selectedZoneId"
                    @click="selectZone(zone.id)"
                  >
                    <span class="min-w-0 truncate font-medium" :class="!zone.parkingSpaceId ? 'text-amber-200' : ''">{{ parking.spaceLabels.value[zone.parkingSpaceId] ?? 'Choose a label' }}</span>
                    <ParkingStatusBadge :status="parking.zoneStatuses.value[zone.id] ?? 'unknown'" :score="zoneScores[zone.id] ?? null" />
                    <span v-if="conflictSpaceIds.includes(zone.parkingSpaceId)" class="text-xs text-amber-300">Check</span>
                  </button>
                  <Button
                    v-if="editor.editing.value"
                    size="icon-sm"
                    variant="ghost"
                    class="mr-1 shrink-0 text-muted-foreground hover:text-destructive"
                    :disabled="saving || !!editor.drawing.value"
                    :aria-label="'Delete zone ' + (parking.spaceLabels.value[zone.parkingSpaceId] ?? 'without label')"
                    @click="removeZone(zone.id)"
                  ><Trash2 /></Button>
                </li>
              </ul>
              <template v-if="focusedZone">
                <ParkingCalibrationPanel
                  :key="focusedZone.id"
                  :label="parking.spaceLabels.value[focusedZone.parkingSpaceId] ?? '?'"
                  :status="parking.zoneStatuses.value[focusedZone.id] ?? 'unknown'"
                  :score="zoneScores[focusedZone.id] ?? null"
                  :calibrated="parking.calibrated.value[focusedZone.id] ?? null"
                  :calibrate="(register) => parking.calibrate(focusedZone!.id, register)"
                  @close="focusedZoneId = null"
                />
                <ParkingSpaceBoard v-if="focusedSummary && focusedSummary.zones.length > 1" :summaries="[focusedSummary]" :camera-names="cameraNames" />
              </template>
            </aside>
          </Transition>
        </div>
      </div>
    </div>
  </main>
</template>

<style scoped>
.zone-panel-enter-active,
.zone-panel-leave-active {
  transition: opacity 180ms ease, transform 180ms ease;
}
.zone-panel-enter-from,
.zone-panel-leave-to {
  opacity: 0;
  transform: translateX(16px);
}
</style>
