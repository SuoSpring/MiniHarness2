<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { request } from "../api";
import type {
  ChangePreview,
  MergeConflict,
  Resolution,
  Run,
  Session,
} from "../types";
import DiffPreview from "./DiffPreview.vue";
import RunDiff from "./RunDiff.vue";
import VerificationSummary from "./VerificationSummary.vue";
const props = defineProps<{
  sessionId: Session["id"];
  projectId?: string;
  run: Run | null;
  canRollback: boolean;
  archived?: boolean;
  hasActive?: boolean;
}>();
const emit = defineEmits<{ changed: [runId?: Run["id"]] }>();
type ChangeAction = "merge" | "archive" | "rollback" | "replace";
const preview = ref<ChangePreview | null>(null),
  kind = ref<ChangeAction>("merge");
const resolutions = ref<Record<string, Resolution>>({}),
  choices = ref<Record<string, string>>({}),
  memoryResolutions = ref<Record<string, "keep" | "restore">>({});
const task = ref(""),
  busy = ref(false),
  error = ref(""),
  notice = ref("");
const merging = computed(
  () => kind.value === "merge" || kind.value === "archive",
);
const ready = computed(
  () =>
    !!preview.value &&
    preview.value.conflicts.length === 0 &&
    (!merging.value ||
      kind.value === "archive" ||
      (preview.value.no_file_changes && !preview.value.verification) ||
      preview.value.verification?.status === "passed") &&
    (preview.value.memory_conflicts || []).every(
      (item) => memoryResolutions.value[item.id],
    ) &&
    (kind.value !== "replace" || task.value.trim()),
);
const resolutionsReady = computed(
  () =>
    !!preview.value?.conflicts.length &&
    preview.value.conflicts.every((item) => choices.value[item.id]),
);
const labels = {
  merge: "合并到项目公共版本",
  archive: "归档会话",
  rollback: "撤回最新调用",
  replace: "替换最新调用",
};
watch([() => props.run?.id, () => props.sessionId], () => {
  preview.value = null;
});
async function inspect(action: ChangeAction): Promise<void> {
  busy.value = true;
  error.value = "";
  notice.value = "";
  preview.value = null;
  kind.value = action;
  if (action === "replace") task.value = props.run?.task || "";
  resolutions.value = {};
  choices.value = {};
  memoryResolutions.value = {};
  try {
    preview.value = await request<ChangePreview>(
      action === "merge" || action === "archive"
        ? `/sessions/${props.sessionId}/${action}/preview`
        : `/runs/${props.run!.id}/rollback/preview`,
      "POST",
      { resolutions: {} },
    );
  } catch (cause) {
    error.value = (cause as Error).message;
  } finally {
    busy.value = false;
  }
}
function choose(id: MergeConflict["id"], choice: string): void {
  choices.value[id] = choice;
  resolutions.value[id] =
    choice === "custom"
      ? { content: "" }
      : choice === "delete"
        ? { content: null }
        : (choice as Resolution);
}
async function updatePreview(): Promise<void> {
  busy.value = true;
  error.value = "";
  try {
    preview.value = await request<ChangePreview>(
      merging.value
        ? `/sessions/${props.sessionId}/${kind.value}/preview`
        : `/runs/${props.run!.id}/rollback/preview`,
      "POST",
      { resolutions: resolutions.value },
    );
  } catch (cause) {
    error.value = (cause as Error).message;
  } finally {
    busy.value = false;
  }
}
function customContent(id: MergeConflict["id"], value: string): void {
  resolutions.value[id] = { content: value };
}
async function apply(): Promise<void> {
  if (!preview.value) return;
  busy.value = true;
  error.value = "";
  const body = {
    token: preview.value.token,
    resolutions: resolutions.value,
    ...(merging.value ? {} : { memory_resolutions: memoryResolutions.value }),
  };
  try {
    const path = merging.value
      ? `/sessions/${props.sessionId}/${kind.value}`
      : `/runs/${props.run!.id}/${kind.value === "rollback" ? "rollback" : "replace"}`;
    const result = await request<{ id?: string; cleanup_pending?: boolean }>(
      path,
      "POST",
      kind.value === "replace"
        ? {
            ...body,
            task: task.value.trim(),
            mode: props.run!.mode,
            token_budget: props.run!.token_budget,
          }
        : body,
    );
    notice.value =
      kind.value === "replace"
        ? "已撤回原调用并启动替换任务。"
        : kind.value === "rollback"
          ? "已撤回该调用，并从会话历史移除。"
          : kind.value === "archive"
            ? result.cleanup_pending
              ? "会话已归档，工作树清理将在服务恢复时重试。"
              : "会话已归档，可随时查看历史。"
            : "已合并到项目公共版本。";
    preview.value = null;
    emit("changed", kind.value === "replace" ? result?.id : undefined);
  } catch (cause) {
    error.value = (cause as Error).message;
    // 替换分两步提交。新任务启动失败时，原调用可能已经成功撤回。
    if (!merging.value) emit("changed");
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <section class="card">
    <div class="section-heading">
      <h2>审阅文件变更</h2>
    </div>
    <RunDiff v-if="run" :key="run.id" :run="run" />
    <p v-if="archived" class="notice">会话已归档，历史记录与项目版本已保留。</p>
    <div v-if="!archived" class="actions">
      <button :disabled="busy || hasActive" @click="inspect('merge')">
        预览合并</button
      ><button :disabled="busy || hasActive" @click="inspect('archive')">
        预览归档</button
      ><button
        :disabled="busy || !run || !canRollback"
        @click="inspect('rollback')"
      >
        预览撤回</button
      ><button
        :disabled="busy || !run || !canRollback"
        @click="inspect('replace')"
      >
        修改并替换调用
      </button>
    </div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="notice" class="success" role="status">{{ notice }}</p>
    <p v-if="busy && kind === 'merge'" role="status">
      正在准备合并并运行必需检查，请稍候…
    </p>
    <div v-if="preview" class="preview-area">
      <VerificationSummary
        :result="preview.verification"
        :project-id="projectId"
      />
      <p
        v-if="
          merging &&
          preview.verification &&
          preview.verification.status !== 'passed'
        "
        class="error"
      >
        合并后的版本未通过验收。请根据检查输出修复后重新预览；公共版本尚未改变。
      </p>
      <h3>{{ labels[kind] }}</h3>
      <p v-if="kind === 'archive'" class="notice">
        归档将关闭本对话，历史消息、会话文件和验收记录仍可查看。<span
          v-if="preview.no_file_changes === false"
          >尚未写回的修改会保存在会话版本中。</span
        >
      </p>
      <p v-if="!merging" class="notice">
        确认后将恢复本次调用前的文件和上下文，并从会话历史移除这次提问、补充消息和回复。
      </p>
      <label v-if="kind === 'replace'"
        >替换后的任务<textarea
          v-model="task"
          rows="4"
          aria-label="替换后的任务"
        ></textarea>
      </label>
      <DiffPreview v-if="preview.diff" :diff="preview.diff" />
      <p v-else-if="kind !== 'archive'" class="muted">没有文件修改。</p>
      <article
        v-for="conflict in preview.conflicts"
        :key="conflict.id"
        class="conflict-card"
      >
        <h4>{{ conflict.path }} <span class="badge warning">冲突</span></h4>
        <div v-if="!conflict.binary" class="conflict-versions">
          <details>
            <summary>公共起点</summary>
            <pre>{{ conflict.base ?? "（不存在）" }}</pre>
          </details>
          <details open>
            <summary>当前版本（ours）</summary>
            <pre>{{ conflict.ours ?? "（不存在）" }}</pre>
          </details>
          <details open>
            <summary>待应用版本（theirs）</summary>
            <pre>{{ conflict.theirs ?? "（不存在）" }}</pre>
          </details>
        </div>
        <p v-else class="muted small">二进制文件需要选择保留哪个版本。</p>
        <label
          >处理方式<select
            :value="choices[conflict.id] || ''"
            :aria-label="`解决冲突 ${conflict.path}`"
            @change="
              choose(conflict.id, ($event.target as HTMLSelectElement).value)
            "
          >
            <option value="" disabled>请选择</option>
            <option value="ours">保留当前版本</option>
            <option value="theirs">使用待应用版本</option>
            <option v-if="!conflict.binary" value="custom">
              手动编辑合并内容
            </option>
            <option value="delete">删除此文件</option>
          </select></label
        >
        <textarea
          v-if="choices[conflict.id] === 'custom'"
          rows="6"
          :aria-label="`合并内容 ${conflict.path}`"
          @input="
            customContent(
              conflict.id,
              ($event.target as HTMLTextAreaElement).value,
            )
          "
        ></textarea>
      </article>
      <article
        v-for="conflict in preview.memory_conflicts"
        :key="conflict.id"
        class="conflict-card"
      >
        <h4>共享记忆：{{ conflict.key }}</h4>
        <p class="small">
          该记忆已被其他调用更新（预期版本
          {{ conflict.expected_revision }}，当前版本
          {{ conflict.current_revision }}）。
        </p>
        <label
          >处理方式<select
            v-model="memoryResolutions[conflict.id]"
            :aria-label="`解决记忆冲突 ${conflict.key}`"
          >
            <option value="" disabled>请选择</option>
            <option value="keep">保留当前记忆</option>
            <option value="restore">恢复调用前的记忆</option>
          </select></label
        >
      </article>
      <div class="actions">
        <button
          v-if="
            kind === 'merge' &&
            !preview.conflicts.length &&
            !preview.no_file_changes
          "
          :disabled="busy"
          @click="updatePreview"
        >
          重新运行合并检查
        </button>
        <button
          v-if="preview.conflicts.length"
          :disabled="busy || !resolutionsReady"
          @click="updatePreview"
        >
          应用选择并重新预览
        </button>
        <button
          :class="kind === 'rollback' ? 'danger' : 'primary'"
          :disabled="busy || !ready"
          @click="apply"
        >
          确认{{ labels[kind] }}</button
        ><button :disabled="busy" @click="preview = null">取消预览</button>
      </div>
    </div>
  </section>
</template>
