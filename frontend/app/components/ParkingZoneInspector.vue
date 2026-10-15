<script setup lang="ts">
import type { ParkingSpace, ParkingZone } from '~/types/parking'
import { Pencil, Plus, Trash2, X } from '@lucide/vue'
import { DialogContent, DialogPortal } from 'reka-ui'
import { Button } from '~/components/ui/button'
import { Dialog, DialogClose, DialogDescription, DialogOverlay, DialogTitle } from '~/components/ui/dialog'
import { compareLabels, findSpaceByLabel, labelKey, MAX_LABEL_LENGTH, validateLabel } from '~/lib/parking'
import { useI18n } from '~/composables/useI18n'

const props = defineProps<{
  zone: ParkingZone
  zones: ParkingZone[]
  spaces: ParkingSpace[]
  disabled: boolean
  createSpace: (label: string) => Promise<ParkingSpace>
  /** Portal inside the workspace so the modal remains visible in native fullscreen. */
  portalTarget?: HTMLElement | null
  /** Visible toolbar control to restore focus to when the side panel is closed. */
  returnFocusTarget?: HTMLElement | null
}>()
const emit = defineEmits<{
  assign: [zoneId: string, spaceId: string]
  remove: [zoneId: string]
  'add-another': []
}>()

const { t } = useI18n()
const input = ref<HTMLInputElement | null>(null)
const labelTrigger = ref<HTMLButtonElement | null>(null)
const open = ref(false)
const modalSpaceId = ref(props.zone.parkingSpaceId)
const closingBusy = ref(false)
const closingExisting = ref(false)
const closingDisabled = ref(false)
const applyDisabled = computed(() => open.value ? props.disabled || busy.value || !query.value.trim() || unchanged.value : closingDisabled.value)
const displayBusy = computed(() => open.value ? busy.value : closingBusy.value)
const displayExisting = computed(() => open.value ? !!existing.value : closingExisting.value)
const query = ref('')
const closingQuery = ref('')
const closingError = ref<string | null>(null)
const closingOptions = ref<ParkingSpace[]>([])
const modalQuery = computed({ get: () => open.value ? query.value : closingQuery.value, set: (value: string) => { query.value = value } })
const modalError = computed(() => open.value ? error.value : closingError.value)
const modalOptions = computed(() => open.value ? labelOptions.value : closingOptions.value)
const busy = ref(false)
const error = ref<string | null>(null)
const inputId = useId()
const errorId = useId()
let requestVersion = 0
const currentLabel = computed(() => props.spaces.find((space) => space.id === props.zone.parkingSpaceId)?.label ?? '')
const existing = computed(() => findSpaceByLabel(props.spaces, query.value))
const labelOptions = computed(() => {
  const search = labelKey(query.value)
  return [...props.spaces]
    .filter((space) => !search || labelKey(space.label).includes(search))
    .sort((a, b) => compareLabels(a.label, b.label))
})
const usedOnCamera = (spaceId: string) => props.zones.some((zone) => zone.id !== props.zone.id && zone.parkingSpaceId === spaceId)
const unchanged = computed(() => !!existing.value && existing.value.id === props.zone.parkingSpaceId)

function setOpen(next: boolean) {
  if (next && props.disabled) return
  // Closing cancels the application, even if a server-side label creation is still in flight.
  requestVersion++
  if (!next && open.value) { closingBusy.value = busy.value; closingExisting.value = !!existing.value; closingDisabled.value = applyDisabled.value; closingQuery.value = query.value; closingError.value = error.value; closingOptions.value = [...labelOptions.value] }
  busy.value = false
  if (next) { error.value = null; query.value = currentLabel.value; modalSpaceId.value = props.zone.parkingSpaceId }
  open.value = next
}

watch([() => props.zone.id, () => props.zone.parkingSpaceId, currentLabel], () => {
  setOpen(false)
  query.value = currentLabel.value
}, { immediate: true })
watch(query, () => { error.value = null })
onUnmounted(() => { requestVersion++ })

function focus() {
  setOpen(true)
}
function onOpenAutoFocus(event: Event) {
  event.preventDefault()
  input.value?.focus()
  input.value?.select()
}
function onDialogKeydown(event: KeyboardEvent) {
  // Keep editor shortcuts out of the modal, while still allowing Escape to dismiss it.
  event.stopPropagation()
  if (event.key === 'Escape') {
    event.preventDefault()
    setOpen(false)
  }
}
function onCloseAutoFocus(event: Event) {
  event.preventDefault()
  const target = labelTrigger.value?.getClientRects().length ? labelTrigger.value : props.returnFocusTarget
  target?.focus()
}
defineExpose({ focus })

async function apply() {
  if (!open.value || props.disabled || busy.value) return
  const label = query.value.trim().replace(/\s+/g, ' ')
  const problem = validateLabel(label)
  if (problem) {
    error.value = problem
    return
  }
  const space = findSpaceByLabel(props.spaces, label)
  if (space && props.zones.some((zone) => zone.id !== props.zone.id && zone.parkingSpaceId === space.id)) {
    error.value = 'That label is already used on this camera.'
    return
  }
  const version = ++requestVersion
  const zoneId = props.zone.id
  busy.value = true
  error.value = null
  try {
    const resolved = space ?? await props.createSpace(label)
    if (version !== requestVersion || !open.value || props.disabled || props.zone.id !== zoneId) return
    if (props.zones.some((zone) => zone.id !== zoneId && zone.parkingSpaceId === resolved.id)) {
      error.value = 'That label is already used on this camera.'
      return
    }
    emit('assign', zoneId, resolved.id)
    setOpen(false)
  } catch (cause) {
    if (version === requestVersion) error.value = cause instanceof Error ? cause.message : "Couldn't add the label."
  } finally {
    if (version === requestVersion) busy.value = false
  }
}
</script>

<template>
  <div class="flex flex-col gap-3" :aria-label="t('Selected zone editor')">
    <div class="min-w-0 flex-1">
      <p class="text-xs text-muted-foreground">{{ t('Selected zone') }}</p>
      <p class="mt-1 truncate text-sm font-semibold" :class="!zone.parkingSpaceId ? 'text-amber-200' : ''">
        {{ currentLabel || t('Choose a label') }}
      </p>
    </div>
    <div class="flex flex-wrap gap-2">
      <Button as-child variant="outline" :disabled="disabled">
        <button ref="labelTrigger" type="button" @click="focus">
          <Pencil v-if="zone.parkingSpaceId" /><Plus v-else />
          {{ zone.parkingSpaceId ? t('Edit label') : t('Set label') }}
        </button>
      </Button>
      <Button variant="outline" :disabled="disabled || !zone.parkingSpaceId" @click="emit('add-another')"><Plus /> {{ t('Add another') }}</Button>
      <Button variant="ghost" class="text-muted-foreground hover:text-destructive" :disabled="disabled" :aria-label="t('Delete selected zone')" @click="emit('remove', zone.id)">
        <Trash2 /><span class="sm:sr-only">{{ t('Delete zone') }}</span>
      </Button>
    </div>

    <Dialog :open="open" @update:open="setOpen">
      <!-- The shared content portals to body. This local portal also supports fullscreen editing. -->
      <DialogPortal :to="portalTarget ?? undefined">
        <DialogOverlay class="parking-label-backdrop bg-black/60 backdrop-blur-sm" />
        <DialogContent
          class="parking-label-modal fixed top-1/2 left-1/2 z-50 grid max-h-[calc(100dvh-2rem)] w-[calc(100%-2rem)] max-w-md -translate-x-1/2 -translate-y-1/2 gap-5 overflow-y-auto rounded-2xl border border-white/10 bg-popover p-5 text-popover-foreground shadow-2xl outline-none sm:p-6"
          @open-auto-focus="onOpenAutoFocus"
          @close-auto-focus="onCloseAutoFocus"
          @keydown="onDialogKeydown"
        >
          <div class="space-y-2 pr-7">
            <DialogTitle class="text-lg font-semibold">{{ modalSpaceId ? t('Edit zone label') : t('Name this zone') }}</DialogTitle>
            <DialogDescription class="text-sm text-muted-foreground">
              {{ t('Choose an existing label to share this spot across cameras, or type a new one.') }}
            </DialogDescription>
          </div>
          <DialogClose as-child>
            <Button variant="ghost" size="icon-sm" class="absolute top-3 right-3" :aria-label="t('Close label dialog')"><X /></Button>
          </DialogClose>
          <form class="space-y-4" @submit.prevent="apply">
            <div class="space-y-2">
              <label :for="inputId" class="text-sm font-medium">{{ t('Label') }}</label>
              <input
                :id="inputId"
                ref="input"
                v-model="modalQuery"
                :maxlength="MAX_LABEL_LENGTH"
                :disabled="disabled || displayBusy"
                :aria-invalid="!!modalError"
                :aria-describedby="modalError ? errorId : undefined"
                autocomplete="off"
                :placeholder="t('e.g. A-01')"
                class="h-11 w-full rounded-lg border border-white/15 bg-white/[0.04] px-3 text-base outline-none placeholder:text-muted-foreground focus:border-primary focus:ring-2 focus:ring-primary/20 disabled:opacity-50"
              >
              <div v-if="modalOptions.length" class="space-y-1.5">
                <p class="px-1 text-xs font-medium text-muted-foreground">{{ t('Registered labels') }}</p>
                <ul class="max-h-48 space-y-1 overflow-y-auto rounded-lg border border-white/10 bg-black/10 p-1" :aria-label="t('Registered labels')">
                  <li v-for="space in modalOptions" :key="space.id">
                    <button type="button" class="flex w-full items-center justify-between gap-3 rounded-md px-3 py-2 text-left text-sm transition-colors hover:bg-white/5 focus-visible:outline focus-visible:outline-primary disabled:cursor-not-allowed disabled:opacity-50" :disabled="displayBusy || disabled || usedOnCamera(space.id)" @click="query = space.label">
                      <span class="truncate">{{ space.label }}</span>
                    </button>
                  </li>
                </ul>
              </div>
              <p v-else-if="modalQuery.trim() && !displayExisting" class="text-xs text-muted-foreground">{{ t('No registered match. A new label will be created.') }}</p>
            </div>
            <p v-if="modalError" :id="errorId" class="text-sm text-red-300" role="alert">{{ t(modalError) }}</p>
            <p class="text-xs leading-relaxed text-muted-foreground">{{ t('Apply the label to this zone, then use Save changes to save your zones.') }}</p>
            <div class="flex justify-end gap-2">
              <Button type="button" variant="outline" @click="setOpen(false)">{{ t('Cancel') }}</Button>
              <Button type="submit" class="border border-primary/20 bg-primary/15 text-primary hover:bg-primary/25" :disabled="applyDisabled">
                {{ displayBusy ? t('Applying…') : displayExisting ? t('Apply label') : t('Create & apply') }}
              </Button>
            </div>
          </form>
        </DialogContent>
      </DialogPortal>
    </Dialog>
  </div>
</template>


<style scoped>
.parking-label-modal[data-state='open'],
.parking-label-backdrop[data-state='open'] {
  animation: parking-label-fade-in 180ms ease-out both;
}
.parking-label-modal[data-state='closed'],
.parking-label-backdrop[data-state='closed'] {
  animation: parking-label-fade-out 140ms ease-in both;
  pointer-events: none;
}
@keyframes parking-label-fade-in {
  from { opacity: 0; }
  to { opacity: 1; }
}
@keyframes parking-label-fade-out {
  from { opacity: 1; }
  to { opacity: 0; }
}
</style>
