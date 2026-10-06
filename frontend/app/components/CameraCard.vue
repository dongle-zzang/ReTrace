<script setup lang="ts">
import type { Camera } from '~/types/camera'
import { Badge } from '~/components/ui/badge'
import { Button } from '~/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '~/components/ui/card'

const props = defineProps<{
  camera: Camera
  previewBaseUrl: string
}>()

const previewElement = ref<HTMLElement | null>(null)
const isVisible = ref(false)
const imageFailed = ref(false)
const retryCount = ref(0)
let observer: IntersectionObserver | undefined

const previewUrl = computed(() => {
  const base = props.previewBaseUrl.trim()
  const path = props.camera.preview_path
  if (!base || !path.startsWith('/') || path.startsWith('//')) return ''

  try {
    const url = new URL(path, `${base.replace(/\/+$/, '')}/`)
    if (!['http:', 'https:'].includes(url.protocol)) return ''
    if (retryCount.value) url.searchParams.set('retrace_retry', String(retryCount.value))
    return url.toString()
  } catch {
    return ''
  }
})

const showPreview = computed(() =>
  props.camera.enabled && isVisible.value && !!previewUrl.value && !imageFailed.value,
)

const statusLabel = computed(() => {
  if (!props.camera.enabled) return '비활성'
  if (props.camera.status.stale) return '상태 지연'
  if (props.camera.status.state === 'online') return '온라인'
  return props.camera.status.state
})

const statusClass = computed(() => {
  if (!props.camera.enabled) return 'bg-stone-100 text-stone-600 ring-stone-200'
  if (props.camera.status.stale) return 'bg-amber-50 text-amber-700 ring-amber-200'
  if (props.camera.status.state === 'online') return 'bg-emerald-50 text-emerald-700 ring-emerald-200'
  return 'bg-rose-50 text-rose-700 ring-rose-200'
})

const fpsLabel = computed(() =>
  Number.isFinite(props.camera.status.fps) ? props.camera.status.fps.toFixed(1) : '—',
)

function retryPreview() {
  retryCount.value += 1
  imageFailed.value = false
}

watch(() => [props.previewBaseUrl, props.camera.preview_path], () => {
  imageFailed.value = false
  retryCount.value = 0
})

onMounted(() => {
  if (!previewElement.value) return
  observer = new IntersectionObserver(([entry]) => {
    isVisible.value = entry?.isIntersecting ?? false
  }, { rootMargin: '120px 0px' })
  observer.observe(previewElement.value)
})

onUnmounted(() => observer?.disconnect())
</script>

<template>
  <Card class="gap-0 overflow-hidden border-0 bg-white py-0 shadow-[0_8px_30px_rgba(31,41,55,0.05)] ring-1 ring-slate-200/80">
    <div ref="previewElement" class="relative aspect-video overflow-hidden bg-slate-950">
      <img
        v-if="showPreview"
        :key="retryCount"
        :src="previewUrl"
        :alt="`${camera.name} 실시간 미리보기`"
        class="h-full w-full object-contain"
        @error="imageFailed = true"
      >
      <div v-else class="flex h-full flex-col items-center justify-center gap-3 px-5 text-center text-slate-300">
        <div class="flex h-11 w-11 items-center justify-center rounded-full border border-white/15 bg-white/5">
          <span class="text-xl">◉</span>
        </div>
        <template v-if="!camera.enabled">
          <p class="text-sm">비활성 카메라</p>
        </template>
        <template v-else-if="imageFailed">
          <p class="text-sm">미리보기를 불러오지 못했습니다</p>
          <Button size="sm" variant="secondary" @click="retryPreview">다시 시도</Button>
        </template>
        <template v-else-if="!previewUrl">
          <p class="text-sm">Preview 주소를 확인하세요</p>
        </template>
        <template v-else>
          <p class="text-sm">미리보기 대기 중</p>
        </template>
      </div>
      <span class="absolute bottom-3 left-3 rounded-md bg-black/55 px-2 py-1 text-[11px] font-semibold tracking-[0.18em] text-white backdrop-blur-sm">
        LIVE · SOURCE {{ camera.source_id }}
      </span>
    </div>

    <CardHeader class="gap-3 px-5 pt-5 pb-3">
      <div class="flex items-start justify-between gap-3">
        <div class="min-w-0">
          <p class="mb-1 text-xs font-medium text-slate-500">{{ camera.floor }}층 · {{ camera.camera_id }}</p>
          <CardTitle class="truncate text-base font-semibold tracking-tight text-slate-900">{{ camera.name }}</CardTitle>
        </div>
        <Badge variant="outline" :class="['shrink-0 border-0 px-2.5 py-1 text-xs font-semibold ring-1', statusClass]">
          <span class="mr-1.5 h-1.5 w-1.5 rounded-full bg-current" />
          {{ statusLabel }}
        </Badge>
      </div>
    </CardHeader>

    <CardContent class="px-5 pb-5">
      <div class="flex items-center justify-between border-t border-slate-100 pt-3 text-xs text-slate-500">
        <span>프레임 속도</span>
        <span class="font-mono text-sm font-semibold text-slate-800">{{ fpsLabel }} <span class="font-sans text-xs font-normal text-slate-500">FPS</span></span>
      </div>
      <details v-if="camera.status.last_error" class="group mt-3 border-t border-slate-100 pt-3 text-xs text-slate-500">
        <summary class="cursor-pointer select-none">최근 오류 기록</summary>
        <p class="mt-2 break-all font-mono text-slate-600">{{ camera.status.last_error }}</p>
      </details>
    </CardContent>
  </Card>
</template>
