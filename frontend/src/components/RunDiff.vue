<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from "vue";
import { apiUrl } from "../api";
import { terminalStatuses, type Run } from "../types";
import DiffPreview from "./DiffPreview.vue";

const props = defineProps<{ run: Run }>();
const open = ref(false);
const full = ref<string | null>(null);
const loading = ref(false);
const error = ref("");
const result = computed(
  () =>
    props.run.result as {
      diff?: string;
      diff_truncated?: boolean;
      has_changes?: boolean;
    } | null,
);
const preview = computed(() =>
  typeof result.value?.diff === "string" ? result.value.diff : null,
);
const text = computed(() => full.value ?? preview.value);
const hasSource = computed(() =>
  Boolean(
    props.run.patch_available ||
      (props.run.start_snapshot && props.run.end_snapshot),
  ),
);
const unchanged = computed(
  () =>
    result.value?.has_changes === false ||
    Boolean(
      props.run.start_snapshot &&
        props.run.start_snapshot === props.run.end_snapshot,
    ),
);
// 历史记录的预览上限为五万字符，尚无明确的截断标志。
const truncated = computed(
  () => result.value?.diff_truncated ?? (preview.value?.length ?? 0) >= 50_000,
);
const needsFull = computed(
  () =>
    hasSource.value && !unchanged.value && (!preview.value || truncated.value),
);
const emptyMessage = computed(() => {
  if (unchanged.value) return "本次调用没有文件变更。";
  if (result.value?.has_changes) return "本次调用有文件变更，暂无差异内容。";
  if (text.value === "") return "本次调用没有文件变更。";
  return terminalStatuses.has(props.run.status)
    ? "本次调用没有保存差异记录。"
    : "调用结束后可查看文件差异。";
});
let controller: AbortController | undefined;

async function load(): Promise<void> {
  if (loading.value || full.value !== null || !needsFull.value) return;
  loading.value = true;
  error.value = "";
  controller = new AbortController();
  try {
    const response = await fetch(
      apiUrl(`/runs/${encodeURIComponent(props.run.id)}/patch?inline=true`),
      { signal: controller.signal },
    );
    if (!response.ok)
      throw new Error(`读取完整差异失败（${response.status}）。`);
    full.value = await response.text();
  } catch (cause) {
    if (!controller.signal.aborted) error.value = (cause as Error).message;
  } finally {
    loading.value = false;
  }
}
watch([open, needsFull], ([visible, needs]) => {
  if (visible && needs) void load();
});
onUnmounted(() => controller?.abort());
</script>

<template>
  <details
    class="run-diff"
    @toggle="open = ($event.currentTarget as HTMLDetailsElement).open"
  >
    <summary>本次调用的文件差异</summary>
    <template v-if="open">
      <p v-if="loading && !text" class="small muted" role="status">
        正在读取差异…
      </p>
      <p v-if="error" class="error" role="alert">
        {{ error }}{{ preview ? "已显示保存的预览。" : "" }}
        <button @click="load">重试</button>
      </p>
      <DiffPreview v-if="text" :diff="text" paged />
      <p v-else-if="!loading && !error" class="small muted">
        {{ emptyMessage }}
      </p>
      <p v-if="truncated && !hasSource" class="small muted">
        这份历史记录仅保存了部分差异。
      </p>
      <a
        v-if="run.patch_available && !unchanged"
        class="text-link"
        :href="apiUrl(`/runs/${run.id}/patch`)"
        download
        >下载完整补丁</a
      >
    </template>
  </details>
</template>
