<script setup lang="ts">
import { Button } from '~/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '~/components/ui/dialog'
import { useConfirmDialog } from '~/composables/useConfirmDialog'
import { useI18n } from '~/composables/useI18n'
const { open, details, finish } = useConfirmDialog()
const { t } = useI18n()
</script>
<template>
  <Dialog :open="open" @update:open="!$event && finish(false)">
    <DialogContent class="border border-white/10 bg-popover/95 shadow-xl backdrop-blur-xl" @keydown.stop>
      <DialogHeader><DialogTitle>{{ details.title }}</DialogTitle><DialogDescription>{{ details.description }}</DialogDescription></DialogHeader>
      <DialogFooter>
        <Button variant="outline" @click="finish(false)">{{ t('Cancel') }}</Button>
        <Button :variant="details.destructive ? 'destructive' : 'default'" :class="!details.destructive ? 'border border-primary/20 bg-primary/15 text-primary hover:bg-primary/25' : ''" @click="finish(true)">{{ details.confirmLabel ?? t('Continue') }}</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
