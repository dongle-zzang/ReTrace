import type { Ref } from 'vue'
import type { ObjectFit, Size } from '~/lib/videoGeometry'
import { contentRect } from '~/lib/videoGeometry'

/**
 * Tracks a video stage element: its size, fullscreen state and where the video content is drawn
 * inside it. `fit` must match the media element's CSS object-fit (CameraVideo uses contain).
 * `fullscreenElement` (default: the stage) is what goes fullscreen, e.g. a wrapper with a toolbar.
 */
export function useVideoStage(
  stageElement: Ref<HTMLElement | null>,
  mediaSize: Ref<Size>,
  fit: ObjectFit = 'contain',
  fullscreenElement: Ref<HTMLElement | null> = stageElement,
) {
  const stageSize = ref<Size>({ width: 0, height: 0 })
  const isFullscreen = ref(false)
  let resizeObserver: ResizeObserver | undefined

  /** Content rect in stage pixels; overlays positioned with it line up with the video. */
  const content = computed(() => contentRect(stageSize.value, mediaSize.value, fit))
  const contentStyle = computed(() => {
    if (!stageSize.value.width || !stageSize.value.height) return { inset: '0' }
    const { left, top, width, height } = content.value
    return { left: left + 'px', top: top + 'px', width: width + 'px', height: height + 'px' }
  })

  function measure() {
    const stage = stageElement.value
    if (stage) stageSize.value = { width: stage.clientWidth, height: stage.clientHeight }
  }

  function onFullscreenChange() {
    isFullscreen.value = !!fullscreenElement.value && document.fullscreenElement === fullscreenElement.value
    measure()
  }

  async function toggleFullscreen() {
    const target = fullscreenElement.value
    if (!target) return
    try {
      if (document.fullscreenElement === target) await document.exitFullscreen()
      else await target.requestFullscreen()
    } catch {
      // Keep the stage usable when the browser does not allow fullscreen.
    }
  }

  onMounted(() => {
    measure()
    if (stageElement.value && typeof ResizeObserver !== 'undefined') {
      resizeObserver = new ResizeObserver(measure)
      resizeObserver.observe(stageElement.value)
    }
    window.addEventListener('resize', measure)
    document.addEventListener('fullscreenchange', onFullscreenChange)
  })

  onUnmounted(() => {
    resizeObserver?.disconnect()
    window.removeEventListener('resize', measure)
    document.removeEventListener('fullscreenchange', onFullscreenChange)
  })

  return { stageSize, isFullscreen, content, contentStyle, toggleFullscreen }
}
