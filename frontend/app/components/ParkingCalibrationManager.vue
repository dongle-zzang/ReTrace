<script setup lang="ts">
import type { ParkingZone, ParkingStatus } from '~/types/parking'
import { Dialog, DialogContent, DialogTitle, DialogDescription } from '~/components/ui/dialog'
import { Button } from '~/components/ui/button'
import ParkingStatusBadge from '~/components/ParkingStatusBadge.vue'
import { useConfirmDialog } from '~/composables/useConfirmDialog'
import { useI18n } from '~/composables/useI18n'
const open = defineModel<boolean>('open', { default: false })
const props = defineProps<{
  zones: ParkingZone[]; labels: Record<string, string>; cameraNames: Record<string, string>
  statuses: Record<string, ParkingStatus>; calibrate: (zoneId: string, register: boolean) => Promise<void>
}>()
const emit = defineEmits<{ compare: [spaceId: string] }>()
const missingOnly = ref(true), busy = ref<string | null>(null), error = ref<string | null>(null)
const rows = computed(() => props.zones.filter(zone => !missingOnly.value || zone.calibrated !== true))
const { confirm } = useConfirmDialog()
const { t } = useI18n()
watch(open, value => { if (value) error.value = null })
async function register(zone: ParkingZone) {
  if (busy.value) return
  if (!await confirm({ title: t('Register empty baseline for {label}?', { label: props.labels[zone.parkingSpaceId] ?? t('this space') }), description: t('Check the camera first and make sure this spot is empty. The current image will become its empty baseline.'), confirmLabel: t('Register baseline') })) return
  busy.value = zone.id; error.value = null
  try { await props.calibrate(zone.id, true) }
  catch (cause) { error.value = t((cause as Error).message) }
  finally { busy.value = null }
}
function compare(id: string) { open.value = false; emit('compare', id) }
function close(value: boolean) { if (!busy.value) { open.value = value; if (value) error.value = null } }
</script>

<template>
  <Dialog :open="open" @update:open="close">
    <DialogContent class="max-h-[85dvh] overflow-y-auto sm:max-w-3xl" :show-close-button="!busy" @escape-key-down="busy && $event.preventDefault()" @pointer-down-outside="busy && $event.preventDefault()">
      <DialogTitle>{{ t('Empty baselines') }}</DialogTitle>
      <DialogDescription>{{ t("Review each camera zone's reference. Only register a baseline while its parking spot is empty.") }}</DialogDescription>
      <label class="flex items-center gap-2 text-sm"><input v-model="missingOnly" type="checkbox" class="accent-primary"> {{ t('Show zones without a registered baseline') }}</label>
      <p v-if="error" role="alert" class="text-sm text-red-300">{{ error }}</p>
      <ul class="divide-y divide-white/10">
        <li v-for="zone in rows" :key="zone.id" class="flex flex-wrap items-center gap-3 py-3">
          <div class="min-w-0 flex-1"><p class="font-mono text-sm font-semibold">{{ labels[zone.parkingSpaceId] ?? t('No label') }}</p><p class="text-xs text-muted-foreground">{{ cameraNames[zone.cameraId] ?? zone.cameraId }} · {{ t(zone.calibrated === true ? 'Registered' : zone.calibrated === false ? 'Not registered' : 'Baseline state unavailable') }}</p></div>
          <ParkingStatusBadge :status="statuses[zone.id] ?? 'unknown'" />
          <Button size="sm" variant="outline" :disabled="!!busy" @click="compare(zone.parkingSpaceId)">{{ t('View cameras') }}</Button>
          <Button as-child size="sm" variant="outline"><NuxtLink :to="`/cameras/${encodeURIComponent(zone.cameraId)}/parking`">{{ t('Edit zone') }}</NuxtLink></Button>
          <Button size="sm" :disabled="!!busy" @click="register(zone)">{{ busy === zone.id ? t('Registering…') : t('Register baseline') }}</Button>
        </li>
      </ul>
      <p v-if="!rows.length" class="py-6 text-sm text-muted-foreground">{{ zones.length ? t('All camera zones have a registered baseline.') : t('No camera zones yet.') }}</p>
    </DialogContent>
  </Dialog>
</template>
