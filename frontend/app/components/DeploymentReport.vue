<script setup lang="ts">
import { DEPLOY_PHASES, DIAGNOSIS_VERDICTS, deploymentCheckText } from '~/lib/training'
import type { DeploymentCheck, DeploymentReport } from '~/lib/training'
import { useI18n } from '~/composables/useI18n'

// Runtime verification detail (vehicle_deployment.py): frozen failures, the latest checks and per-camera values.
const props = defineProps<{ report: DeploymentReport }>()
const { t } = useI18n()
const failures = computed(() => props.report.failures ?? [])
const checks = computed(() => props.report.checks ?? [])
const evidence = computed(() => Object.entries(props.report.evidence ?? {}))
const diagnosis = computed(() => Object.entries(props.report.diagnosis?.cameras ?? {}))
const slowCameras = computed(() => Object.entries(props.report.service?.cameras ?? {}).filter(([, item]) => item.fps < 0.85 * item.baseline_fps))
function checkText(check: DeploymentCheck) {
  const { key, params } = deploymentCheckText(check)
  return t(key, params)
}
const yesNo = (value?: boolean) => value ? t('yes') : t('no')
const range = (values?: (number | null)[] | null) => ({ low: values?.[0] ?? '–', high: values?.[1] ?? '–' })
</script>

<template>
  <div class="space-y-2 text-xs">
    <div v-for="(failure, index) in failures" :key="index" class="space-y-1 rounded-lg border border-red-400/20 bg-red-400/5 p-2 text-red-200">
      <p><span class="font-mono">{{ failure.code }}</span> · {{ t(DEPLOY_PHASES[failure.phase] ?? failure.phase) }}<template v-if="failure.model_id"> · {{ failure.model_id }}</template></p>
      <p class="text-red-200/80">{{ t('Load event received: {event} · model loaded: {loaded} · {seconds} s after switch', { event: yesNo(failure.load_event), loaded: yesNo(failure.model_loaded), seconds: failure.elapsed_seconds ?? '–' }) }}</p>
      <p v-for="(check, i) in failure.checks" :key="i">• {{ checkText(check) }}</p>
      <p v-for="(item, camera) in failure.evidence ?? {}" :key="camera" class="font-mono text-red-200/70">{{ camera }} · {{ t('{fps}/{baseline} fps · {state} · {frames} inference frames · {detections} detections · last inference {age} s ago', { fps: item.fps ?? '–', baseline: item.baseline_fps ?? '–', state: item.state ?? '–', frames: item.frames, detections: item.detections, age: item.inference_age ?? '–' }) }}</p>
      <p v-if="failure.service" class="font-mono text-red-200/70">{{ t('Other cameras: {fps}/{baseline} fps total', { fps: failure.service.fps, baseline: failure.service.baseline_fps }) }}</p>
    </div>
    <div v-if="diagnosis.length" class="space-y-1 rounded-lg border border-amber-400/20 bg-amber-400/5 p-2 text-amber-100">
      <p>{{ t('FPS after rollback') }}</p>
      <p v-for="[camera, item] in diagnosis" :key="camera" class="font-mono text-amber-100/80">{{ t('{camera}: {baseline} fps before (range {low}–{high}) → {candidate} fps with the new model → {rollback} fps with the previous model; {verdict}', { camera, baseline: item.baseline_fps, ...range(item.baseline_range), candidate: item.candidate_fps, rollback: item.rollback_fps, verdict: t(DIAGNOSIS_VERDICTS[item.verdict] ?? item.verdict) }) }}</p>
    </div>
    <p v-for="(check, i) in checks" :key="`c${i}`" :class="check.blocking === false ? 'text-amber-200' : 'text-red-300'">
      {{ check.blocking === false ? t('Advisory') : t('Failing check') }}: {{ checkText(check) }}
    </p>
    <p v-for="[camera, item] in evidence" :key="camera" class="text-muted-foreground">
      {{ camera }} · {{ t('{frames} inference frames, {detections} detections', { frames: item.frames, detections: item.detections }) }}<template v-if="item.baseline_fps != null"> · {{ t('{fps}/{baseline} fps · {state} · last inference {age} s ago', { fps: item.fps ?? '–', baseline: item.baseline_fps, state: item.state ?? '–', age: item.inference_age ?? '–' }) }}</template><template v-if="item.baseline_range"> · {{ t('pre-switch range {low}–{high} fps', range(item.baseline_range)) }}</template>
    </p>
    <p v-if="report.service" class="text-muted-foreground">
      {{ t('Other cameras: {fps}/{baseline} fps total', { fps: report.service.fps, baseline: report.service.baseline_fps }) }}<template v-if="slowCameras.length"> · {{ t('below 85%: {cameras}', { cameras: slowCameras.map(([camera, item]) => `${camera} ${item.fps}/${item.baseline_fps}`).join(', ') }) }}</template>
    </p>
  </div>
</template>
