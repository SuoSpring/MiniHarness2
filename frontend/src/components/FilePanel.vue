<script setup lang="ts">
import { ref, watch } from "vue";
import { apiUrl, request } from "../api";
import type { FileContent, FileEntry } from "../types";
const props = defineProps<{
  projectId: string;
  sessionId: string;
  revision: number;
}>();
const files = ref<FileEntry[]>([]),
  selected = ref<FileContent | null>(null),
  publicView = ref(false),
  error = ref(""),
  busy = ref(false);
let listingVersion = 0,
  fileVersion = 0;
function query(): string {
  return publicView.value ? "" : `session_id=${props.sessionId}`;
}
async function load(): Promise<void> {
  const version = ++listingVersion;
  fileVersion++;
  busy.value = true;
  error.value = "";
  selected.value = null;
  try {
    const data = await request<FileEntry[]>(
      `/projects/${props.projectId}/files?${query()}`,
    );
    if (version === listingVersion) files.value = data;
  } catch (cause) {
    if (version === listingVersion) error.value = (cause as Error).message;
  } finally {
    if (version === listingVersion) busy.value = false;
  }
}
async function open(path: FileEntry["path"]): Promise<void> {
  const version = ++fileVersion;
  error.value = "";
  try {
    const data = await request<FileContent>(
      `/projects/${props.projectId}/file?path=${encodeURIComponent(path)}&${query()}`,
    );
    if (version === fileVersion) selected.value = data;
  } catch (cause) {
    if (version === fileVersion) error.value = (cause as Error).message;
  }
}
watch(
  () => [props.projectId, props.sessionId, props.revision, publicView.value],
  load,
  { immediate: true },
);
</script>
<template>
  <section class="card">
    <div class="section-heading">
      <h2>项目文件</h2>
      <label class="toggle"
        ><input v-model="publicView" type="checkbox" />查看公共版本</label
      ><button :disabled="busy" @click="load">刷新</button>
    </div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <div class="file-browser">
      <nav aria-label="文件列表">
        <button
          v-for="file in files"
          :key="file.path"
          :class="{ selected: selected?.path === file.path }"
          @click="open(file.path)"
        >
          {{ file.path }}
        </button>
        <p v-if="!files.length" class="muted small">暂无文件。</p>
      </nav>
      <div>
        <div v-if="selected" class="section-heading">
          <strong>{{ selected.path }}</strong
          ><a
            :href="
              apiUrl(
                `/projects/${projectId}/download?path=${encodeURIComponent(selected.path)}&${query()}`,
              )
            "
            download
            >下载完整文件</a
          >
        </div>
        <p v-if="selected?.binary" class="notice">
          这是二进制文件，无法显示文本预览。
        </p>
        <p v-if="selected?.truncated" class="notice">
          文件较大，当前只显示预览。下载可获得完整内容。
        </p>
        <pre v-if="!selected?.binary" class="output file-content">{{
          selected?.content ?? "选择文件查看内容。"
        }}</pre>
      </div>
    </div>
  </section>
</template>
