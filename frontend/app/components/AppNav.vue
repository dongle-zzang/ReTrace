<script setup lang="ts">
import { useIntervalFn, useResizeObserver } from '@vueuse/core'

const route = useRoute()

// The parking editor (/cameras/:id/parking) belongs to the parking section.
const items = [
  { to: '/', label: 'CCTV Monitoring', match: (path: string) => path === '/' },
  { to: '/parking', label: 'Parking', match: (path: string) => path === '/parking' || /^\/cameras\/[^/]+\/parking\/?$/.test(path) },
]

// Sliding active pill: moves on click (before the route resolves) and follows the route after.
const activeIndex = computed(() => items.findIndex((item) => item.match(route.path)))
const selectedIndex = ref(activeIndex.value)
watch(activeIndex, (index) => { selectedIndex.value = index })

const menuElement = ref<HTMLElement | null>(null)
const itemElements = ref<HTMLElement[]>([])
const indicator = ref<{ left: number; width: number } | null>(null)
// No slide on first paint; only between tabs.
const animate = ref(false)

function measure() {
  const element = itemElements.value[selectedIndex.value]
  indicator.value = element ? { left: element.offsetLeft, width: element.offsetWidth } : null
}

watch(selectedIndex, () => nextTick(measure))
// Also catches the web font swapping in and the responsive padding change.
useResizeObserver(menuElement, measure)
onMounted(() => {
  measure()
  requestAnimationFrame(() => { animate.value = true })
})

const now = ref(new Date())
useIntervalFn(() => { now.value = new Date() }, 1000)
const timeLabel = computed(() => now.value.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', hour12: false }))
const dateLabel = computed(() => now.value.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' }))
</script>

<template>
  <header class="sticky top-0 z-40 px-3 pt-3 sm:px-5">
    <nav
      aria-label="Main"
      class="mx-auto grid h-14 max-w-[1500px] grid-cols-[auto_1fr] items-center gap-3 rounded-2xl bg-white/[0.03] px-2.5 shadow-[0_10px_40px_-16px_rgb(0_0_0/0.45)] backdrop-blur-xl sm:grid-cols-[1fr_auto_1fr] sm:px-3"
    >
      <NuxtLink to="/" class="flex items-center gap-2 justify-self-start rounded-lg px-1.5 py-1 outline-none focus-visible:ring-2 focus-visible:ring-ring/50">
        <span class="text-lg font-bold tracking-[-0.04em]">Re<span class="font-light">Trace</span></span>
      </NuxtLink>

      <ul ref="menuElement" class="relative flex items-center gap-1 justify-self-end rounded-full bg-white/[0.05] p-1 sm:justify-self-center">
        <span
          v-if="indicator"
          aria-hidden="true"
          :class="['glass-active pointer-events-none absolute inset-y-1 left-0 rounded-full', animate ? 'transition-[translate,width] duration-500 ease-[cubic-bezier(0.22,1,0.36,1)]' : '']"
          :style="{ translate: indicator.left + 'px 0', width: indicator.width + 'px' }"
        />
        <li v-for="(item, index) in items" :key="item.to" ref="itemElements" class="relative">
          <NuxtLink
            :to="item.to"
            :aria-current="item.match(route.path) ? 'page' : undefined"
            :class="[
              'flex h-8 items-center gap-1.5 rounded-full px-3 text-sm sm:px-3.5 font-medium whitespace-nowrap outline-none transition-colors duration-300 focus-visible:ring-2 focus-visible:ring-ring/50',
              index === selectedIndex
                ? 'text-foreground'
                : 'text-muted-foreground hover:text-foreground',
            ]"
            @click="selectedIndex = index"
          >
            {{ item.label }}
          </NuxtLink>
        </li>
      </ul>

      <ClientOnly>
        <p class="hidden items-baseline gap-2 justify-self-end pr-2 sm:flex">
          <span class="text-xs text-muted-foreground">{{ dateLabel }}</span>
          <time class="font-mono text-sm font-semibold tabular-nums">{{ timeLabel }}</time>
        </p>
      </ClientOnly>
    </nav>
  </header>
</template>
