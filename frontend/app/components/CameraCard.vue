<script setup lang="ts">
import type { Camera } from '~/types/camera'
import type { ConnectionState, RealtimePreviewClient } from '~/composables/realtimePreviewClient'
import type { RealtimeCameraStatus } from '~/types/realtime'
import type { CameraOverlayData, OverlaySelection } from '~/types/overlay'
import { emptyCameraOverlay } from '~/types/overlay'
import CameraVideo from '~/components/CameraVideo.vue'
import CameraOverlay from '~/components/CameraOverlay.vue'
import { Maximize2, Minimize2 } from '@lucide/vue'
import { Button } from '~/components/ui/button'
import { Card, CardHeader, CardTitle } from '~/components/ui/card'
import { Tooltip, TooltipContent, TooltipTrigger } from '~/components/ui/tooltip'

const props = defineProps<{
  camera: Camera
  realtimeClient: RealtimePreviewClient | null
  connection: ConnectionState
  liveStatus?: RealtimeCameraStatus
  annotations?: CameraOverlayData
}>()

const stageElement = ref<HTMLElement | null>(null)
const isVisible = ref(false)
const isFullscreen = ref(false)
const stageSize = ref({ width: 0, height: 0 })
const mediaSize = ref({ width: 1280, height: 720 })
const selection = ref<OverlaySelection>(null)
const emptyAnnotations = emptyCameraOverlay()
const displayAnnotations = computed(() => props.annotations ?? emptyAnnotations)
let observer: IntersectionObserver | undefined
let resizeObserver: ResizeObserver | undefined

const overlayStyle = computed(() => {
  const { width: stageWidth, height: stageHeight } = stageSize.value
  const { width: mediaWidth, height: mediaHeight } = mediaSize.value
  if (!stageWidth || !stageHeight || !mediaWidth || !mediaHeight) {
    return { inset: '0' }
  }

  const scale = Math.min(stageWidth / mediaWidth, stageHeight / mediaHeight)
  const width = mediaWidth * scale
  const height = mediaHeight * scale
  return {
    left: (stageWidth - width) / 2 + 'px',
    top: (stageHeight - height) / 2 + 'px',
    width: width + 'px',
    height: height + 'px',
  }
})

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
  if (!props.camera.enabled) return '비활성'
  if (status.value.stale) return '상태 지연'
  if (status.value.state === 'online') return '온라인'
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

function measureStage() {
  const stage = stageElement.value
  if (stage) stageSize.value = { width: stage.clientWidth, height: stage.clientHeight }
}

function onFullscreenChange() {
  isFullscreen.value = document.fullscreenElement === stageElement.value
  measureStage()
}

async function toggleFullscreen() {
  const stage = stageElement.value
  if (!stage) return
  try {
    if (document.fullscreenElement === stage) await document.exitFullscreen()
    else await stage.requestFullscreen()
  } catch {
    // Keep the card usable when the browser does not allow fullscreen.
  }
}

watch(() => props.camera.camera_id, () => {
  selection.value = null
})

onMounted(() => {
  measureStage()
  if (stageElement.value) {
    if (typeof IntersectionObserver !== 'undefined') {
      observer = new IntersectionObserver(([entry]) => {
        isVisible.value = entry?.isIntersecting ?? false
      }, { rootMargin: '120px 0px' })
      observer.observe(stageElement.value)
    } else {
      isVisible.value = true
    }

    if (typeof ResizeObserver !== 'undefined') {
      resizeObserver = new ResizeObserver(measureStage)
      resizeObserver.observe(stageElement.value)
    }
  }
  window.addEventListener('resize', measureStage)
  document.addEventListener('fullscreenchange', onFullscreenChange)
})

onUnmounted(() => {
  observer?.disconnect()
  resizeObserver?.disconnect()
  window.removeEventListener('resize', measureStage)
  document.removeEventListener('fullscreenchange', onFullscreenChange)
})
</script>

<template>
  <Card class="group gap-0 py-0 ring-white/[0.08]">
    <div
      ref="stageElement"
      class="relative aspect-video overflow-hidden bg-black"
      :style="isFullscreen ? { aspectRatio: 'auto', width: '100vw', height: '100vh' } : undefined"
    >
      <CameraVideo
        :camera="camera"
        :realtime-client="realtimeClient"
        :connection="connection"
        :camera-state="liveStatus ? String(liveStatus.state) : undefined"
        :active="isVisible || isFullscreen"
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
      <div class="pointer-events-none absolute inset-x-0 top-0 z-20 h-16 bg-gradient-to-b from-black/50 to-transparent" />
      <Tooltip>
        <TooltipTrigger as-child>
          <Button
            type="button"
            size="icon-sm"
            variant="ghost"
            class="absolute top-3 right-3 z-20 border border-white/15 bg-black/45 text-white opacity-80 backdrop-blur-md transition-opacity group-hover:opacity-100 hover:bg-black/70 hover:text-white"
            :aria-label="isFullscreen ? '전체 화면 종료' : '영상 전체 화면'"
            @click="toggleFullscreen"
          >
            <Minimize2 v-if="isFullscreen" />
            <Maximize2 v-else />
          </Button>
        </TooltipTrigger>
        <TooltipContent>{{ isFullscreen ? '전체 화면 종료' : '전체 화면' }}</TooltipContent>
      </Tooltip>
    </div>

    <CardHeader class="px-5 py-4">
      <div class="flex items-center justify-between gap-3">
        <CardTitle class="min-w-0 truncate text-[15px] font-semibold tracking-tight">{{ camera.name }}</CardTitle>
        <Tooltip :disabled="!status.last_error">
          <TooltipTrigger as-child>
            <div
              tabindex="0"
              :aria-label="'상태: ' + statusLabel + (status.last_error ? ', 최근 오류 있음' : '')"
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
    </CardHeader>

  </Card>
</template>
