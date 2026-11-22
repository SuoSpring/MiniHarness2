<script setup lang="ts">
import { computed, ref, watch } from "vue";
import {
  display,
  type Plan,
  type PlanApproval,
  type PlanEntryCheck,
  type Run,
} from "../types";
const props = defineProps<{ run: Run; busy: boolean; latest: boolean }>();
const emit = defineEmits<{
  approve: [body: PlanApproval];
}>();
const feedback = ref("");
const interruption = computed(
  () =>
    props.run.interrupt as {
      plan?: Plan;
      plan_version?: number;
      message?: string;
    } | null,
);
const waiting = computed(() =>
  ["awaiting_approval", "waiting_approval"].includes(props.run.status),
);
// 审批查看新候选；已执行计划仍留在 state.plan 中，不能用旧版替代待审版本。
const plan = computed(() =>
  waiting.value
    ? interruption.value?.plan || props.run.state?.pending_plan
    : props.run.state?.plan,
);
const criteria = computed(() => {
  const steps = plan.value?.steps ?? [];
  return (plan.value?.criteria ?? props.run.state?.criteria ?? []).map(
    (item) => ({
      ...item,
      positions: steps.flatMap((step, index) =>
        (
          item.step_ids
            ? item.step_ids.includes(step.id ?? "")
            : step.criterion_ids?.includes(item.id)
        )
          ? [index + 1]
          : [],
      ),
    }),
  );
});
watch([() => props.run.id, () => plan.value?.version], () => {
  feedback.value = "";
});
function decide(decision: PlanApproval["decision"]): void {
  emit("approve", {
    plan_version: plan.value?.version || interruption.value?.plan_version || 1,
    decision,
    feedback: feedback.value.trim(),
  });
}
function entryCommand(entry: PlanEntryCheck): string {
  return ["python", entry.path, ...entry.args]
    .map((arg) =>
      /^[\p{L}\p{N}_./:+-]+$/u.test(arg) ? arg : JSON.stringify(arg),
    )
    .join(" ");
}
</script>

<template>
  <section
    v-if="plan?.steps?.length || waiting"
    class="card plan-card"
    :class="{ 'needs-attention': waiting }"
  >
    <div class="section-heading">
      <h2>{{ waiting ? "请审阅这份执行计划" : "执行计划" }}</h2>
      <span class="badge">第 {{ plan?.version || 1 }} 版</span>
    </div>
    <p v-if="plan?.goal">{{ plan.goal }}</p>
    <p v-if="plan?.rationale" class="muted small">{{ plan.rationale }}</p>
    <p v-if="plan?.changed_assumption" class="notice">
      这次调整：{{ plan.changed_assumption }}
    </p>
    <p v-if="plan?.checkpoint_steps?.length" class="small muted">
      后续检查点：完成第
      {{ plan.checkpoint_steps.join("、") }} 步后。最后一次覆盖全部需求。
    </p>
    <ol v-if="plan?.steps?.length" class="plan-steps" aria-label="有序实施步骤">
      <li v-for="(step, index) in plan.steps" :key="step.id || index">
        <strong>{{ step.title || step.description || display(step) }}</strong>
        <span v-if="plan.retained_steps?.includes(step.id || '')" class="badge"
          >保留</span
        >
        <span
          v-else-if="
            (plan.version || 1) > 1 &&
            plan.replanned_steps?.includes(step.id || '')
          "
          class="badge"
          >重新规划</span
        >
        <span v-if="step.status" class="small muted">{{ step.status }}</span>
      </li>
    </ol>
    <pre v-else-if="run.interrupt" class="output">{{
      display(run.interrupt)
    }}</pre>
    <template v-if="plan?.steps?.length && criteria.length">
      <h3>本次验收标准</h3>
      <ul aria-label="验收条目">
        <li v-for="criterion in criteria" :key="criterion.id">
          <span>{{ criterion.description }}</span>
          <span v-if="criterion.positions.length" class="small muted">
            （关联第 {{ criterion.positions.join("、") }} 步）
          </span>
        </li>
      </ul>
    </template>
    <template v-if="plan?.entry_checks?.length">
      <h3>计划入口检查</h3>
      <ul aria-label="计划入口检查">
        <li v-for="(entry, index) in plan.entry_checks" :key="index">
          <code>{{ entryCommand(entry) }}</code>
          <span>
            · 预期退出码
            {{ entry.exit_code === "nonzero" ? "非零" : entry.exit_code }}</span
          >
          <span v-if="entry.stdin">
            · 标准输入 <code>{{ JSON.stringify(entry.stdin) }}</code></span
          >
          <span v-if="entry.stdout != null">
            · 预期输出 <code>{{ JSON.stringify(entry.stdout) }}</code></span
          >
          <span v-if="entry.stdout_reference" class="small muted">
            （由参考函数
            <code>{{ entry.stdout_reference.function }}</code> 计算）</span
          >
          <span class="small muted">（{{ entry.criterion_id }}）</span>
        </li>
      </ul>
    </template>
    <template v-if="waiting && latest">
      <label for="plan-feedback">修改意见（要求修改时必填）</label>
      <textarea
        id="plan-feedback"
        v-model="feedback"
        rows="2"
        placeholder="例如：先确认现有测试，再修改数据库结构。"
      ></textarea>
      <div class="actions">
        <button class="primary" :disabled="busy" @click="decide('approve')">
          批准计划</button
        ><button
          :disabled="busy || !feedback.trim()"
          @click="decide('changes')"
        >
          要求修改</button
        ><button
          class="danger subtle"
          :disabled="busy"
          @click="decide('reject')"
        >
          拒绝执行
        </button>
      </div>
    </template>
  </section>
</template>
