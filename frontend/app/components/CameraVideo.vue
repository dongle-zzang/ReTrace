<script setup lang="ts">
import type { Camera } from '~/types/camera'
import type { ConnectionState, RealtimePreviewClient, TransportState } from '~/composables/realtimePreviewClient'
import { JpegFrameView } from '~/composables/realtimePreviewClient'
import { LoaderCircle, Video, VideoOff } from '@lucide/vue'

const props = defineProps<{
  camera: Camera
  active: boolean
  realtimeClient: RealtimePreviewClient | null
  connection: ConnectionState
  cameraState?: string
}>()

const emit = defineEmits<{
  dimensions: [size: { width: number; height: number }]
  transport: [state: TransportState]
}>()

const imageElement = ref<HTMLImageElement | null>(null)
const hasFrame = ref(false)
let view: JpegFrameView | null = null
let stopWatch: (() => void) | undefined

const cameraId = computed(() => props.camera.camera_id)
const shouldWatch = computed(() => props.camera.enabled && props.active && !!props.realtimeClient)
const serverConnected = computed(() => props.connection === 'open')
const cameraUnavailable = computed(() => !!props.cameraState && !['online', 'degraded'].includes(props.cameraState))

const state = computed<TransportState>(() => {
  if (!shouldWatch.value) return 'waiting'
  if (!serverConnected.value) return props.connection === 'reconnecting' ? 'reconnecting' : 'connecting'
  return hasFrame.value ? 'connected' : 'connecting'
})

watch(state, (next) => emit('transport', next), { immediate: true })

function stopFrames() {
  stopWatch?.()
  stopWatch = undefined
  view?.clear()
  hasFrame.value = false
}

function startFrames() {
  stopFrames()
  const client = props.realtimeClient
  if (!shouldWatch.value || !client || !view) return
  stopWatch = client.watch(cameraId.value, (frame) => view?.push(frame.jpeg))
}

let lastSize = ''
function onFrameShown(size: { width: number; height: number }) {
  hasFrame.value = true
  const key = size.width + 'x' + size.height
  if (!size.width || !size.height || key === lastSize) return
  lastSize = key
  emit('dimensions', size)
}

// Watch primitives only: the camera object is replaced on every 10-second API refresh and
// must not restart the stream.
watch([cameraId, shouldWatch, () => props.realtimeClient], startFrames, { flush: 'post' })

// A dropped connection leaves no fresh frames; don't keep showing a frozen image.
watch(serverConnected, (connected) => {
  if (!connected) {
    view?.clear()
    hasFrame.value = false
  }
})

onMounted(() => {
  if (imageElement.value) view = new JpegFrameView(imageElement.value, onFrameShown)
  startFrames()
})

onUnmounted(() => {
  stopFrames()
  view = null
})
</script>

<template>
  <div class="absolute inset-0">
    <img
      ref="imageElement"
      class="h-full w-full object-contain transition-[opacity,filter] duration-700 ease-out"
      :class="state === 'connected' ? 'opacity-100 blur-0' : 'opacity-0 blur-sm'"
      :alt="camera.name + ' 실시간 미리보기'"
    >
    <Transition
      enter-active-class="transition-opacity duration-300"
      enter-from-class="opacity-0"
      leave-active-class="transition-opacity duration-500"
      leave-to-class="opacity-0"
    >
      <div
        v-if="state !== 'connected'"
        class="absolute inset-0 flex h-full flex-col items-center justify-center gap-3 bg-[radial-gradient(70%_70%_at_50%_45%,rgba(255,255,255,0.05),transparent)] px-5 text-center text-muted-foreground"
      >
        <div class="flex size-11 items-center justify-center rounded-full bg-white/5 ring-1 ring-white/10">
          <VideoOff v-if="!camera.enabled || (state === 'connecting' && serverConnected && cameraUnavailable)" class="size-5" />
          <LoaderCircle v-else-if="state === 'connecting' || state === 'reconnecting'" class="size-5 animate-spin text-primary" />
          <Video v-else class="size-5" />
        </div>
        <p v-if="!camera.enabled" class="text-sm">비활성 카메라</p>
        <p v-else-if="state === 'reconnecting' || state === 'connecting' && !serverConnected" class="text-sm">서버 연결 중…</p>
        <p v-else-if="state === 'connecting' && cameraUnavailable" class="text-sm">카메라 영상이 없습니다 ({{ cameraState }})</p>
        <p v-else-if="state === 'connecting'" class="text-sm">영상 수신 대기 중…</p>
        <p v-else class="text-sm">화면에 보이면 영상을 표시합니다</p>
      </div>
    </Transition>
  </div>
</template>
