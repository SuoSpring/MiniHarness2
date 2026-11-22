<script setup lang="ts">
import { computed, defineAsyncComponent, ref } from "vue";
const MarkdownText = defineAsyncComponent(() => import("./MarkdownText.vue"));
const props = defineProps<{ value: unknown; name?: string }>();
const expanded = ref(false);
const nested = computed(
  () => props.value !== null && typeof props.value === "object",
);
const entries = computed(() =>
  nested.value ? Object.entries(props.value as object) : [],
);
const labels: Record<string, string> = {
  summary: "说明",
  status: "状态",
  verified: "通过验收",
  diff: "文件差异",
  has_changes: "有文件改动",
  snapshot: "文件版本",
  source_event_id: "复用的工具事件",
  file_revision: "文件修订号",
  publication: "项目写回",
  path: "路径",
  review: "审查意见",
  memory: "记忆",
  verification: "验收记录",
  decision: "决定",
  checks: "检查",
  reason: "原因",
  description: "说明",
  error: "错误",
  conflicts: "冲突文件",
  evidence_ids: "证据编号",
  criterion_id: "验收条目",
  passed: "通过",
  task_kind: "任务类型",
  step_complete: "步骤完成",
  stdout: "标准输出",
  stderr: "错误输出",
  scope: "验收范围",
  suite_ids: "用例组编号",
  probe: "测试输入与入口",
  assertions: "预期结果",
  expected: "预期值",
  operator: "比较方式",
  reference: "预期值的参考计算",
  function: "参考函数",
  inputs: "参考计算的输入",
  compare_to: "作比较的另一项观测",
  input_path: "原始输入路径",
  target: "入口文件",
  symbol: "函数",
  args: "参数",
  kwargs: "命名参数",
};
const label = computed(() =>
  props.name === undefined
    ? ""
    : (labels[props.name] ??
      (/^\d+$/.test(props.name) ? String(Number(props.name) + 1) : props.name)),
);
const leaf = computed(() =>
  props.value === true
    ? "是"
    : props.value === false
      ? "否"
      : String(props.value ?? "无"),
);
</script>
<template>
  <details
    v-if="nested && name !== undefined"
    class="data-group"
    @toggle="expanded = ($event.currentTarget as HTMLDetailsElement).open"
  >
    <summary>{{ label }}</summary>
    <ReadableValue v-if="expanded" :value="value" />
  </details>
  <div v-else-if="nested" class="data-fields">
    <ReadableValue
      v-for="[key, item] in entries"
      :key="key"
      :name="key"
      :value="item"
    />
  </div>
  <div v-else class="data-field">
    <strong v-if="label">{{ label }}</strong>
    <MarkdownText
      v-if="
        typeof value === 'string' &&
        ['summary', 'reason', 'description', 'text', 'answer'].includes(
          name ?? '',
        )
      "
      :text="value"
    />
    <pre v-else class="field-value">{{ leaf }}</pre>
  </div>
</template>
