<script setup lang="ts">
import { ref, watch } from "vue";
import { request } from "../api";
import type {
  EnvironmentSelection,
  EnvironmentSettings,
  PythonEnvironment,
} from "../types";
import EnvironmentFields from "./EnvironmentFields.vue";

const props = defineProps<{ projectId?: string; revision: number }>();
const settings = ref<EnvironmentSettings | null>(null);
const current = ref<PythonEnvironment | null>(null);
const selection = ref<EnvironmentSelection>({
  mode: "default",
  python: "",
  name: "",
});
const defaultPython = ref("");
const defaultName = ref("");
const busy = ref(false),
  error = ref(""),
  notice = ref("");
let generation = 0;
watch(
  () => [props.projectId, props.revision],
  async () => {
    const token = ++generation;
    error.value = "";
    notice.value = "";
    current.value = null;
    try {
      const [configuration, environment] = await Promise.all([
        request<EnvironmentSettings>("/settings/python-environment"),
        props.projectId
          ? request<PythonEnvironment>(
              `/projects/${props.projectId}/environment`,
            )
          : Promise.resolve(null),
      ]);
      if (token !== generation) return;
      settings.value = configuration;
      current.value = environment;
      defaultPython.value = configuration.default.python;
      defaultName.value = configuration.default.name;
      selection.value = { mode: "default", python: "", name: "" };
    } catch (cause) {
      if (token === generation) error.value = (cause as Error).message;
    }
  },
  { immediate: true },
);

async function saveDefault(): Promise<void> {
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    const environment = await request<PythonEnvironment>(
      "/settings/python-environment",
      "PUT",
      { python: defaultPython.value, name: defaultName.value },
    );
    if (settings.value) settings.value.default = environment;
    notice.value = "已保存，新项目使用此环境。";
  } catch (cause) {
    error.value = (cause as Error).message;
  } finally {
    busy.value = false;
  }
}
async function saveProject(): Promise<void> {
  const projectId = props.projectId;
  if (!projectId) return;
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    const environment = await request<PythonEnvironment>(
      `/projects/${projectId}/environment`,
      "PUT",
      selection.value,
    );
    if (projectId !== props.projectId) return;
    current.value = environment;
    notice.value = "项目环境已保存。";
  } catch (cause) {
    if (projectId === props.projectId) error.value = (cause as Error).message;
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div class="panel-stack environment-layout">
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <section v-if="projectId" class="card">
      <h2>项目 Python 运行环境</h2>
      <div v-if="current" class="environment-current">
        <strong>{{ current.name }}</strong
        ><span class="badge">{{
          current.managed ? "项目独立环境" : "已有环境"
        }}</span>
        <p>Python {{ current.version || "版本未读取" }}</p>
        <code>{{ current.python }}</code>
      </div>
      <form @submit.prevent="saveProject">
        <fieldset :disabled="busy || !current">
          <legend>切换项目环境</legend>
          <EnvironmentFields
            v-model="selection"
            prefix="project-environment"
            :environments="settings?.environments"
          />
          <button class="primary" :disabled="busy || !current">
            {{ busy ? "正在配置…" : "应用项目环境" }}
          </button>
        </fieldset>
      </form>
    </section>
    <section class="card">
      <h2>后端默认 Python 环境</h2>
      <p class="muted small">仅用于新建项目。</p>
      <form @submit.prevent="saveDefault">
        <fieldset :disabled="busy || !settings">
          <legend>默认环境设置</legend>
          <label for="default-python">默认 Python 解释器或环境目录</label>
          <input
            id="default-python"
            v-model="defaultPython"
            required
            list="default-environment-choices"
            placeholder="解释器或环境目录的绝对路径"
          />
          <datalist id="default-environment-choices">
            <option
              v-for="environment in settings?.environments || []"
              :key="`${environment.python}:${environment.name}`"
              :value="environment.python"
            >
              {{ environment.name }}
            </option>
          </datalist>
          <label for="default-environment-name">默认环境名称</label
          ><input
            id="default-environment-name"
            v-model="defaultName"
            maxlength="100"
          />
          <button class="primary" :disabled="busy || !settings">
            保存默认环境
          </button>
        </fieldset>
      </form>
    </section>
  </div>
</template>
