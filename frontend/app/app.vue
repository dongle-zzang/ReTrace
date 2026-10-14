<script setup lang="ts">
import { TooltipProvider } from '~/components/ui/tooltip'
import { useI18n } from '~/composables/useI18n'

const { locale } = useI18n()
useHead({ htmlAttrs: { class: 'dark', lang: locale } })
</script>

<template>
  <TooltipProvider :delay-duration="250">
    <NuxtLayout>
      <NuxtPage />
    </NuxtLayout>
  </TooltipProvider>
</template>
