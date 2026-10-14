<script setup lang="ts">
import { ChevronDown } from '@lucide/vue'
import { SelectContent, SelectItem, SelectItemText, SelectPortal, SelectRoot, SelectTrigger, SelectViewport } from 'reka-ui'
import { useI18n } from '~/composables/useI18n'
defineOptions({ inheritAttrs: false })
const props = withDefaults(defineProps<{
  modelValue: string; options: { value: string; label: string; disabled?: boolean }[]
  label: string; placeholder?: string; disabled?: boolean
}>(), { disabled: false })
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()
const { t } = useI18n()
const selectedLabel = computed(() => props.options.find(item => item.value === props.modelValue)?.label ?? props.placeholder ?? t('Select an option'))
</script>

<template>
  <SelectRoot :model-value="modelValue" :disabled="disabled" @update:model-value="emit('update:modelValue', String($event))">
    <SelectTrigger v-bind="$attrs" :aria-label="label" class="group flex h-9 w-52 max-w-full min-w-0 items-center justify-between gap-2 rounded-lg border border-white/10 bg-white/[0.04] px-3 text-left text-sm shadow-sm outline-none transition-colors hover:border-white/20 hover:bg-white/[0.07] focus-visible:border-primary/50 focus-visible:ring-2 focus-visible:ring-primary/30 data-[state=open]:border-primary/40 data-[state=open]:bg-white/[0.07] disabled:cursor-not-allowed disabled:opacity-50">
      <span class="truncate font-medium text-foreground">{{ selectedLabel }}</span>
      <ChevronDown aria-hidden="true" class="size-4 shrink-0 text-muted-foreground transition-transform duration-150 group-data-[state=open]:rotate-180" />
    </SelectTrigger>
    <SelectPortal>
      <SelectContent :body-lock="false" position="popper" align="end" :side-offset="8" class="z-[60] max-h-[min(24rem,calc(100vh-2rem))] w-[var(--reka-select-trigger-width)] max-w-[calc(100vw-1.5rem)] overflow-hidden rounded-xl border border-white/10 bg-popover/95 p-1.5 text-popover-foreground shadow-xl shadow-black/30 ring-1 ring-white/10 backdrop-blur-xl outline-none data-open:animate-in data-closed:animate-out data-closed:fade-out-0 data-open:fade-in-0 data-closed:zoom-out-95 data-open:zoom-in-95 duration-150">
        <SelectViewport class="max-h-[min(20rem,calc(100vh-5rem))] overflow-y-auto">
          <SelectItem v-for="item in options" :key="item.value" :value="item.value" :text-value="item.label" :disabled="item.disabled" class="flex min-h-10 cursor-default select-none items-center rounded-lg px-2.5 py-2 text-sm outline-none transition-colors data-[highlighted]:bg-white/[0.07] data-[state=checked]:bg-primary/[0.08] data-[disabled]:pointer-events-none data-[disabled]:opacity-40">
            <SelectItemText class="truncate font-medium">{{ item.label }}</SelectItemText>
          </SelectItem>
          <p v-if="!options.length" class="px-3 py-6 text-center text-xs text-muted-foreground">{{ t('No options are available.') }}</p>
        </SelectViewport>
      </SelectContent>
    </SelectPortal>
  </SelectRoot>
</template>
