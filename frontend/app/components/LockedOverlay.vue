<script setup lang="ts">
import { Lock } from '@lucide/vue'
</script>

<template>
  <!-- Visual only: the locked card is made inert by its parent, so nothing behind can be focused or clicked. -->
  <div class="pointer-events-none absolute inset-0 z-10 grid place-items-center rounded-[inherit] bg-background/20 backdrop-blur-[6px]" aria-hidden="true">
    <Lock class="size-6 text-foreground/80" />
  </div>
</template>
