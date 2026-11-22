<script setup lang="ts">
import { onMounted, ref } from "vue";
defineProps<{
  title: string;
  message: string;
  busy: boolean;
  error?: string;
  action?: string;
}>();
const emit = defineEmits<{ confirm: []; close: [] }>();
const dialog = ref<HTMLDialogElement>();
onMounted(() => dialog.value?.showModal());
</script>
<template>
  <dialog
    ref="dialog"
    class="confirm-dialog"
    aria-labelledby="confirmation-title"
    @cancel.prevent="!busy && emit('close')"
  >
    <h2 id="confirmation-title">{{ title }}</h2>
    <p>{{ message }}</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <div class="actions">
      <button :disabled="busy" autofocus @click="emit('close')">取消</button>
      <button class="danger" :disabled="busy" @click="emit('confirm')">
        {{ busy ? "处理中…" : action || "确认删除" }}
      </button>
    </div>
  </dialog>
</template>
