<script setup lang="ts">
import { useI18n } from '~/composables/useI18n'
import { LOCALES } from '~/lib/i18n'

const { locale, setLocale, t } = useI18n()
// Each option names itself in its own language.
const names = { ko: '한국어', en: 'English' } as const
const short = { ko: '한', en: 'EN' } as const
</script>

<template>
  <div role="group" :aria-label="t('Language')" class="flex items-center rounded-full bg-white/[0.05] p-0.5">
    <button
      v-for="option in LOCALES"
      :key="option"
      type="button"
      :lang="option"
      :aria-pressed="locale === option"
      :aria-label="names[option]"
      :title="names[option]"
      :class="[
        'h-7 min-w-8 rounded-full px-2 text-xs font-medium outline-none transition-colors duration-300 focus-visible:ring-2 focus-visible:ring-ring/50',
        locale === option ? 'glass-active text-foreground' : 'text-muted-foreground hover:text-foreground',
      ]"
      @click="setLocale(option)"
    >
      {{ short[option] }}
    </button>
  </div>
</template>
