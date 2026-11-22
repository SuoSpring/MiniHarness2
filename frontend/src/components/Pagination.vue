<script setup lang="ts">
import { computed } from "vue";
const props = defineProps<{
  page: number;
  total: number;
  pageSize: number;
  busy?: boolean;
  label: string;
}>();
const emit = defineEmits<{ change: [page: number] }>();
const pages = computed(() =>
  Math.max(1, Math.ceil(props.total / props.pageSize)),
);
</script>
<template>
  <nav class="pagination" :aria-label="label">
    <span class="muted small">共 {{ total.toLocaleString() }} 条</span>
    <div class="actions">
      <button
        :disabled="busy || page <= 1"
        aria-label="首页"
        @click="emit('change', 1)"
      >
        «
      </button>
      <button :disabled="busy || page <= 1" @click="emit('change', page - 1)">
        上一页
      </button>
      <span class="page-number">{{ page }} / {{ pages }}</span>
      <button
        :disabled="busy || page >= pages"
        @click="emit('change', page + 1)"
      >
        下一页
      </button>
      <button
        :disabled="busy || page >= pages"
        aria-label="末页"
        @click="emit('change', pages)"
      >
        »
      </button>
    </div>
  </nav>
</template>
