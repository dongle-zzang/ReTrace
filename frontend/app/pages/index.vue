<script setup lang="ts">
import CameraCard from '~/components/CameraCard.vue'
import { Button } from '~/components/ui/button'
import { Skeleton } from '~/components/ui/skeleton'
import { useCameras } from '~/composables/useCameras'

useHead({ title: 'ReTrace · Camera Dashboard' })

const config = useRuntimeConfig()
const { cameras, loading, refreshing, error, refresh } = useCameras()

const sortedCameras = computed(() => [...cameras.value].sort((a, b) =>
  a.floor - b.floor || a.name.localeCompare(b.name, 'ko'),
))
const onlineCount = computed(() => cameras.value.filter((camera) =>
  camera.enabled && !camera.status.stale && camera.status.state === 'online',
).length)
const attentionCount = computed(() => cameras.value.filter((camera) =>
  camera.enabled && (camera.status.stale || camera.status.state !== 'online'),
).length)
const disabledCount = computed(() => cameras.value.filter((camera) => !camera.enabled).length)
</script>

<template>
  <main class="min-h-screen bg-[#f6f8fb] text-slate-900">
    <header class="relative overflow-hidden bg-[#101c2c] text-white">
      <div class="pointer-events-none absolute -top-36 right-[-4rem] h-96 w-96 rounded-full bg-cyan-400/10 blur-3xl" />
      <div class="pointer-events-none absolute -bottom-48 left-1/3 h-80 w-80 rounded-full bg-blue-500/10 blur-3xl" />
      <div class="relative mx-auto max-w-[1500px] px-5 py-6 sm:px-8 lg:px-10">
        <div class="flex items-center justify-between gap-5 border-b border-white/10 pb-6">
          <div class="flex items-center gap-3">
            <div class="flex h-10 w-10 items-center justify-center rounded-xl bg-cyan-400 text-[#101c2c] shadow-lg shadow-cyan-400/10">
              <span class="text-lg font-black">R</span>
            </div>
            <div>
              <p class="text-lg font-bold leading-none tracking-tight">ReTrace</p>
              <p class="mt-1 text-[10px] font-semibold uppercase tracking-[0.23em] text-slate-400">Camera Intelligence</p>
            </div>
          </div>
          <span class="hidden rounded-full border border-white/10 px-3 py-1.5 text-xs text-slate-300 sm:inline-flex">Live monitoring</span>
        </div>

        <div class="flex flex-col justify-between gap-6 py-9 md:flex-row md:items-end">
          <div>
            <p class="mb-2 text-xs font-semibold uppercase tracking-[0.2em] text-cyan-300">Overview</p>
            <h1 class="text-3xl font-bold tracking-tight sm:text-4xl">카메라 대시보드</h1>
            <p class="mt-3 text-sm text-slate-300">카메라 연결 상태와 실시간 미리보기를 한눈에 확인합니다.</p>
          </div>
          <div class="grid grid-cols-3 gap-3 sm:gap-4">
            <div class="min-w-24 rounded-xl border border-white/10 bg-white/5 px-4 py-3 backdrop-blur-sm">
              <p class="text-xs text-slate-400">온라인</p><p class="mt-1 text-2xl font-semibold text-emerald-300">{{ onlineCount }}</p>
            </div>
            <div class="min-w-24 rounded-xl border border-white/10 bg-white/5 px-4 py-3 backdrop-blur-sm">
              <p class="text-xs text-slate-400">확인 필요</p><p class="mt-1 text-2xl font-semibold text-amber-300">{{ attentionCount }}</p>
            </div>
            <div class="min-w-24 rounded-xl border border-white/10 bg-white/5 px-4 py-3 backdrop-blur-sm">
              <p class="text-xs text-slate-400">비활성</p><p class="mt-1 text-2xl font-semibold text-slate-200">{{ disabledCount }}</p>
            </div>
          </div>
        </div>
      </div>
    </header>

    <section class="mx-auto max-w-[1500px] px-5 py-8 sm:px-8 lg:px-10">
      <div class="mb-6 flex flex-wrap items-center justify-between gap-4">
        <div>
          <h2 class="text-xl font-semibold tracking-tight">전체 카메라 <span class="ml-1 font-mono text-slate-400">{{ cameras.length }}</span></h2>
          <p class="mt-1 text-sm text-slate-500">상태는 10초마다 자동 갱신됩니다.</p>
        </div>
        <Button variant="outline" class="bg-white" :disabled="refreshing" @click="refresh">
          {{ refreshing ? '새로고침 중…' : '새로고침' }}
        </Button>
      </div>

      <div v-if="error" role="alert" class="mb-6 rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
        {{ error }}
      </div>

      <div v-if="loading" class="grid gap-5 sm:grid-cols-2 xl:grid-cols-3">
        <div v-for="item in 6" :key="item" class="overflow-hidden rounded-xl bg-white shadow-sm ring-1 ring-slate-200">
          <Skeleton class="aspect-video w-full rounded-none" />
          <div class="space-y-3 p-5"><Skeleton class="h-4 w-2/3" /><Skeleton class="h-4 w-1/3" /></div>
        </div>
      </div>
      <div v-else-if="!cameras.length" class="rounded-2xl border border-dashed border-slate-300 bg-white px-6 py-20 text-center">
        <p class="text-lg font-semibold text-slate-800">표시할 카메라가 없습니다</p>
        <p class="mt-2 text-sm text-slate-500">Backend의 카메라 목록을 확인하세요.</p>
      </div>
      <div v-else class="grid gap-5 sm:grid-cols-2 xl:grid-cols-3">
        <CameraCard
          v-for="camera in sortedCameras"
          :key="camera.camera_id"
          :camera="camera"
          :preview-base-url="config.public.previewBaseUrl"
        />
      </div>
    </section>
  </main>
</template>
