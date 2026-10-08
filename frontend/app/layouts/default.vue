<script setup lang="ts">
import AppNav from '~/components/AppNav.vue'
import LiquidEtherBackground from '~/components/LiquidEtherBackground.vue'

const year = new Date().getFullYear()
const etherColors = ['#5bb7b1', '#587fa3', '#857bb0']
</script>

<template>
  <div class="relative flex min-h-screen flex-col bg-background text-foreground">
    <!-- Lives in the layout so it keeps running across page changes. -->
    <div class="pointer-events-none absolute inset-x-0 top-0 h-[36rem] overflow-hidden">
      <LiquidEtherBackground :colors="etherColors" :resolution="0.45" />
      <div class="absolute inset-0 bg-[radial-gradient(140%_100%_at_50%_0%,transparent_50%,var(--background)_100%)]" />
      <div class="absolute inset-x-0 bottom-0 h-56 bg-gradient-to-b from-transparent to-background" />
    </div>
    <AppNav />
    <div class="relative flex-1">
      <slot />
    </div>
    <footer class="relative pb-8 text-center text-[11px] tracking-wide text-muted-foreground/60">
      © {{ year }} ReTrace · made by ohjoo
    </footer>
  </div>
</template>
