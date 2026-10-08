import type { Camera } from '~/types/camera'

const REFRESH_INTERVAL_MS = 10_000

export function useCameras() {
  const cameras = ref<Camera[]>([])
  const loading = ref(true)
  const refreshing = ref(false)
  const error = ref<string | null>(null)
  let intervalId: ReturnType<typeof setInterval> | undefined

  async function refresh() {
    if (refreshing.value) return

    refreshing.value = true
    try {
      // Same origin in production; the dev server proxies /api (see nuxt.config.ts).
      const response = await $fetch<Camera[]>('/api/cameras')
      cameras.value = response
      error.value = null
    } catch {
      error.value = "Couldn't load the camera list. Check the backend connection."
    } finally {
      refreshing.value = false
      loading.value = false
    }
  }

  onMounted(() => {
    void refresh()
    intervalId = setInterval(() => void refresh(), REFRESH_INTERVAL_MS)
  })

  onUnmounted(() => {
    if (intervalId) clearInterval(intervalId)
  })

  return { cameras, loading, refreshing, error, refresh }
}
