import type { DatasetPage } from '~/types/dataset'

/** Keep the visible page exactly as it was, then fill its neighboring pages in list order. */
export async function collectDatasetIds(current: DatasetPage, fetchPage: (page: number) => Promise<DatasetPage>): Promise<string[]> {
  const ids: string[] = []
  const pages = Math.max(current.page, Math.ceil(current.total / current.page_size))
  for (let page = 1; page <= pages; page++) {
    const rows = page === current.page ? current : await fetchPage(page)
    ids.push(...rows.items.map(image => image.id))
  }
  return [...new Set(ids)]
}
export function datasetNeighbors(ids: string[], currentId: string) {
  const index = ids.indexOf(currentId)
  return { previous: index > 0 ? ids[index - 1]! : null, next: index >= 0 && index + 1 < ids.length ? ids[index + 1]! : null }
}
