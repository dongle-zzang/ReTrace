<script setup lang="ts">
import { TooltipProvider } from '~/components/ui/tooltip'

useHead({ htmlAttrs: { class: 'dark', lang: 'en' } })
</script>

<template>
  <TooltipProvider :delay-duration="250">
    <NuxtLayout>
      <NuxtPage />
    </NuxtLayout>
  </TooltipProvider>
</template>
