<script setup lang="ts">
import { useIntervalFn, useResizeObserver } from '@vueuse/core'
import LocaleToggle from '~/components/LocaleToggle.vue'
import { useI18n } from '~/composables/useI18n'

const route = useRoute()
const { locale, dateLocale, t } = useI18n()

// The parking editor (/cameras/:id/parking) belongs to the parking section.
const items = computed(() => [
  { to: '/parking', label: t('Parking'), shortLabel: t('Parking'), match: (path: string) => path === '/parking' || /^\/cameras\/[^/]+\/parking\/?$/.test(path) },
  { to: '/datasets', label: t('AI Datasets'), shortLabel: t('Datasets'), match: (path: string) => path === '/datasets' || path.startsWith('/datasets/') },
  { to: '/training', label: t('AI Training'), shortLabel: t('Train'), match: (path: string) => path === '/training' },
  { to: '/monitoring', label: t('CCTV Monitoring'), shortLabel: t('Monitoring'), match: (path: string) => path === '/monitoring' },
])

// Sliding active pill: moves on click (before the route resolves) and follows the route after.
const activeIndex = computed(() => items.value.findIndex((item) => item.match(route.path)))
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
// Labels change width with the language.
watch(locale, () => nextTick(measure))
// Also catches the web font swapping in and the responsive padding change.
useResizeObserver(menuElement, measure)
onMounted(() => {
  measure()
  requestAnimationFrame(() => { animate.value = true })
})

const now = ref(new Date())
useIntervalFn(() => { now.value = new Date() }, 1000)
const timeLabel = computed(() => now.value.toLocaleTimeString(locale.value === 'ko' ? 'ko-KR' : 'en-GB', { hour: '2-digit', minute: '2-digit', hour12: false }))
const dateLabel = computed(() => now.value.toLocaleDateString(dateLocale.value, { weekday: 'short', month: 'short', day: 'numeric' }))
</script>

<template>
  <header class="sticky top-0 z-40 px-3 pt-3 sm:px-5">
    <nav
      :aria-label="t('Main')"
      class="mx-auto grid h-14 max-w-[1500px] grid-cols-[auto_1fr] items-center gap-3 rounded-2xl bg-white/[0.03] px-2.5 shadow-[0_10px_40px_-16px_rgb(0_0_0/0.45)] backdrop-blur-xl sm:grid-cols-[1fr_auto_1fr] sm:px-3"
    >
      <NuxtLink to="/parking" class="flex items-center gap-2 justify-self-start rounded-lg px-1.5 py-1 outline-none focus-visible:ring-2 focus-visible:ring-ring/50">
        <span class="text-lg font-bold tracking-[-0.04em]">Re<span class="font-light">Trace</span></span>
      </NuxtLink>

      <ul ref="menuElement" class="relative flex items-center gap-0.5 sm:gap-1 justify-self-end rounded-full bg-white/[0.05] p-1 sm:justify-self-center">
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
              'flex h-8 items-center gap-1.5 rounded-full px-2 text-xs sm:px-3.5 sm:text-sm font-medium whitespace-nowrap outline-none transition-colors duration-300 focus-visible:ring-2 focus-visible:ring-ring/50',
              index === selectedIndex
                ? 'text-foreground'
                : 'text-muted-foreground hover:text-foreground',
            ]"
            @click="selectedIndex = index"
          >
            <span class="sm:hidden">{{ item.shortLabel }}</span><span class="hidden sm:inline">{{ item.label }}</span>
          </NuxtLink>
        </li>
      </ul>

      <!-- Phones have no room beside the menu; the toggle moves below the bar there. -->
      <div class="hidden items-center gap-3 justify-self-end sm:flex">
        <ClientOnly>
          <p class="hidden items-baseline gap-2 lg:flex">
            <span class="text-xs text-muted-foreground">{{ dateLabel }}</span>
            <time class="font-mono text-sm font-semibold tabular-nums">{{ timeLabel }}</time>
          </p>
        </ClientOnly>
        <LocaleToggle />
      </div>
    </nav>
    <div class="mx-auto mt-1.5 flex max-w-[1500px] justify-end sm:hidden">
      <LocaleToggle />
    </div>
  </header>
</template>
