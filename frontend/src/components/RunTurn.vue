<script setup lang="ts">
import { computed, defineAsyncComponent, ref } from "vue";
import { request } from "../api";
import {
  display,
  displaySummary,
  statusLabels,
  type Run,
  type Session,
  type RunReview,
  type VerificationResult,
} from "../types";
import AcceptancePanel from "./AcceptancePanel.vue";
import ReadableValue from "./ReadableValue.vue";
import ReviewDetails from "./ReviewDetails.vue";
import RunDiff from "./RunDiff.vue";
import VerificationSummary from "./VerificationSummary.vue";

const MarkdownText = defineAsyncComponent(() => import("./MarkdownText.vue"));
const props = defineProps<{
  entry: Run;
  selected?: Run;
  session: Session;
  number: number;
}>();
const expanded = ref(false);
const cached = ref<Run | null>(null);
const loading = ref(false);
const error = ref("");
const detailsOpen = ref(false);
const details = computed(
  () => props.selected || (expanded.value ? cached.value : null),
);
const turn = computed(() => props.selected ?? props.entry);
const result = computed(
  () =>
    details.value?.result as {
      summary?: string;
      review?: RunReview;
      memory?: { status: string; error?: string };
      verification?: VerificationResult;
      publication?: { status: string; path?: string; reason?: string };
    } | null,
);
const answer = computed(() => {
  const summary =
    (turn.value.result as { summary?: string } | null)?.summary ??
    turn.value.reply ??
    result.value?.summary;
  return typeof summary === "string" ? displaySummary(summary) : "";
});
const userMessages = computed(() => {
  if (turn.value.followups) return turn.value.followups;
  const applied = new Set(turn.value.state?.applied_instruction_ids || []);
  return [
    ...(turn.value.state?.user_updates || []),
    ...(turn.value.instructions || [])
      .filter((item) => !applied.has(item.id))
      .map((item) => item.text),
  ];
});
const verification = computed(
  () =>
    result.value?.verification ??
    (details.value?.state?.verification as VerificationResult | undefined),
);
const review = computed(() => {
  const value = result.value?.review;
  return value &&
    (value.decision || value.summary || value.reason || value.checks?.length)
    ? value
    : undefined;
});
const resultDetails = computed(() => {
  const value = details.value?.result;
  if (!value || typeof value !== "object") return {};
  return Object.fromEntries(
    Object.entries(value).filter(
      ([key]) =>
        ![
          "summary",
          "diff",
          "diff_truncated",
          "has_changes",
          "review",
        ].includes(key),
    ),
  );
});
async function loadDetails(): Promise<void> {
  if (cached.value || loading.value) return;
  loading.value = true;
  error.value = "";
  try {
    cached.value = await request<Run>(
      "/runs/" + encodeURIComponent(props.entry.id),
    );
  } catch (cause) {
    error.value = (cause as Error).message;
  } finally {
    loading.value = false;
  }
}
function toggleDetails(): void {
  expanded.value = !expanded.value;
  if (expanded.value) void loadDetails();
}
</script>

<template>
  <article
    class="conversation-turn"
    :aria-label="'第 ' + number + ' 轮'"
    :data-run-id="entry.id"
  >
    <header class="turn-heading">
      <span>第 {{ number }} 轮</span>
      <span class="badge" :data-status="turn.status">{{
        statusLabels[turn.status] || turn.status
      }}</span>
    </header>
    <p class="task-title">{{ turn.task }}</p>
    <p v-for="(message, index) in userMessages" :key="index" class="task-title">
      {{ message }}
    </p>
    <slot v-if="selected" name="activity" />
    <MarkdownText v-if="answer" class="result-output" :text="answer" />
    <ReadableValue
      v-else-if="details?.result != null && typeof details.result !== 'object'"
      :value="details.result"
    />
    <p v-if="turn.status === 'crashed'" class="notice">
      任务意外中断，可以点击“重试任务”重新执行。
    </p>
    <button
      v-if="!selected"
      class="turn-disclosure"
      type="button"
      :aria-expanded="expanded"
      @click="toggleDetails"
    >
      {{ expanded ? "收起本轮详情" : "查看本轮详情" }}
    </button>
    <p
      v-if="expanded && !selected && loading"
      class="small muted"
      role="status"
    >
      正在读取本轮详情…
    </p>
    <p v-if="expanded && !selected && error" class="error" role="alert">
      {{ error }} <button @click="loadDetails">重试</button>
    </p>
    <template v-if="details">
      <slot :item="details" />
      <VerificationSummary
        :result="verification"
        :project-id="session.project_id"
      >
        <AcceptancePanel
          :run-id="details.id"
          :session-id="session.id"
          :verification-id="verification?.id"
        />
      </VerificationSummary>
      <template v-if="details.result">
        <p
          v-if="result?.publication"
          :class="
            result.publication.status === 'pending' ? 'notice' : 'small success'
          "
        >
          {{
            result.publication.status === "written"
              ? "文件已自动写回项目"
              : result.publication.status === "unchanged"
                ? "项目文件已是验收版本"
                : "项目文件待写回"
          }}
          <code v-if="result.publication.path">{{
            result.publication.path
          }}</code>
          <span v-if="result.publication.reason">
            · {{ result.publication.reason }}</span
          >
        </p>
        <p v-if="result?.memory?.status === 'failed'" class="notice">
          记忆保存失败：{{ result.memory.error }}
        </p>
        <RunDiff :run="details" />
        <ReviewDetails
          v-if="review"
          :review="review"
          :run="details"
          :answer="answer"
        />
        <details
          v-if="Object.keys(resultDetails).length"
          @toggle="
            detailsOpen = ($event.currentTarget as HTMLDetailsElement).open
          "
        >
          <summary>查看结果详情</summary>
          <ReadableValue v-if="detailsOpen" :value="resultDetails" />
        </details>
      </template>
      <pre
        v-if="
          details.interrupt &&
          (details.interrupt as { kind?: string }).kind !==
            'plan_clarification' &&
          !['paused', 'awaiting_approval', 'awaiting_tool_approval'].includes(
            details.status,
          )
        "
        class="output"
        >{{ display(details.interrupt) }}</pre
      >
    </template>
  </article>
</template>
