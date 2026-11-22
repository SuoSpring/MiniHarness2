<script setup lang="ts">
import { computed } from "vue";
import {
  display,
  type PermissionRequest,
  type Run,
  type ToolApproval,
  type PythonEnvironment,
} from "../types";

const props = defineProps<{ run: Run; busy: boolean; latest: boolean }>();
const emit = defineEmits<{ approve: [body: ToolApproval] }>();
const request = computed(() => props.run.interrupt as PermissionRequest | null);
const environment = computed(
  () => request.value?.arguments._environment as PythonEnvironment | undefined,
);
const visibleArguments = computed(() =>
  Object.fromEntries(
    Object.entries(request.value?.arguments || {}).filter(
      ([key]) => !key.startsWith("_environment"),
    ),
  ),
);
function decide(decision: ToolApproval["decision"]): void {
  if (request.value)
    emit("approve", { request_id: request.value.request_id, decision });
}
</script>

<template>
  <section
    v-if="
      run.status === 'awaiting_tool_approval' &&
      request?.kind === 'tool_permission'
    "
    class="card needs-attention"
    aria-label="操作审批"
  >
    <div class="section-heading">
      <h2>这次操作需要你批准</h2>
      <span class="badge">{{ request.tool }}</span>
    </div>
    <ul>
      <li v-for="reason in request.reasons" :key="reason">{{ reason }}</li>
    </ul>
    <p class="small">
      工作目录：<code>{{ request.workdir }}</code>
    </p>
    <p v-if="environment" class="small">
      当前项目环境：{{ environment.name }}<br /><code>{{
        environment.python
      }}</code>
    </p>
    <template v-if="request.write_paths.length">
      <h3>将写入的路径</h3>
      <ul>
        <li v-for="path in request.write_paths" :key="path">
          <code>{{ path }}</code
          ><strong v-if="request.outside_paths.includes(path)"
            >（项目外）</strong
          >
        </li>
      </ul>
    </template>
    <details v-if="request.read_paths.length">
      <summary>读取路径</summary>
      <pre class="output">{{ request.read_paths.join("\n") }}</pre>
    </details>
    <details open>
      <summary>具体操作与参数</summary>
      <pre class="output">{{ display(visibleArguments) }}</pre>
    </details>
    <details v-if="request.script">
      <summary>查看将运行的 Python 脚本</summary>
      <pre class="output">{{ request.script.content }}</pre>
      <p v-if="request.script.truncated" class="notice">
        预览仅展示前 32,000 字节，请检查完整文件后决定。
      </p>
    </details>
    <p class="notice">{{ request.notice }}</p>
    <p v-if="request.similar_approval" class="small">
      本次任务可记住的范围：{{ request.similar_approval.description }}。
    </p>
    <div v-if="latest" class="actions">
      <button class="primary" :disabled="busy" @click="decide('approve')">
        仅批准这次操作
      </button>
      <button
        v-if="request.similar_approval"
        :disabled="busy"
        @click="decide('approve_similar')"
      >
        本次任务自动批准同类操作
      </button>
      <button class="danger subtle" :disabled="busy" @click="decide('reject')">
        拒绝这次操作
      </button>
    </div>
  </section>
</template>
