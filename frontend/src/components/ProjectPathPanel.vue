<script setup lang="ts">
import { ref, watch } from "vue";
import type { Project } from "../types";
const props = defineProps<{ project: Project; busy: boolean }>();
const emit = defineEmits<{ save: [path: string] }>();
const path = ref("");
watch(
  () => [props.project.id, props.project.workspace_path],
  () => {
    path.value = props.project.workspace_path || "";
  },
  { immediate: true },
);
</script>
<template>
  <section class="card project-path-panel">
    <h2>项目目录</h2>
    <p class="muted small">
      调用通过验收后，文件自动写回这里。可指定已有目录导入代码，也可使用空目录保存项目。
    </p>
    <p v-if="project.workspace_path" class="path-current">
      当前目录：<code>{{ project.workspace_path }}</code>
    </p>
    <p v-else class="muted small">当前使用内部工作区，尚未关联本地目录。</p>
    <form @submit.prevent="emit('save', path.trim())">
      <label for="project-directory">本地项目路径</label>
      <div class="inline-form">
        <input
          id="project-directory"
          v-model="path"
          placeholder="例如 E:\Code\my-project"
          required
          :disabled="busy"
        />
        <button
          class="primary"
          :disabled="
            busy || !path.trim() || path.trim() === project.workspace_path
          "
        >
          {{ busy ? "保存中…" : "保存项目目录" }}
        </button>
      </div>
    </form>
    <p
      v-if="project.workspace_path && path.trim() === project.workspace_path"
      class="small success"
      role="status"
    >
      项目目录已关联。
    </p>
  </section>
</template>
