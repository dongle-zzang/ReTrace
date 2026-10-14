<script setup lang="ts">
import type { ParkingSpace, ParkingZone } from '~/types/parking'
import { labelKey, validateLabel } from '~/lib/parking'
import { Dialog, DialogContent, DialogTitle, DialogDescription } from '~/components/ui/dialog'
import { Button } from '~/components/ui/button'
import { Input } from '~/components/ui/input'
import { useConfirmDialog } from '~/composables/useConfirmDialog'
import { useI18n } from '~/composables/useI18n'
const open = defineModel<boolean>('open', { default: false })
const props = defineProps<{
  spaces: ParkingSpace[]; zones: ParkingZone[]; cameraNames: Record<string, string>
  rename: (id: string, label: string) => Promise<void>; remove: (id: string) => Promise<void>
}>()
const query = ref(''), unusedOnly = ref(false), editing = ref<string | null>(null), name = ref('')
const busy = ref(false), error = ref<string | null>(null)
const { confirm } = useConfirmDialog()
const { t } = useI18n()
const usage = (id: string) => props.zones.filter(zone => zone.parkingSpaceId === id)
const rows = computed(() => props.spaces.filter(space => labelKey(space.label).includes(labelKey(query.value)) && (!unusedOnly.value || !usage(space.id).length)))
watch(open, value => { if (value) { error.value = null; editing.value = null } })
function close(value: boolean) { if (!busy.value) open.value = value }
async function save(space: ParkingSpace) {
  const invalid = validateLabel(name.value)
  if (invalid) { error.value = t(invalid); return }
  if (!await confirm({ title: t('Rename {label}?', { label: space.label }), description: t('The new label will be used by every camera linked to this space. Polygons and empty baselines will be kept.'), confirmLabel: t('Rename label') })) return
  busy.value = true; error.value = null
  try { await props.rename(space.id, name.value); editing.value = null }
  catch (cause) { error.value = t((cause as Error).message) }
  finally { busy.value = false }
}
async function remove(space: ParkingSpace) {
  if (usage(space.id).length || busy.value) return
  if (!await confirm({ title: t('Delete {label}?', { label: space.label }), description: t('This label has no camera zones. Delete the unused label permanently?'), confirmLabel: t('Delete label'), destructive: true })) return
  busy.value = true; error.value = null
  try { await props.remove(space.id) }
  catch (cause) { error.value = t((cause as Error).message) }
  finally { busy.value = false }
}
</script>

<template>
  <Dialog :open="open" @update:open="close">
    <DialogContent class="max-h-[85dvh] overflow-y-auto sm:max-w-2xl" :show-close-button="!busy" @escape-key-down="busy && $event.preventDefault()" @pointer-down-outside="busy && $event.preventDefault()">
      <DialogTitle>{{ t('Manage labels') }}</DialogTitle>
      <DialogDescription>{{ t('Rename shared labels or remove unused ones. Labels linked to camera zones cannot be deleted.') }}</DialogDescription>
      <div class="flex flex-wrap items-center gap-3"><Input v-model="query" :placeholder="t('Search labels')" :aria-label="t('Search managed labels')" class="min-w-0 flex-1" /><label class="flex items-center gap-2 text-sm"><input v-model="unusedOnly" type="checkbox" class="accent-primary"> {{ t('Unused only') }}</label></div>
      <p v-if="error" role="alert" class="text-sm text-red-300">{{ error }}</p>
      <ul class="divide-y divide-white/10">
        <li v-for="space in rows" :key="space.id" class="space-y-2 py-3">
          <div class="flex flex-wrap items-center gap-2"><span class="mr-auto font-mono text-sm font-semibold">{{ space.label }}</span><Button size="sm" variant="outline" :disabled="busy" @click="editing = space.id; name = space.label; error = null">{{ t('Rename') }}</Button><Button size="sm" variant="destructive" :disabled="busy || usage(space.id).length > 0" @click="remove(space)">{{ t('Delete') }}</Button></div>
          <p class="text-xs text-muted-foreground">{{ usage(space.id).length ? [...new Set(usage(space.id).map(zone => cameraNames[zone.cameraId] ?? zone.cameraId))].join(' · ') : t('Unused · no camera zones') }}</p>
          <form v-if="editing === space.id" class="flex flex-wrap gap-2" @submit.prevent="save(space)"><Input v-model="name" :disabled="busy" :maxlength="40" :aria-label="t('New name for {label}', { label: space.label })" class="min-w-0 flex-1" /><Button type="submit" size="sm" :disabled="busy">{{ busy ? t('Saving…') : t('Save name') }}</Button><Button type="button" size="sm" variant="outline" :disabled="busy" @click="editing = null">{{ t('Cancel') }}</Button></form>
        </li>
      </ul>
      <p v-if="!rows.length" class="py-6 text-sm text-muted-foreground">{{ t('No matching labels.') }}</p>
    </DialogContent>
  </Dialog>
</template>
