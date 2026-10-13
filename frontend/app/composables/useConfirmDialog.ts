import { ref, shallowRef } from 'vue'

type Confirmation = { title: string; description: string; confirmLabel?: string; destructive?: boolean }
const open = ref(false)
const details = shallowRef<Confirmation>({ title: '', description: '' })
let settle: ((accepted: boolean) => void) | null = null

export function useConfirmDialog() {
  function finish(accepted: boolean) {
    open.value = false
    const resolve = settle
    settle = null
    resolve?.(accepted)
  }
  function confirm(options: Confirmation): Promise<boolean> {
    // A new request cancels the previous one instead of leaving a route guard pending.
    finish(false)
    details.value = options
    open.value = true
    return new Promise(resolve => { settle = resolve })
  }
  return { open, details, confirm, finish }
}
