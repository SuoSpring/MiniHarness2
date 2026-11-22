<script setup lang="ts">
import { computed, defineAsyncComponent, ref } from "vue";
import type { Run, RunReview } from "../types";
const MarkdownText = defineAsyncComponent(() => import("./MarkdownText.vue"));

const props = defineProps<{ review: RunReview; run: Run; answer?: string }>();
const open = ref(false);
const decisions: Record<string, string> = {
  complete: "审查通过",
  next: "进入下一步",
  repair: "需要修复",
  replan: "需要重新规划",
  blocked: "受阻",
};
const criteria = computed(
  () =>
    new Map([
      ["goal", props.run.task] as const,
      ...(props.run.state?.criteria ?? []).map(
        (item) => [item.id, item.description] as const,
      ),
      ...(props.run.state?.plan?.criteria ?? []).map(
        (item) => [item.id, item.description] as const,
      ),
    ]),
);
const summary = computed(() => {
  const value = props.review.summary?.replaceAll("\r\n", "\n").trim();
  const answer = props.answer?.replaceAll("\r\n", "\n").trim();
  if (!value || answer === value || answer?.endsWith("\n\n" + value)) return "";
  return value;
});
</script>

<template>
  <details
    class="run-review"
    @toggle="open = ($event.currentTarget as HTMLDetailsElement).open"
  >
    <summary>审查意见</summary>
    <template v-if="open">
      <p v-if="review.decision" class="small">
        {{ decisions[review.decision] || review.decision }}
      </p>
      <MarkdownText v-if="summary" :text="summary" />
      <MarkdownText
        v-if="review.reason && review.reason !== review.summary"
        :text="review.reason"
      />
      <ul
        v-if="review.checks?.length"
        class="review-checks"
        aria-label="逐项审查"
      >
        <li v-for="check in review.checks" :key="check.criterion_id">
          <strong>{{
            criteria.get(check.criterion_id) || check.criterion_id
          }}</strong>
          <span
            class="badge"
            :data-status="check.passed ? 'completed' : 'failed'"
          >
            {{ check.passed ? "通过" : "未通过" }}
          </span>
          <MarkdownText v-if="check.reason" :text="check.reason" />
          <details v-if="check.evidence_ids?.length">
            <summary>查看证据</summary>
            <ul>
              <li v-for="id in check.evidence_ids" :key="id">
                <code>{{ id }}</code>
              </li>
            </ul>
          </details>
        </li>
      </ul>
    </template>
  </details>
</template>
