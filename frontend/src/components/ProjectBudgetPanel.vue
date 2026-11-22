<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { request } from "../api";
import type { ProjectRunSettings } from "../types";
import RunLimitsFields from "./RunLimitsFields.vue";
const props = defineProps<{ projectId: string }>();
const settings = ref<ProjectRunSettings | null>(null);
const saved = ref("");
const busy = ref(false),
  loading = ref(true),
  error = ref(""),
  notice = ref("");
let version = 0;
const dirty = computed(
  () => !!settings.value && JSON.stringify(settings.value) !== saved.value,
);
const validation = computed(() => {
  const value = settings.value;
  if (
    value?.max_context_tokens &&
    value.max_output_tokens >= value.max_context_tokens
  )
    return "上下文窗口需要大于单次输出上限，也可以将其中一项设为无上限。";
  return "";
});
async function load(): Promise<void> {
  const current = ++version;
  loading.value = true;
  settings.value = null;
  error.value = notice.value = "";
  try {
    const value = await request<ProjectRunSettings>(
      `/projects/${props.projectId}/run-settings`,
    );
    if (current !== version) return;
    settings.value = value;
    saved.value = JSON.stringify(value);
  } catch (cause) {
    if (current === version)
      error.value = String(cause instanceof Error ? cause.message : cause);
  } finally {
    if (current === version) loading.value = false;
  }
}
async function save(): Promise<void> {
  if (!settings.value || validation.value || busy.value) return;
  const current = version;
  busy.value = true;
  error.value = notice.value = "";
  try {
    const value = await request<ProjectRunSettings>(
      `/projects/${props.projectId}/run-settings`,
      "PUT",
      settings.value,
    );
    if (current !== version) return;
    settings.value = value;
    saved.value = JSON.stringify(value);
    notice.value =
      "项目设置已保存。下次任务、重试或预算耗尽后的接续会使用这些设置。";
  } catch (cause) {
    if (current === version)
      error.value = String(cause instanceof Error ? cause.message : cause);
  } finally {
    busy.value = false;
  }
}
watch(() => props.projectId, load, { immediate: true });
</script>
<template>
  <section class="budget-panel">
    <div class="settings-title">
      <span class="eyebrow">项目偏好</span>
      <h2>预算与模型</h2>
      <p>为这个项目设置默认额度，专注于每一次任务。</p>
    </div>
    <p v-if="loading" class="muted" role="status">正在加载项目设置…</p>
    <div v-if="error" class="error" role="alert">
      {{ error }} <button v-if="!settings" @click="load">重新加载</button>
    </div>
    <form v-if="settings" @submit.prevent="save">
      <RunLimitsFields v-model="settings" :disabled="busy" />
      <div class="settings-footnote">
        <span aria-hidden="true">ⓘ</span>
        <p>
          “无上限”表示 MiniHarness
          不额外设限；模型服务自身的限制仍然有效。已有任务保留启动时的设置。
          网络故障最多自动重试 3
          次，中途失败不写入对话上下文；重试仍受总时限与预算约束。
        </p>
      </div>
      <p v-if="validation" class="error" role="alert">{{ validation }}</p>
      <div class="settings-savebar">
        <span v-if="notice && !dirty" class="success" role="status">{{
          notice
        }}</span>
        <span v-else class="muted small">{{
          dirty ? "有尚未保存的修改" : "设置仅对当前项目生效"
        }}</span>
        <button
          type="submit"
          class="primary"
          :disabled="busy || !dirty || !!validation"
        >
          {{ busy ? "保存中…" : "保存项目设置" }}
        </button>
      </div>
    </form>
  </section>
</template>
