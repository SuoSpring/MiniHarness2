<script setup lang="ts">
import { ref, watch } from "vue";
import { request } from "../api";
import type { Memory } from "../types";
const props = defineProps<{
  projectId: string;
  sessionId: string;
  revision: number;
}>();
const memories = ref<Memory[]>([]),
  key = ref(""),
  content = ref(""),
  keywords = ref(""),
  scope = ref("project"),
  editing = ref<Memory | null>(null),
  error = ref(""),
  busy = ref(false),
  notice = ref("");
async function load(): Promise<void> {
  error.value = "";
  try {
    memories.value = await request<Memory[]>(
      `/projects/${props.projectId}/memories?session_id=${props.sessionId}`,
    );
  } catch (cause) {
    error.value = (cause as Error).message;
  }
}
function reset(): void {
  editing.value = null;
  key.value = "";
  content.value = "";
  keywords.value = "";
  scope.value = "project";
}
function edit(memory: Memory): void {
  editing.value = memory;
  key.value = memory.key;
  content.value = memory.content;
  keywords.value = (memory.keywords || []).join("，");
  scope.value = memory.session_id ? "session" : "project";
  notice.value = "";
}
async function save(): Promise<void> {
  busy.value = true;
  error.value = "";
  try {
    await request(`/projects/${props.projectId}/memories`, "PUT", {
      key: key.value.trim(),
      content: content.value.trim(),
      keywords: keywords.value
        .split(/[,，;；\n]+/)
        .map((word) => word.trim())
        .filter(Boolean),
      session_id: scope.value === "session" ? props.sessionId : null,
      ...(editing.value ? { expected_revision: editing.value.revision } : {}),
    });
    reset();
    notice.value = "记忆已保存。";
    await load();
  } catch (cause) {
    error.value = (cause as Error).message;
  } finally {
    busy.value = false;
  }
}
async function remove(memory: Memory): Promise<void> {
  busy.value = true;
  error.value = "";
  try {
    await request(
      `/memories/${memory.id}?expected_revision=${memory.revision}`,
      "DELETE",
    );
    if (editing.value?.id === memory.id) reset();
    await load();
  } catch (cause) {
    error.value = (cause as Error).message;
  } finally {
    busy.value = false;
  }
}
watch(
  () => [props.projectId, props.sessionId],
  () => {
    reset();
    void load();
  },
  { immediate: true },
);
watch(() => props.revision, load);
</script>
<template>
  <section class="card">
    <div class="section-heading">
      <h2>长期记忆</h2>
      <button :disabled="busy" @click="load">刷新</button>
    </div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="notice" class="success" role="status">{{ notice }}</p>
    <div class="memory-grid">
      <article v-for="memory in memories" :key="memory.id" class="memory-card">
        <div class="section-heading">
          <h3>{{ memory.key }}</h3>
          <span class="badge"
            >{{ memory.session_id ? "会话" : "项目" }} · v{{
              memory.revision
            }}</span
          >
        </div>
        <p>{{ memory.content }}</p>
        <p v-if="memory.updated_at" class="muted small">
          最近更新 <time :datetime="memory.updated_at">{{ new Date(memory.updated_at).toLocaleString() }}</time>
        </p>
        <p v-if="memory.keywords?.length" class="muted small">
          关键词：{{ memory.keywords.join(" · ") }}
        </p>
        <div class="actions">
          <button :disabled="busy" @click="edit(memory)">编辑</button
          ><button
            :disabled="busy"
            class="danger subtle"
            @click="remove(memory)"
          >
            删除
          </button>
        </div>
      </article>
    </div>
    <form class="stack-form" @submit.prevent="save">
      <h3>{{ editing ? "编辑记忆" : "新增记忆" }}</h3>
      <div class="form-columns">
        <label
          >名称<input
            v-model="key"
            :disabled="!!editing"
            required
            maxlength="200" /></label
        ><label
          >范围<select v-model="scope" :disabled="!!editing">
            <option value="project">整个项目</option>
            <option value="session">当前会话</option>
          </select></label
        >
      </div>
      <label
        >内容<textarea v-model="content" rows="3" required></textarea>
      </label>
      <label
        >关键词<input
          v-model="keywords"
          placeholder="例如：后端，开发语言，Java（逗号分隔，最多 12 个）"
      /></label>
      <div class="actions">
        <button
          class="primary"
          :disabled="busy || !key.trim() || !content.trim()"
        >
          保存记忆</button
        ><button v-if="editing" type="button" @click="reset">取消编辑</button>
      </div>
    </form>
  </section>
</template>
