export async function datasetRequest<T>(path: string, options: { method?: 'GET' | 'POST' | 'PUT' | 'DELETE'; body?: object } = {}): Promise<T> {
  try {
    return await $fetch<T>('/api/datasets' + path, { ...options, retry: 0 })
  } catch (cause) {
    const error = cause as { statusCode?: number; data?: { detail?: unknown } }
    const detail = error.data?.detail
    throw new Error(typeof detail === 'string' ? detail : error.statusCode === 404
      ? 'The dataset API is unavailable. The backend needs the dataset update.'
      : 'Could not reach the dataset service. Please try again.')
  }
}
