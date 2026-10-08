import type { RealtimeCameraStatus, RealtimeMessage } from '~/types/realtime'
import type { ConnectionState } from '~/composables/realtimePreviewClient'
import { RealtimePreviewClient } from '~/composables/realtimePreviewClient'

const WS_PATH = '/ws'
// camera_status arrives ~10 Hz per camera; apply it in batches so the grid doesn't re-render per message.
const STATUS_FLUSH_MS = 500

function clampFps(value: unknown) {
  const fps = Math.round(Number(value))
  return Number.isFinite(fps) ? Math.min(30, Math.max(1, fps)) : 10
}

/**
 * Owns the page's single /ws connection. Created only after mount (client side) and closed on
 * unmount or page exit without reconnecting. Person boxes and track IDs are already drawn into
 * the JPEG frames by the server, so detections are not re-rendered here.
 */
export function useRealtimePreview(videoFps: unknown) {
  const fps = clampFps(videoFps)
  const client = shallowRef<RealtimePreviewClient | null>(null)
  const connection = ref<ConnectionState>('connecting')
  const statuses = shallowRef<Record<string, RealtimeCameraStatus>>({})
  const pendingStatuses = new Map<string, RealtimeCameraStatus>()
  const messageListeners = new Set<(message: RealtimeMessage) => void>()
  let flushTimer: ReturnType<typeof setTimeout> | null = null

  /** Other features (parking occupancy) read text messages from this same socket. */
  function onMessage(listener: (message: RealtimeMessage) => void) {
    messageListeners.add(listener)
    return () => void messageListeners.delete(listener)
  }

  function flushStatuses() {
    flushTimer = null
    if (!pendingStatuses.size) return
    statuses.value = { ...statuses.value, ...Object.fromEntries(pendingStatuses) }
    pendingStatuses.clear()
  }

  function resetStatuses() {
    if (flushTimer) clearTimeout(flushTimer)
    flushTimer = null
    pendingStatuses.clear()
    statuses.value = {}
  }

  function handleMessage(message: RealtimeMessage) {
    for (const listener of messageListeners) listener(message)
    if (message.type !== 'camera_status') return
    if (typeof message.cameraId !== 'string' || !message.status || typeof message.status !== 'object') return
    pendingStatuses.set(message.cameraId, message.status)
    flushTimer ??= setTimeout(flushStatuses, STATUS_FLUSH_MS)
  }

  function handleConnection(state: ConnectionState) {
    connection.value = state
    if (state !== 'open') resetStatuses()
  }

  function open() {
    client.value?.close()
    client.value = new RealtimePreviewClient(WS_PATH, fps, handleMessage, handleConnection)
  }

  function close() {
    client.value?.close()
    client.value = null
  }

  function onPageShow(event: PageTransitionEvent) {
    // Restored from the back/forward cache after pagehide closed the socket.
    if (event.persisted && !client.value) open()
  }

  onMounted(() => {
    open()
    window.addEventListener('pagehide', close)
    window.addEventListener('pageshow', onPageShow)
  })
  onUnmounted(() => {
    window.removeEventListener('pagehide', close)
    window.removeEventListener('pageshow', onPageShow)
    close()
  })

  return { client, connection, statuses, videoFps: fps, onMessage }
}
