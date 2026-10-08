<script setup lang="ts">
import { Cctv, CircleAlert, RefreshCw, Search, SearchX, X } from '@lucide/vue'
import CameraCard from '~/components/CameraCard.vue'
import { Alert, AlertAction, AlertDescription, AlertTitle } from '~/components/ui/alert'
import { Badge } from '~/components/ui/badge'
import { Button } from '~/components/ui/button'
import { Card, CardContent } from '~/components/ui/card'
import { Empty, EmptyContent, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from '~/components/ui/empty'
import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupInput } from '~/components/ui/input-group'
import { Separator } from '~/components/ui/separator'
import { Skeleton } from '~/components/ui/skeleton'
import { tabsListVariants } from '~/components/ui/tabs'
import { useCameras } from '~/composables/useCameras'
import { useRealtimePreview } from '~/composables/useRealtimePreview'
import { compareCameras, floorLabel } from '~/lib/cameraSort'
import { cn } from '~/lib/utils'

useHead({ title: 'ReTrace · CCTV Monitoring' })

const config = useRuntimeConfig()
const { cameras: allCameras, loading, refreshing, error, refresh } = useCameras()
const cameras = computed(() => allCameras.value.filter((camera) => Number(camera.floor) !== 2))
const { client: realtimeClient, connection, statuses } = useRealtimePreview(config.public.previewVideoFps)

// Floors to show; empty means every floor ('All').
const selectedFloors = ref<string[]>([])
const query = ref('')

const sortedCameras = computed(() => [...cameras.value].sort(compareCameras))
const floors = computed(() => [...new Set(sortedCameras.value.map((camera) => String(camera.floor)))])

watch(floors, (available) => {
  const kept = selectedFloors.value.filter((floor) => available.includes(floor))
  if (kept.length !== selectedFloors.value.length) selectedFloors.value = kept
})

function toggleFloor(floor: string) {
  const next = selectedFloors.value.includes(floor)
    ? selectedFloors.value.filter((item) => item !== floor)
    : [...selectedFloors.value, floor]
  // Selecting every floor is the same as 'All'.
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

function enterDelay(index: number, step = 70, base = 0) {
  return { animationDelay: `${base + Math.min(index, 12) * step}ms` }
}

const floorButtons = computed(() => [
  { value: null, label: 'All', count: cameras.value.length, active: !selectedFloors.value.length },
  ...floors.value.map((floor) => ({
    value: floor,
    label: floorLabel(floor),
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
  <main class="text-foreground">
    <div class="mx-auto max-w-[1500px] px-5 pt-10 sm:px-8 sm:pt-12 lg:px-10">
      <div class="flex flex-col gap-5 md:flex-row md:items-end md:justify-between motion-safe:animate-fade-up">
        <div class="min-w-0">
          <h1 class="text-3xl font-semibold tracking-tight sm:text-4xl">CCTV Monitoring</h1>
          <p class="mt-2 text-sm text-muted-foreground">Live video from every camera, grouped by floor.</p>
        </div>
        <InputGroup class="h-9 transition-[border-color,box-shadow] duration-200 md:w-72 has-[[data-slot=input-group-control]:focus-visible]:border-white/25 has-[[data-slot=input-group-control]:focus-visible]:ring-2 has-[[data-slot=input-group-control]:focus-visible]:ring-white/[0.06]">
          <InputGroupAddon>
            <Search />
          </InputGroupAddon>
          <InputGroupInput v-model="query" placeholder="Search cameras" aria-label="Search cameras" />
          <InputGroupAddon v-if="query" align="inline-end">
            <InputGroupButton size="icon-xs" aria-label="Clear search" @click="query = ''">
              <X />
            </InputGroupButton>
          </InputGroupAddon>
        </InputGroup>
      </div>

      <div class="-mx-1 mt-6 min-w-0 overflow-x-auto px-1 [scrollbar-width:none]">
        <div role="group" aria-label="Floors" :class="cn(tabsListVariants(), 'h-9 gap-0.5 border border-white/[0.08] bg-white/[0.04] backdrop-blur-xl')">
          <button
            v-for="(item, index) in floorButtons"
            :key="item.value ?? 'all'"
            type="button"
            :aria-pressed="item.active"
            :data-active="item.active ? '' : undefined"
            :style="enterDelay(index, 50, 120)"
            class="group/tab inline-flex h-full items-center justify-center gap-1.5 whitespace-nowrap rounded-md border border-transparent px-3 text-sm font-medium text-muted-foreground outline-none transition-colors duration-300 hover:text-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 data-active:glass-active data-active:text-foreground motion-safe:animate-fade-up"
            @click="onFloorButton(item.value)"
          >
            {{ item.label }}
            <span class="text-xs tabular-nums text-muted-foreground transition-colors duration-300 group-data-active/tab:text-foreground/70">{{ item.count }}</span>
          </button>
        </div>
      </div>
    </div>

    <section aria-label="Cameras" class="mx-auto max-w-[1500px] px-5 pt-6 pb-16 sm:px-8 lg:px-10">
      <Alert v-if="error" variant="destructive" class="motion-safe:animate-fade-up mb-8 border-destructive/30 bg-destructive/10 px-4 py-3">
        <CircleAlert />
        <AlertTitle>Couldn't load cameras</AlertTitle>
        <AlertDescription>{{ error }}</AlertDescription>
        <AlertAction class="top-1/2 -translate-y-1/2">
          <Button size="sm" variant="outline" :disabled="refreshing" @click="refresh">Retry</Button>
        </AlertAction>
      </Alert>

      <div v-if="loading" class="grid gap-5 sm:grid-cols-2 xl:grid-cols-3" aria-label="Loading cameras">
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
          <EmptyTitle class="text-base">No cameras yet</EmptyTitle>
          <EmptyDescription>Previews appear here once cameras are connected.</EmptyDescription>
        </EmptyHeader>
        <EmptyContent>
          <Button variant="outline" :disabled="refreshing" @click="refresh">
            <RefreshCw :class="{ 'animate-spin': refreshing }" />
            Refresh
          </Button>
        </EmptyContent>
      </Empty>

      <Empty v-else-if="cameras.length && !filteredCameras.length" class="border border-border bg-card/40 py-16 motion-safe:animate-fade-up">
        <EmptyHeader>
          <EmptyMedia variant="icon" class="size-12 rounded-xl">
            <SearchX class="size-6" />
          </EmptyMedia>
          <EmptyTitle class="text-base">No matching cameras</EmptyTitle>
          <EmptyDescription>Try another floor or search term.</EmptyDescription>
        </EmptyHeader>
        <EmptyContent>
          <Button variant="outline" @click="resetFilters">Reset filters</Button>
        </EmptyContent>
      </Empty>

      <div v-else-if="filteredCameras.length" class="space-y-10">
        <section v-for="group in floorGroups" :key="group.floor" :aria-labelledby="'floor-' + group.floor">
          <div class="mb-4 flex items-center gap-3 motion-safe:animate-fade-up">
            <h2 :id="'floor-' + group.floor" class="text-lg font-semibold tracking-tight">{{ floorLabel(group.floor) }}</h2>
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
          Showing {{ filteredCameras.length }} of {{ cameras.length }} cameras
        </p>
      </div>
    </section>

  </main>
</template>
