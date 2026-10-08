<script setup lang="ts">
import type { ParkingSpace, ParkingStatus, ParkingZone } from '~/types/parking'
import { LoaderCircle, Plus, Trash2, TriangleAlert } from '@lucide/vue'
import { Button } from '~/components/ui/button'
import { compareLabels, findSpaceByLabel, labelKey, parkingStatusLabel, validateLabel } from '~/lib/parking'

/**
 * The one place labels are chosen. Every parking space (label) is a row; a label drawn on this
 * camera shows its status dot. The last row searches the list and creates a label that does not
 * exist yet (Enter or +), so the same label is never created twice.
 */
const props = withDefaults(defineProps<{
  spaces: ParkingSpace[]
  /** This camera's zones (the draft while editing). */
  zones: ParkingZone[]
  statuses: Record<string, ParkingStatus>
  scores?: Record<string, number | null>
  /** Highlighted label: the selected zone's, or the one picked for the next shape. */
  activeSpaceId?: string | null
  selectedZoneId?: string | null
  editing?: boolean
  conflictSpaceIds?: string[]
  createSpace: (label: string) => Promise<ParkingSpace>
}>(), { scores: () => ({}), activeSpaceId: null, selectedZoneId: null, editing: false, conflictSpaceIds: () => [] })

const emit = defineEmits<{
  pick: [spaceId: string]
  'select-zone': [zoneId: string]
  'remove-zone': [zoneId: string]
}>()

const query = ref('')
const creating = ref(false)
const error = ref<string | null>(null)

const zoneBySpace = computed(() => new Map(props.zones.filter((zone) => zone.parkingSpaceId).map((zone) => [zone.parkingSpaceId, zone])))
const unlabeled = computed(() => props.zones.filter((zone) => !zone.parkingSpaceId))

const rows = computed(() => {
  const key = labelKey(query.value)
  return [...props.spaces]
    .filter((space) => !key || labelKey(space.label).includes(key))
    // Labels drawn on this camera first, then the rest; each group in natural order.
    .sort((a, b) => Number(zoneBySpace.value.has(b.id)) - Number(zoneBySpace.value.has(a.id)) || compareLabels(a.label, b.label))
})

const dot: Record<ParkingStatus, string> = {
  occupied: 'bg-red-500 shadow-[0_0_8px_var(--color-red-500)]',
  empty: 'bg-green-500 shadow-[0_0_8px_var(--color-green-500)]',
  unknown: 'bg-zinc-400',
}
const text: Record<ParkingStatus, string> = { occupied: 'text-red-300', empty: 'text-green-300', unknown: 'text-muted-foreground' }

async function submit() {
  const label = query.value.trim().replace(/\s+/g, ' ')
  if (!label || creating.value) return
  const existing = findSpaceByLabel(props.spaces, label)
  if (existing) {
    query.value = ''
    emit('pick', existing.id)
    return
  }
  const problem = validateLabel(label)
  if (problem) {
    error.value = problem
    return
  }
  creating.value = true
  error.value = null
  try {
    const space = await props.createSpace(label)
    query.value = ''
    emit('pick', space.id)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "Couldn't add the label."
  } finally {
    creating.value = false
  }
}

watch(query, () => (error.value = null))
</script>

<template>
  <div class="flex min-h-0 flex-col gap-1.5">
    <ul class="-mx-1 min-h-0 flex-1 space-y-1.5 overflow-y-auto px-1" aria-label="Labels">
      <li v-for="zone in unlabeled" :key="zone.id">
        <button
          type="button"
          :class="[
            'flex h-11 w-full items-center gap-3 rounded-xl border px-3 text-left text-sm transition-colors',
            selectedZoneId === zone.id ? 'border-amber-400/50 bg-amber-400/[0.08]' : 'border-transparent bg-white/[0.04] hover:bg-white/[0.07]',
          ]"
          @click="emit('select-zone', zone.id)"
        >
          <span class="size-3 shrink-0 rounded-full border border-dashed border-amber-300" aria-hidden="true" />
          <span class="flex-1 text-amber-200">New zone</span>
          <span class="text-xs text-muted-foreground">pick a label</span>
        </button>
      </li>

      <li v-for="space in rows" :key="space.id" class="group/row relative">
        <button
          type="button"
          :class="[
            'flex h-11 w-full items-center gap-3 rounded-xl border px-3 text-left text-sm transition-colors',
            editing && zoneBySpace.get(space.id) ? 'pr-11' : '',
            activeSpaceId === space.id ? 'border-primary/50 bg-primary/[0.08]' : 'border-transparent bg-white/[0.04] hover:bg-white/[0.07]',
          ]"
          :aria-pressed="activeSpaceId === space.id"
          @click="emit('pick', space.id)"
        >
          <span
            :class="[
              'size-3 shrink-0 rounded-full',
              zoneBySpace.get(space.id) ? dot[statuses[zoneBySpace.get(space.id)!.id] ?? 'unknown'] : 'border border-white/25',
            ]"
            aria-hidden="true"
          />
          <span :class="['min-w-0 flex-1 truncate font-medium', zoneBySpace.get(space.id) ? '' : 'text-muted-foreground']">{{ space.label }}</span>
          <TriangleAlert v-if="conflictSpaceIds.includes(space.id)" class="size-3.5 shrink-0 text-amber-300" aria-label="Cameras disagree" />
          <span v-if="zoneBySpace.get(space.id)" :class="['shrink-0 text-xs', text[statuses[zoneBySpace.get(space.id)!.id] ?? 'unknown']]">
            {{ parkingStatusLabel[statuses[zoneBySpace.get(space.id)!.id] ?? 'unknown'] }}
            <span v-if="scores[zoneBySpace.get(space.id)!.id] != null" class="font-mono text-muted-foreground tabular-nums">{{ scores[zoneBySpace.get(space.id)!.id]!.toFixed(2) }}</span>
          </span>
          <span v-else class="shrink-0 text-xs text-muted-foreground/70">not on this camera</span>
        </button>
        <Button
          v-if="editing && zoneBySpace.get(space.id)"
          size="icon-sm"
          variant="ghost"
          class="absolute top-1/2 right-1.5 -translate-y-1/2 text-muted-foreground hover:text-destructive"
          :aria-label="'Remove ' + space.label + ' from this camera'"
          @click="emit('remove-zone', zoneBySpace.get(space.id)!.id)"
        >
          <Trash2 />
        </Button>
      </li>
    </ul>
    <p v-if="query.trim() && !rows.length" class="px-1 text-xs text-muted-foreground">No label matches “{{ query.trim() }}”.</p>

    <form class="relative shrink-0" @submit.prevent="submit">
      <input
        v-model="query"
        type="text"
        maxlength="60"
        placeholder="Add label"
        aria-label="Search or add a label"
        class="h-11 w-full rounded-xl border border-dashed border-white/10 bg-white/[0.02] pr-11 pl-3 text-sm text-foreground outline-none transition-colors placeholder:text-muted-foreground hover:bg-white/[0.05] focus:border-primary/50 focus:bg-white/[0.05]"
      >
      <Button
        type="submit"
        size="icon-sm"
        variant="ghost"
        class="absolute top-1/2 right-1.5 -translate-y-1/2 text-muted-foreground"
        :disabled="!query.trim() || creating"
        aria-label="Add label"
      >
        <LoaderCircle v-if="creating" class="animate-spin" />
        <Plus v-else />
      </Button>
    </form>
    <p v-if="error" class="px-1 text-xs text-red-300" role="alert">{{ error }}</p>
    <p v-else-if="query.trim() && !findSpaceByLabel(spaces, query)" class="px-1 text-xs text-muted-foreground">
      Enter to add “{{ query.trim() }}” as a new label
    </p>
  </div>
</template>
