<script setup lang="ts">
import { TransitionPresets, useTransition } from '@vueuse/core'
import { Cctv, CircleAlert, RefreshCw, Search, SearchX, X } from '@lucide/vue'
import CameraCard from '~/components/CameraCard.vue'
import LiquidEtherBackground from '~/components/LiquidEtherBackground.vue'
import { Alert, AlertAction, AlertDescription, AlertTitle } from '~/components/ui/alert'
import { Badge } from '~/components/ui/badge'
import { Button } from '~/components/ui/button'
import { Card, CardContent } from '~/components/ui/card'
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from '~/components/ui/empty'
import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupInput } from '~/components/ui/input-group'
import { Separator } from '~/components/ui/separator'
import { Skeleton } from '~/components/ui/skeleton'
import { tabsListVariants } from '~/components/ui/tabs'
import { Tooltip, TooltipContent, TooltipTrigger } from '~/components/ui/tooltip'
import { useCameras } from '~/composables/useCameras'
import { useRealtimePreview } from '~/composables/useRealtimePreview'
import { cn } from '~/lib/utils'

useHead({ title: 'ReTrace · Camera Dashboard' })

const year = new Date().getFullYear()
const etherColors = ['#5bb7b1', '#587fa3', '#857bb0']

const config = useRuntimeConfig()
const { cameras: allCameras, loading, refreshing, error, refresh } = useCameras()
const cameras = computed(() => allCameras.value.filter((camera) => Number(camera.floor) !== 2))
const { client: realtimeClient, connection, statuses } = useRealtimePreview(config.public.previewVideoFps)

// Floors to show; empty means every floor ('전체').
const selectedFloors = ref<string[]>([])
const query = ref('')

// Basement labels (B1, B2, ...) sort below ground floors: B2, B1, 1, 3, 4, ...
function floorOrder(floor: number | string) {
  const basement = /^B(\d+)$/i.exec(String(floor))
  return basement ? -Number(basement[1]) : Number(floor)
}

const sortedCameras = computed(() => [...cameras.value].sort((a, b) =>
  floorOrder(a.floor) - floorOrder(b.floor) || a.name.localeCompare(b.name, 'ko'),
))
const floors = computed(() => [...new Set(sortedCameras.value.map((camera) => String(camera.floor)))])

watch(floors, (available) => {
  const kept = selectedFloors.value.filter((floor) => available.includes(floor))
  if (kept.length !== selectedFloors.value.length) selectedFloors.value = kept
})

function toggleFloor(floor: string) {
  const next = selectedFloors.value.includes(floor)
    ? selectedFloors.value.filter((item) => item !== floor)
    : [...selectedFloors.value, floor]
  // Selecting every floor is the same as '전체'.
  selectedFloors.value = next.length === floors.value.length ? [] : next
}

const filteredCameras = computed(() => {
  const keyword = query.value.trim().toLowerCase()
  return sortedCameras.value.filter((camera) =>
    (!selectedFloors.value.length || selectedFloors.value.includes(String(camera.floor)))
    && (!keyword || camera.name.toLowerCase().includes(keyword)),
  )
})

const floorGroups = computed(() => {
  const groups = new Map<string, typeof filteredCameras.value>()
  for (const camera of filteredCameras.value) {
    const key = String(camera.floor)
    groups.set(key, [...(groups.get(key) ?? []), camera])
  }
  return [...groups].map(([floor, items]) => ({ floor, items }))
})

const isFiltered = computed(() => !!selectedFloors.value.length || !!query.value.trim())

// One dot per camera, grouped by floor, for the hero status map.
const floorStatus = computed(() => floors.value.map((floor) => ({
  floor,
  cameras: sortedCameras.value
    .filter((camera) => String(camera.floor) === floor)
    .map((camera) => {
      const tone: keyof typeof dotClass = !camera.enabled ? 'disabled' : !camera.status.stale && camera.status.state === 'online' ? 'online' : 'warning'
      const label = tone === 'disabled' ? '비활성' : tone === 'online' ? '온라인' : camera.status.stale ? '상태 지연' : camera.status.state
      return { id: camera.camera_id, name: camera.name, tone, label }
    }),
})).map((group) => ({
  ...group,
  online: group.cameras.filter((camera) => camera.tone === 'online').length,
  warning: group.cameras.some((camera) => camera.tone === 'warning'),
})))

// Floor cards act as a shortcut: show only that floor, or everything again on a second click.
function focusFloor(floor: string) {
  selectedFloors.value = selectedFloors.value.length === 1 && selectedFloors.value[0] === floor ? [] : [floor]
}

const dotClass = {
  online: 'bg-emerald-300/75 shadow-[0_0_8px_rgba(110,231,183,0.45)]',
  warning: 'bg-amber-300/85 shadow-[0_0_8px_rgba(252,211,77,0.5)]',
  disabled: 'ring-1 ring-inset ring-white/25',
} as const

const onlineCount = computed(() => cameras.value.filter((camera) =>
  camera.enabled && !camera.status.stale && camera.status.state === 'online',
).length)

function useCountUp(source: () => number) {
  const output = useTransition(source, { duration: 900, transition: TransitionPresets.easeOutCubic })
  return computed(() => Math.round(output.value))
}

const animatedTotal = useCountUp(() => cameras.value.length)
const animatedOnline = useCountUp(() => onlineCount.value)

function enterDelay(index: number, step = 70, base = 0) {
  return { animationDelay: `${base + Math.min(index, 12) * step}ms` }
}


const floorButtons = computed(() => [
  { value: null, label: '전체', count: cameras.value.length, active: !selectedFloors.value.length },
  ...floors.value.map((floor) => ({
    value: floor,
    label: floor + '층',
    count: cameras.value.filter((camera) => String(camera.floor) === floor).length,
    active: selectedFloors.value.includes(floor),
  })),
])

function onFloorButton(value: string | null) {
  if (value === null) selectedFloors.value = []
  else toggleFloor(value)
}

function resetFilters() {
  selectedFloors.value = []
  query.value = ''
}
</script>

<template>
  <main class="relative min-h-screen bg-background text-foreground">
    <header class="relative isolate overflow-hidden">
      <LiquidEtherBackground
        :colors="etherColors"
        :resolution="0.45"
      />
      <div class="pointer-events-none absolute inset-0 bg-[radial-gradient(140%_100%_at_50%_0%,transparent_50%,var(--background)_100%)]" />
      <div class="pointer-events-none absolute inset-x-0 bottom-0 h-32 bg-gradient-to-b from-transparent to-background" />

      <div class="relative mx-auto max-w-[1500px] px-5 sm:px-8 lg:px-10">

        <div class="flex flex-col items-center pt-20 pb-16 text-center sm:pt-28 sm:pb-24">
          <h1 class="sr-only">카메라 대시보드</h1>
          <Skeleton v-if="loading" class="h-28 w-72 bg-white/[0.06] sm:h-40 sm:w-[28rem]" />
          <p
            v-else
            :style="enterDelay(1, 80, 80)"
            class="flex items-baseline font-bold leading-[0.9] tracking-[-0.06em] tabular-nums motion-safe:animate-fade-up"
            :aria-label="'온라인 ' + onlineCount + '대, 전체 ' + cameras.length + '대'"
          >
            <span class="glass-text glass-text-online text-[7.5rem] sm:text-[12rem]">{{ animatedOnline }}</span>
            <span class="glass-text ml-3 text-6xl sm:ml-5 sm:text-8xl">/ {{ animatedTotal }}</span>
          </p>

          <ul
            v-if="!loading && floorStatus.length"
            :style="enterDelay(2, 80, 80)"
            class="mt-10 flex flex-wrap items-center justify-center gap-x-1 gap-y-2 motion-safe:animate-fade-up"
            aria-label="층별 카메라 상태"
          >
            <li v-for="group in floorStatus" :key="group.floor">
              <button
                type="button"
                :aria-pressed="selectedFloors.length === 1 && selectedFloors[0] === group.floor"
                :aria-label="group.floor + '층, 전체 ' + group.cameras.length + '대 중 ' + group.online + '대 온라인. 이 층만 보기'"
                :class="[
                  'flex items-center gap-2.5 rounded-full px-3 py-1.5 outline-none transition-colors duration-300 hover:bg-white/[0.06] focus-visible:ring-2 focus-visible:ring-white/25',
                  selectedFloors.length === 1 && selectedFloors[0] === group.floor ? 'bg-white/[0.08]' : '',
                ]"
                @click="focusFloor(group.floor)"
              >
                <span class="text-xs font-semibold tabular-nums text-foreground/60">{{ group.floor }}F</span>
                <span class="flex gap-1">
                  <Tooltip v-for="camera in group.cameras" :key="camera.id">
                    <TooltipTrigger as-child>
                      <span :class="['size-1.5 rounded-full transition-colors duration-500', dotClass[camera.tone]]" />
                    </TooltipTrigger>
                    <TooltipContent side="top">{{ camera.name }} · {{ camera.label }}</TooltipContent>
                  </Tooltip>
                </span>
              </button>
            </li>
          </ul>
        </div>
      </div>
    </header>

    <div class="sticky top-0 z-30 border-y border-border/60 bg-background/75 backdrop-blur-xl">
      <div class="mx-auto flex max-w-[1500px] flex-col gap-3 px-5 py-3 sm:px-8 md:flex-row md:items-center md:justify-between lg:px-10">
        <div class="-mx-1 min-w-0 overflow-x-auto px-1 [scrollbar-width:none]">
          <div role="group" aria-label="층 선택" :class="cn(tabsListVariants(), 'h-9 gap-0.5')">
            <button
              v-for="(item, index) in floorButtons"
              :key="item.value ?? 'all'"
              type="button"
              :aria-pressed="item.active"
              :data-active="item.active ? '' : undefined"
              :style="enterDelay(index, 50, 200)"
              class="group/tab inline-flex h-full items-center justify-center gap-1.5 whitespace-nowrap rounded-md border border-transparent px-3 text-sm font-medium text-muted-foreground outline-none transition-colors duration-300 hover:text-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 data-active:bg-white/[0.08] data-active:text-foreground motion-safe:animate-fade-up"
              @click="onFloorButton(item.value)"
            >
              {{ item.label }}
              <span class="text-xs tabular-nums text-muted-foreground transition-colors duration-300 group-data-active/tab:text-foreground/70">{{ item.count }}</span>
            </button>
          </div>
        </div>

        <div class="flex items-center gap-2">
          <InputGroup class="h-9 transition-[border-color,box-shadow] duration-200 md:w-72 has-[[data-slot=input-group-control]:focus-visible]:border-white/25 has-[[data-slot=input-group-control]:focus-visible]:ring-2 has-[[data-slot=input-group-control]:focus-visible]:ring-white/[0.06]">
            <InputGroupAddon>
              <Search />
            </InputGroupAddon>
            <InputGroupInput v-model="query" placeholder="카메라 이름 검색" aria-label="카메라 검색" />
            <InputGroupAddon v-if="query" align="inline-end">
              <InputGroupButton size="icon-xs" aria-label="검색어 지우기" @click="query = ''">
                <X />
              </InputGroupButton>
            </InputGroupAddon>
          </InputGroup>
        </div>
      </div>
    </div>

    <section aria-label="카메라 목록" class="mx-auto max-w-[1500px] px-5 pt-8 pb-16 sm:px-8 lg:px-10">
      <Alert v-if="error" variant="destructive" class="motion-safe:animate-fade-up mb-8 border-destructive/30 bg-destructive/10 px-4 py-3">
        <CircleAlert />
        <AlertTitle>카메라 목록을 확인할 수 없습니다</AlertTitle>
        <AlertDescription>{{ error }}</AlertDescription>
        <AlertAction class="top-1/2 -translate-y-1/2">
          <Button size="sm" variant="outline" :disabled="refreshing" @click="refresh">다시 시도</Button>
        </AlertAction>
      </Alert>

      <div v-if="loading" class="grid gap-5 sm:grid-cols-2 xl:grid-cols-3" aria-label="카메라 목록을 불러오는 중">
        <Card v-for="item in 6" :key="item" :style="enterDelay(item - 1)" class="gap-0 py-0 motion-safe:animate-fade-up">
          <Skeleton class="aspect-video w-full rounded-none" />
          <CardContent class="space-y-3 px-5 py-5">
            <Skeleton class="h-4 w-2/3" />
            <Skeleton class="h-3 w-1/3" />
          </CardContent>
        </Card>
      </div>

      <Empty v-else-if="!cameras.length && !error" class="border border-border bg-card/40 py-16 motion-safe:animate-fade-up">
        <EmptyHeader>
          <EmptyMedia variant="icon" class="size-12 rounded-xl">
            <Cctv class="size-6" />
          </EmptyMedia>
          <EmptyTitle class="text-base">표시할 카메라가 없습니다</EmptyTitle>
          <EmptyDescription>카메라가 연결되면 이곳에 미리보기가 표시됩니다.</EmptyDescription>
        </EmptyHeader>
        <EmptyContent>
          <Button variant="outline" :disabled="refreshing" @click="refresh">
            <RefreshCw :class="{ 'animate-spin': refreshing }" />
            새로고침
          </Button>
        </EmptyContent>
      </Empty>

      <Empty v-else-if="cameras.length && !filteredCameras.length" class="border border-border bg-card/40 py-16 motion-safe:animate-fade-up">
        <EmptyHeader>
          <EmptyMedia variant="icon" class="size-12 rounded-xl">
            <SearchX class="size-6" />
          </EmptyMedia>
          <EmptyTitle class="text-base">조건에 맞는 카메라가 없습니다</EmptyTitle>
          <EmptyDescription>층 필터나 검색어를 변경해 보세요.</EmptyDescription>
        </EmptyHeader>
        <EmptyContent>
          <Button variant="outline" @click="resetFilters">필터 초기화</Button>
        </EmptyContent>
      </Empty>

      <div v-else-if="filteredCameras.length" class="space-y-10">
        <section v-for="group in floorGroups" :key="group.floor" :aria-labelledby="'floor-' + group.floor">
          <div class="mb-4 flex items-center gap-3 motion-safe:animate-fade-up">
            <h2 :id="'floor-' + group.floor" class="text-lg font-semibold tracking-tight">{{ group.floor }}층</h2>
            <Badge variant="secondary" class="tabular-nums">{{ group.items.length }}</Badge>
            <Separator class="flex-1" />
          </div>
          <div class="grid gap-5 sm:grid-cols-2 xl:grid-cols-3">
            <div
              v-for="(camera, index) in group.items"
              :key="camera.camera_id"
              :style="enterDelay(index, 70, 80)"
              class="motion-safe:animate-fade-up"
            >
              <CameraCard
                :camera="camera"
                :realtime-client="realtimeClient"
                :connection="connection"
                :live-status="statuses[camera.camera_id]"
              />
            </div>
          </div>
        </section>
        <p v-if="isFiltered" class="text-center text-xs text-muted-foreground">
          {{ cameras.length }}대 중 {{ filteredCameras.length }}대 표시 중
        </p>
      </div>
    </section>

    <footer class="pb-8 text-center text-[11px] tracking-wide text-muted-foreground/60">
      © {{ year }} ReTrace · made by ohjoo
    </footer>
  </main>
</template>
