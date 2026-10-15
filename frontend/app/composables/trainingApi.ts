// Training control is protected by the Backend's TRAINING_ADMIN_TOKEN (no login exists yet).
// The token stays in this tab's sessionStorage only and is sent as a Bearer header.
const TOKEN_KEY = 'retrace.trainingToken'
const token = ref('')
let loaded = false

function readToken() {
  if (loaded || typeof window === 'undefined') return
  loaded = true
  try { token.value = window.sessionStorage.getItem(TOKEN_KEY) ?? '' } catch { token.value = '' }
}

export function useTrainingToken() {
  readToken()
  function set(value: string) {
    token.value = value
    try {
      if (value) window.sessionStorage.setItem(TOKEN_KEY, value)
      else window.sessionStorage.removeItem(TOKEN_KEY)
    } catch { /* private mode: keep it in memory for this page only */ }
  }
  return { token: readonly(token), setToken: set }
}

function message(cause: unknown) {
  const error = cause as { statusCode?: number; data?: { detail?: unknown } }
  const detail = error.data?.detail
  if (typeof detail === 'string') return detail
  if (error.statusCode === 404) return 'The training API is unavailable. The backend needs the training update.'
  if (error.statusCode === 401) return 'Training admin token required or invalid.'
  return 'Could not reach the training service. Please try again.'
}

export async function trainingRequest<T>(path: string, options: { method?: 'GET' | 'POST'; body?: object; auth?: boolean } = {}): Promise<T> {
  readToken()
  const method = options.method ?? 'GET'
  // The Preview proxy rejects a POST without a JSON body (415), so body-less calls (/auth, /stop) send {}.
  const body = method === 'POST' ? options.body ?? {} : options.body
  try {
    return await $fetch<T>('/api/training' + path, {
      method, body, retry: 0,
      headers: {
        ...(body ? { 'Content-Type': 'application/json' } : {}),
        ...(options.auth && token.value ? { Authorization: `Bearer ${token.value}` } : {}),
      },
    })
  } catch (cause) {
    const error = new Error(message(cause)) as Error & { status?: number }
    error.status = (cause as { statusCode?: number }).statusCode
    throw error
  }
}

// Model/artifact files need the Authorization header, so they are fetched and saved as a blob.
export async function downloadTrainingFile(url: string, filename: string) {
  readToken()
  const response = await fetch(url, { headers: token.value ? { Authorization: `Bearer ${token.value}` } : {} })
  if (!response.ok) {
    let detail = ''
    try { detail = (await response.json()).detail } catch { /* not JSON */ }
    throw new Error(detail || (response.status === 401 ? 'Training admin token required or invalid.' : `Download failed (HTTP ${response.status}).`))
  }
  const href = URL.createObjectURL(await response.blob())
  const link = document.createElement('a')
  link.href = href
  link.download = filename
  link.click()
  setTimeout(() => URL.revokeObjectURL(href), 1000)
}
