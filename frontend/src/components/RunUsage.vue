<script setup lang="ts">
import { computed, ref, watch } from "vue";
import type { Run } from "../types";

const props = defineProps<{ run: Run | null }>();
const open = ref(false);
watch(
  () => props.run?.id,
  () => (open.value = false),
);
const usage = computed(() => props.run?.usage ?? props.run?.state?.usage ?? {});
const limits = computed(() => props.run?.limits);
const used = computed(
  () => usage.value.context_tokens ?? (props.run ? null : 0),
);
const capacity = computed(() => limits.value?.max_context_tokens);
const percent = computed(() =>
  used.value !== null && capacity.value
    ? Math.min(100, Math.round((used.value / capacity.value) * 100))
    : null,
);
const label = computed(() =>
  percent.value !== null
    ? `${percent.value}%`
    : used.value !== null
      ? `${used.value.toLocaleString()} 词元`
      : "待统计",
);
function limit(value?: number, unit = ""): string {
  if (value == null) return "未记录";
  return value ? value.toLocaleString() + unit : "无上限";
}
const elapsed = computed(() => {
  const seconds = Math.floor(usage.value.active_seconds || 0);
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
});
</script>

<template>
  <div
    class="context-usage"
    @mouseenter="open = true"
    @mouseleave="open = false"
    @focusin="open = true"
    @focusout="open = false"
    @keydown.esc.stop="open = false"
  >
    <button
      type="button"
      class="usage-trigger"
      :aria-label="`上下文用量 ${label}`"
      :aria-describedby="open ? 'usage-tooltip' : undefined"
      @click="open = true"
    >
      <span>上下文</span>
      <progress
        v-if="percent !== null"
        aria-label="上下文占用"
        :max="capacity"
        :value="used ?? 0"
      ></progress>
      <span v-else class="usage-empty-track" aria-hidden="true"></span>
      <span>{{ label }}</span>
    </button>
    <div v-if="open" id="usage-tooltip" role="tooltip" class="usage-breakdown">
      <strong>用量明细</strong>
      <dl>
        <dt>最近规划或执行的输入</dt>
        <dd>
          {{ usage.context_estimated ? "约 " : ""
          }}{{ used?.toLocaleString() ?? "待统计" }} / {{ limit(capacity) }}
        </dd>
        <template v-if="capacity && limits?.compression_threshold != null">
          <dt>上下文压缩</dt>
          <dd>
            {{
              limits?.compression_threshold
                ? Math.round(limits.compression_threshold * 100) + "%"
                : "达到容量时"
            }}
          </dd>
        </template>
        <dt>累计令牌</dt>
        <dd>{{ (usage.total_tokens ?? 0).toLocaleString() }}</dd>
        <dt>输入 / 输出</dt>
        <dd>
          {{ (usage.input_tokens ?? 0).toLocaleString() }} /
          {{ (usage.output_tokens ?? 0).toLocaleString() }}
        </dd>
        <dt>模型请求</dt>
        <dd>{{ usage.rounds ?? 0 }} / {{ limit(limits?.max_rounds) }}</dd>
        <dt>工具调用</dt>
        <dd>
          {{ usage.tool_calls ?? 0 }} / {{ limit(limits?.max_tool_calls) }}
        </dd>
        <dt>执行用时</dt>
        <dd>{{ elapsed }}</dd>
        <template v-if="run?.token_budget != null"
          ><dt>预算</dt>
          <dd>{{ limit(run.token_budget) }}</dd></template
        >
        <template v-if="limits">
          <dt>单次输出</dt>
          <dd>{{ limit(limits.max_output_tokens) }}</dd>
          <dt>模型等待</dt>
          <dd>{{ limit(limits.llm_timeout, " 秒") }}</dd>
          <dt>任务时限</dt>
          <dd>{{ limit(limits.run_timeout, " 秒") }}</dd>
        </template>
        <template v-if="usage.estimated_cost != null"
          ><dt>估计费用</dt>
          <dd>${{ usage.estimated_cost.toFixed(4) }}</dd></template
        >
      </dl>
    </div>
  </div>
</template>
