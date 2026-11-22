<script setup lang="ts">
import { computed, ref } from "vue";
const props = defineProps<{ diff: string; paged?: boolean }>();
const lines = computed(() => props.diff.split("\n"));
const limit = ref(300);
const visible = computed(() =>
  props.paged ? lines.value.slice(0, limit.value) : lines.value,
);
function lineClass(line: string): string {
  return line.startsWith("+++ ") ||
    line.startsWith("--- ") ||
    line.startsWith("diff --git ")
    ? "file-line"
    : line.startsWith("+")
      ? "added"
      : line.startsWith("-")
        ? "removed"
        : line.startsWith("@@")
          ? "hunk"
          : "";
}
</script>
<template>
  <pre
    class="diff"
  ><code v-for="(line, index) in visible" :key="index" :class="lineClass(line)">{{ line || ' ' }}
</code></pre>
  <button
    v-if="paged && limit < lines.length"
    type="button"
    @click="limit += 300"
  >
    显示更多差异
  </button>
</template>
