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
      error.value = '카메라 목록을 불러오지 못했습니다. Backend 연결을 확인하세요.'
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
