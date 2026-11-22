<script setup lang="ts">
import { computed, ref, watch } from "vue";
import RunUsage from "./RunUsage.vue";
import {
  activeStatuses,
  statusLabels,
  type Run,
  type RunControlArgs,
  type PermissionPolicy,
} from "../types";
const props = defineProps<{
  run: Run | null;
  busy: boolean;
  enabled: boolean;
  latest: boolean;
  hasActive: boolean;
  activeRun?: Run;
  sessionId: string;
  send: (
    text: string,
    mode: Run["mode"],
    permissions: PermissionPolicy,
  ) => Promise<boolean>;
}>();
const emit = defineEmits<{
  control: RunControlArgs;
  retry: [];
  cancel: [];
}>();
const task = ref("");
const mode = ref<Run["mode"]>("react");
const sending = ref(false);
const revisedGoal = ref("");
const revisedCriteria = ref("");
const retryable = computed(
  () =>
    !!props.run &&
    ["failed", "crashed", "blocked", "budget_exceeded", "cancelled"].includes(
      props.run.status,
    ),
);
try {
  task.value =
    sessionStorage.getItem(`miniharness2.draft.${props.sessionId}`) || "";
} catch {
  /* 使用默认值。 */
}
watch(task, (value) => {
  try {
    sessionStorage.setItem(`miniharness2.draft.${props.sessionId}`, value);
  } catch {
    /* 可继续输入。 */
  }
});
function savedPermissions(): PermissionPolicy {
  try {
    const saved = JSON.parse(
      localStorage.getItem("miniharness2.permissions") || "null",
    );
    if (
      saved &&
      ["none", "project", "all"].includes(saved.write_scope) &&
      ["request", "full_access"].includes(saved.approval_mode)
    ) {
      return {
        ...saved,
        write_scope:
          saved.approval_mode === "full_access" ? "all" : saved.write_scope,
      };
    }
  } catch {
    /* 使用默认权限 */
  }
  return { write_scope: "project", approval_mode: "request" };
}
const permissions = ref<PermissionPolicy>(savedPermissions());
watch(
  permissions,
  (value) => {
    if (value.approval_mode === "full_access") value.write_scope = "all";
    try {
      localStorage.setItem("miniharness2.permissions", JSON.stringify(value));
    } catch {
      /* 偏好可选。 */
    }
  },
  { deep: true, flush: "sync" },
);
const active = computed(
  () => !!props.run && activeStatuses.has(props.run.status),
);
const waiting = computed(
  () =>
    !!props.run &&
    (props.run.interrupt as { kind?: string } | null)?.kind !==
      "plan_clarification" &&
    ["paused", "awaiting_input", "waiting_human", "interrupted"].includes(
      props.run.status,
    ),
);
const inProgress = computed(
  () =>
    active.value ||
    waiting.value ||
    props.run?.status === "awaiting_input" ||
    ["awaiting_approval", "awaiting_tool_approval"].includes(
      props.run?.status || "",
    ),
);
const shownMode = computed({
  get: () => props.activeRun?.mode ?? mode.value,
  set: (value) => {
    mode.value = value;
  },
});
const shownApproval = computed({
  get: () =>
    props.activeRun?.permission_policy?.approval_mode ??
    permissions.value.approval_mode,
  set: (value) => {
    permissions.value.approval_mode = value;
  },
});
const shownScope = computed({
  get: () =>
    props.activeRun?.permission_policy?.write_scope ??
    permissions.value.write_scope,
  set: (value) => {
    permissions.value.write_scope = value;
  },
});
async function send(): Promise<void> {
  const draft = task.value;
  if (props.busy || sending.value || !props.enabled || !draft.trim()) return;
  sending.value = true;
  try {
    if (
      (await props.send(draft.trim(), mode.value, { ...permissions.value })) &&
      task.value === draft
    ) {
      task.value = "";
    }
  } finally {
    sending.value = false;
  }
}
function onEnter(event: KeyboardEvent): void {
  if (event.isComposing || event.keyCode === 229 || event.shiftKey) return;
  event.preventDefault();
  if (!event.repeat) void send();
}
function continueBudget(): void {
  emit("control", "resume", {});
}
function reviseSpecification(): void {
  const criteria = revisedCriteria.value
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  emit("control", "steer", {
    text: "用户明确修订本次任务的目标与验收标准。",
    spec_update: {
      ...(revisedGoal.value.trim() ? { goal: revisedGoal.value.trim() } : {}),
      ...(criteria.length
        ? {
            criteria: criteria.map((description, index) => ({
              id: `u:spec:${index + 1}`,
              description,
            })),
          }
        : {}),
    },
  });
}
</script>

<template>
  <section class="card composer" aria-label="对话输入">
    <form @submit.prevent="send">
      <label class="sr-only" for="task">任务要求</label>
      <textarea
        id="task"
        v-model="task"
        @keydown.enter="onEnter"
        placeholder="描述任务、提出问题，或者一起构思…"
        rows="3"
        :disabled="!enabled"
        required
      ></textarea>
      <div class="composer-toolbar">
        <label v-if="!hasActive || activeRun" class="toggle"
          ><input
            v-model="shownMode"
            :disabled="busy || hasActive"
            type="checkbox"
            true-value="plan"
            false-value="react"
          /><span>计划模式</span></label
        >
        <span
          v-if="run && (!hasActive || latest)"
          class="badge"
          :data-status="run.status"
          >{{ statusLabels[run.status] || run.status }}</span
        >
        <RunUsage :run="run" />
      </div>
      <div class="actions">
        <button
          class="primary"
          :disabled="busy || sending || !enabled || !task.trim()"
          type="submit"
        >
          发送 <span aria-hidden="true">↗</span>
        </button>
        <button
          v-if="active && latest"
          type="button"
          :disabled="busy"
          @click="emit('control', 'pause')"
        >
          暂停执行
        </button>
        <button
          v-if="waiting && latest"
          type="button"
          :disabled="busy"
          @click="task.trim() ? send() : emit('control', 'resume', {})"
        >
          继续执行
        </button>
        <button
          v-if="run?.status === 'budget_exceeded'"
          type="button"
          :disabled="busy || hasActive"
          @click="continueBudget"
          title="从会话当前文件接着完成所选任务，保留后来已提交的成果"
        >
          按项目设置继续
        </button>
        <button
          v-if="retryable && run?.status !== 'budget_exceeded'"
          type="button"
          title="使用原任务的执行方式与权限，按最新项目预算重新执行"
          :disabled="busy || hasActive"
          @click="emit('retry')"
        >
          重试任务
        </button>
        <button
          v-if="inProgress && latest"
          type="button"
          class="danger subtle"
          :disabled="busy"
          @click="emit('cancel')"
        >
          停止任务
        </button>
        <div
          v-if="!hasActive || activeRun"
          class="composer-permissions"
          role="group"
          aria-label="操作权限"
        >
          <select
            v-model="shownApproval"
            aria-label="审批模式"
            :disabled="busy || hasActive"
          >
            <option value="request">重大操作请求批准</option>
            <option value="full_access">完全访问权限</option>
          </select>
          <span aria-hidden="true">·</span>
          <select
            v-model="shownScope"
            aria-label="写入范围"
            :disabled="busy || hasActive || shownApproval === 'full_access'"
          >
            <option value="none">只读</option>
            <option value="project">项目内写入</option>
            <option value="all">全部文件</option>
          </select>
        </div>
      </div>
    </form>
    <details v-if="inProgress && latest" class="spec-revision">
      <summary>修订目标与验收标准</summary>
      <label
        >修订后的目标<textarea
          v-model="revisedGoal"
          rows="2"
          placeholder="留空表示保留当前目标"
        ></textarea>
      </label>
      <label
        >修订后的验收标准（每行一项）<textarea
          v-model="revisedCriteria"
          rows="3"
          placeholder="例如：程序支持指定输入文件&#10;已有测试全部通过"
        ></textarea>
      </label>
      <button
        type="button"
        :disabled="busy || (!revisedGoal.trim() && !revisedCriteria.trim())"
        @click="reviseSpecification"
      >
        提交明确修订
      </button>
    </details>
  </section>
</template>
