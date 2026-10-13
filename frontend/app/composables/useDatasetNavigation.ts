import { datasetRequest } from '~/composables/datasetApi'
import { collectDatasetIds, datasetNeighbors } from '~/lib/datasetNavigation'
import type { DatasetPage } from '~/types/dataset'

type ListContext = { ids: string[]; camera: string; status: string; page: number; pageSize: number }
const storageKey = 'retrace.dataset.navigation.v1'
function restore(): ListContext | null {
  if (typeof sessionStorage === 'undefined') return null
  try {
    const value = JSON.parse(sessionStorage.getItem(storageKey) ?? 'null')
    if (value && Array.isArray(value.ids) && value.ids.every((id: unknown) => typeof id === 'string') && typeof value.camera === 'string' && typeof value.status === 'string' && Number.isInteger(value.page) && value.page > 0) {
      // Older sessions used 24 images per page.
      return { ...value, pageSize: [2, 4, 8, 24].includes(value.pageSize) ? value.pageSize : 24 }
    }
  } catch { /* Storage may be unavailable; in-memory navigation still works. */ }
  return null
}
export function useDatasetNavigation() {
  const context = useState<ListContext | null>('dataset-navigation', restore)
  const listCamera = useState('dataset-list-camera', () => context.value?.camera ?? '')
  const listStatus = useState('dataset-list-status', () => context.value?.status ?? '')
  const listPage = useState('dataset-list-page', () => context.value?.page ?? 1)
  const listPageSize = useState('dataset-list-page-size', () => context.value?.pageSize ?? 8)
  async function prepare(current: DatasetPage, camera: string, status: string, targetId: string) {
    const query = new URLSearchParams({ page_size: String(current.page_size) })
    if (camera) query.set('camera_id', camera)
    if (status) query.set('status', status)
    const ids = await collectDatasetIds(current, page => {
      query.set('page', String(page))
      return datasetRequest<DatasetPage>('/images?' + query)
    })
    if (!ids.includes(targetId)) ids.unshift(targetId)
    context.value = { ids, camera, status, page: current.page, pageSize: current.page_size }
    try { sessionStorage.setItem(storageKey, JSON.stringify(context.value)) } catch { /* Optional persistence. */ }
  }
  async function ensure(currentId: string) {
    if (context.value?.ids.includes(currentId)) return
    const first = await datasetRequest<DatasetPage>('/images?page=1&page_size=24')
    await prepare(first, '', '', currentId)
    listCamera.value = ''; listStatus.value = ''; listPage.value = 1; listPageSize.value = 24
  }
  return { context, listCamera, listStatus, listPage, listPageSize, prepare, ensure, neighbors: (id: string) => datasetNeighbors(context.value?.ids ?? [], id) }
}
