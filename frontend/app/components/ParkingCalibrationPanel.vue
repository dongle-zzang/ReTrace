<script setup lang="ts">
import type { ParkingStatus } from '~/types/parking'
import { CircleAlert, Crosshair, RotateCcw, X } from '@lucide/vue'
import ParkingStatusBadge from '~/components/ParkingStatusBadge.vue'
import { Alert, AlertDescription } from '~/components/ui/alert'
import { Badge } from '~/components/ui/badge'
import { Button } from '~/components/ui/button'

/**
 * Empty-spot baseline for one zone. The color detector compares the live view against this
 * baseline, so it must be registered while the spot is actually empty.
 */
const props = defineProps<{
  label: string
  status: ParkingStatus
  score: number | null
  /** true / false when known from this page's own calibrate/reset; null when the server has not said. */
  calibrated: boolean | null
  calibrate: (register: boolean) => Promise<void>
}>()

const emit = defineEmits<{ close: [] }>()

const busy = ref<'register' | 'reset' | null>(null)
const error = ref<string | null>(null)
const done = ref<string | null>(null)

async function run(register: boolean) {
  if (register && props.status === 'occupied'
    && !window.confirm(`${props.label} currently reads as occupied. Register the baseline anyway? Only do this if the spot is really empty.`)) return
  if (!register && !window.confirm(`Reset the empty baseline of ${props.label}?`)) return
  busy.value = register ? 'register' : 'reset'
  error.value = null
  done.value = null
  try {
    await props.calibrate(register)
    done.value = register ? 'Empty baseline registered.' : 'Baseline reset.'
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : 'Request failed.'
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
  <section class="space-y-3 rounded-xl border border-primary/30 bg-primary/[0.05] p-3.5" :aria-label="'Calibration of ' + label">
    <div class="flex items-center gap-2">
      <Crosshair class="size-4 text-primary" />
      <h3 class="font-mono text-sm font-semibold">{{ label }}</h3>
      <Button size="icon-sm" variant="ghost" class="ml-auto" aria-label="Close calibration" @click="emit('close')">
        <X />
      </Button>
    </div>

    <dl class="grid grid-cols-[auto_1fr] items-center gap-x-3 gap-y-1.5 text-xs">
      <dt class="text-muted-foreground">Status (this camera)</dt>
      <dd class="justify-self-end"><ParkingStatusBadge :status="status" :score="score" /></dd>
      <dt class="text-muted-foreground">Empty baseline</dt>
      <dd class="justify-self-end">
        <Badge v-if="calibrated === true" class="bg-green-500/15 text-green-300">Registered</Badge>
        <Badge v-else-if="calibrated === false" variant="outline" class="text-muted-foreground">Not registered</Badge>
        <span v-else class="text-muted-foreground">Not reported by the server</span>
      </dd>
    </dl>

    <p class="text-xs leading-relaxed text-amber-200/90">
      Register the baseline only while this spot is actually empty — no car, nothing parked on the lines.
      The current camera view becomes the reference for “empty”.
    </p>

    <div class="flex flex-wrap gap-2">
      <Button size="sm" :disabled="!!busy" @click="run(true)">
        <Crosshair /> {{ busy === 'register' ? 'Registering…' : 'Register empty baseline' }}
      </Button>
      <Button size="sm" variant="outline" :disabled="!!busy" @click="run(false)">
        <RotateCcw /> {{ busy === 'reset' ? 'Resetting…' : 'Reset baseline' }}
      </Button>
    </div>

    <p v-if="done" class="text-xs text-green-300" role="status">{{ done }}</p>
    <Alert v-if="error" variant="destructive" class="border-destructive/30 bg-destructive/10 px-3 py-2">
      <CircleAlert />
      <AlertDescription>{{ error }}</AlertDescription>
    </Alert>
  </section>
</template>
