<script setup lang="ts">
import { TooltipProvider } from '~/components/ui/tooltip'

useHead({ htmlAttrs: { class: 'dark', lang: 'ko' } })
</script>

<template>
  <TooltipProvider :delay-duration="250">
    <NuxtPage />
  </TooltipProvider>
</template>
