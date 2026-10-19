<script setup lang="ts">
import type { ParkingStatus } from '~/types/parking'
import { CircleAlert, Crosshair, RotateCcw, X } from '@lucide/vue'
import ParkingStatusBadge from '~/components/ParkingStatusBadge.vue'
import { Alert, AlertDescription } from '~/components/ui/alert'
import { Badge } from '~/components/ui/badge'
import { Button } from '~/components/ui/button'
import { useI18n } from '~/composables/useI18n'

/**
 * Empty-spot baseline for one zone. The color detector compares the live view against this
 * baseline, so it must be registered while the spot is actually empty. With `baseline: false`
 * (vehicle-detection occupancy) only the zone's status is shown.
 */
const props = withDefaults(defineProps<{
  label: string
  status: ParkingStatus
  score: number | null
  /** The zone's `calibrated` flag from the server; null when it isn't known yet. */
  calibrated: boolean | null
  calibrate: (register: boolean) => Promise<void>
  baseline?: boolean
  /** Detection mode: this zone's own "occupied from" overlap (0–1), or null for the default. */
  threshold?: number | null
  defaultThreshold?: number
}>(), { baseline: true, threshold: null, defaultThreshold: 0.55 })

const emit = defineEmits<{ close: []; 'update:threshold': [value: number | null] }>()
const percent = (value: number) => Math.round(value * 100)
const thresholdPercent = computed(() => percent(props.threshold ?? props.defaultThreshold))
function onThreshold(event: Event) {
  const value = Number((event.target as HTMLInputElement).value) / 100
  emit('update:threshold', percent(value) === percent(props.defaultThreshold) ? null : value)
}

const busy = ref<'register' | 'reset' | null>(null)
const error = ref<string | null>(null)
const done = ref<string | null>(null)

const { confirm } = useConfirmDialog()
const { t } = useI18n()

async function run(register: boolean) {
  if (busy.value) return
  const requestedLabel = props.label
  if (register && props.status === 'occupied'
    && !await confirm({ title: t('Register empty baseline?'), description: t('{label} currently reads as occupied. Continue only if the spot is actually empty.', { label: props.label }), confirmLabel: t('Register baseline') })) return
  if (!register && !await confirm({ title: t('Reset empty baseline?'), description: t('Reset the empty baseline of {label}?', { label: props.label }), confirmLabel: t('Reset baseline'), destructive: true })) return
  if (props.label !== requestedLabel || busy.value) return
  busy.value = register ? 'register' : 'reset'
  error.value = null
  done.value = null
  try {
    await props.calibrate(register)
    done.value = register ? t('Empty baseline registered.') : t('Baseline reset.')
  } catch (cause) {
    error.value = t(cause instanceof Error ? cause.message : 'Request failed.')
  } finally {
    busy.value = null
  }
}

watch(() => props.label, () => {
  error.value = null
  done.value = null
})
</script>

<template>
  <section class="space-y-3 rounded-xl border border-primary/30 bg-primary/[0.05] p-3.5" :aria-label="t('Calibration of {label}', { label })">
    <div class="flex items-center gap-2">
      <Crosshair class="size-4 text-primary" />
      <h3 class="font-mono text-sm font-semibold">{{ label }}</h3>
      <Button size="icon-sm" variant="ghost" class="ml-auto" :aria-label="t('Close calibration')" @click="emit('close')">
        <X />
      </Button>
    </div>

    <dl class="grid grid-cols-[auto_1fr] items-center gap-x-3 gap-y-1.5 text-xs">
      <dt class="text-muted-foreground">{{ t('Status (this camera)') }}</dt>
      <dd class="justify-self-end"><ParkingStatusBadge :status="status" :score="score" /></dd>
      <template v-if="baseline">
      <dt class="text-muted-foreground">{{ t('Empty baseline') }}</dt>
      <dd class="justify-self-end">
        <Badge v-if="calibrated === true" class="bg-green-500/15 text-green-300">{{ t('Registered') }}</Badge>
        <Badge v-else-if="calibrated === false" variant="outline" class="text-muted-foreground">{{ t('Not registered') }}</Badge>
        <span v-else class="text-muted-foreground">{{ t('Not reported by the server') }}</span>
      </dd>
      </template>
    </dl>

    <template v-if="!baseline">
      <p class="text-xs leading-relaxed text-muted-foreground">
        {{ t('Decided by vehicle detection: occupied while a detected vehicle’s bottom area sits on this zone. The number is how much of that area overlaps the zone.') }}
      </p>
      <div class="space-y-1.5">
        <div class="flex items-center justify-between gap-2 text-xs">
          <label :for="`threshold-${label}`" class="text-muted-foreground">{{ t('Occupied from') }}</label>
          <span class="font-mono tabular-nums">{{ thresholdPercent }}%<span v-if="threshold === null" class="ml-1 font-sans text-muted-foreground">({{ t('default') }})</span></span>
        </div>
        <input :id="`threshold-${label}`" type="range" min="10" max="95" step="5" :value="thresholdPercent" class="w-full accent-primary" @change="onThreshold">
        <div class="flex items-center justify-between gap-2">
          <p class="text-xs text-muted-foreground">{{ t('Overlap now: {value}%', { value: score === null ? '–' : percent(score) }) }}</p>
          <Button v-if="threshold !== null" size="sm" variant="ghost" class="h-6 px-2 text-xs" @click="emit('update:threshold', null)">{{ t('Reset to default') }}</Button>
        </div>
        <p class="text-xs text-muted-foreground">{{ t('Saved in this browser.') }}</p>
      </div>
    </template>

    <template v-else>
    <p class="text-xs leading-relaxed text-amber-200/90">
      {{ t('Register the baseline only while this spot is actually empty — no car, nothing parked on the lines. The current camera view becomes the reference for “empty”.') }}
    </p>

    <div class="flex flex-wrap gap-2">
      <Button size="sm" :disabled="!!busy" @click="run(true)">
        <Crosshair /> {{ busy === 'register' ? t('Registering…') : t('Register empty baseline') }}
      </Button>
      <Button size="sm" variant="outline" :disabled="!!busy" @click="run(false)">
        <RotateCcw /> {{ busy === 'reset' ? t('Resetting…') : t('Reset baseline') }}
      </Button>
    </div>
    </template>

    <p v-if="done" class="text-xs text-green-300" role="status">{{ done }}</p>
    <Alert v-if="error" variant="destructive" class="border-destructive/30 bg-destructive/10 px-3 py-2">
      <CircleAlert />
      <AlertDescription>{{ error }}</AlertDescription>
    </Alert>
  </section>
</template>
