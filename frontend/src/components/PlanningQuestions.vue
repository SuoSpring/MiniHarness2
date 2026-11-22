<script setup lang="ts">
import { computed, ref, watch } from "vue";
import type { PlanningAnswers, PlanningRequest, Run } from "../types";

const props = defineProps<{ run: Run; busy: boolean; latest: boolean }>();
const emit = defineEmits<{ answer: [body: { answer: PlanningAnswers }] }>();
const request = computed(() => {
  const value = props.run.interrupt as PlanningRequest | null;
  return props.run.status === "awaiting_input" &&
    value?.kind === "plan_clarification"
    ? value
    : null;
});
const choices = ref<Record<string, string>>({});
const notes = ref<Record<string, string>>({});
const hasChoices = computed(() =>
  request.value?.questions.some((question) => question.options.length),
);
watch([() => props.run.id, () => request.value?.request_id], () => {
  choices.value = {};
  notes.value = {};
});
const ready = computed(() =>
  request.value?.questions.every(
    (question) =>
      choices.value[question.id] || notes.value[question.id]?.trim(),
  ),
);
function submit(): void {
  if (!request.value || !ready.value || props.busy || !props.latest) return;
  emit("answer", {
    answer: {
      request_id: request.value.request_id,
      answers: request.value.questions.map((question) => ({
        question_id: question.id,
        option_id: choices.value[question.id] || "",
        text: notes.value[question.id]?.trim() || "",
      })),
    },
  });
}
</script>

<template>
  <section
    v-if="request"
    class="card needs-attention"
    aria-label="规划设计选择"
  >
    <h2>{{ hasChoices ? "确认方案" : "补充信息" }}</h2>
    <p v-if="request.feedback" class="notice" role="alert">
      {{ request.feedback }}
    </p>
    <form @submit.prevent="submit">
      <fieldset
        v-for="question in request.questions"
        :key="question.id"
        class="planning-question"
        :disabled="busy || !latest"
      >
        <legend>{{ question.question }}</legend>
        <p class="muted small">{{ question.reason }}</p>
        <label
          v-for="option in question.options"
          :key="option.id"
          class="planning-option"
        >
          <input
            v-model="choices[question.id]"
            type="radio"
            :name="question.id"
            :value="option.id"
          />
          <span>
            <strong>{{ option.label }}</strong>
            <span
              v-if="question.recommended_option_id === option.id"
              class="badge"
              >推荐</span
            >
            <span class="option-description">{{ option.description }}</span>
          </span>
        </label>
        <label v-if="question.options.length" class="planning-option">
          <input
            v-model="choices[question.id]"
            type="radio"
            :name="question.id"
            value=""
          />
          <span>使用自定义方案</span>
        </label>
        <label :for="`planning-note-${question.id}`">{{
          question.options.length ? "补充说明或自定义方案" : "你的回答"
        }}</label>
        <textarea
          :id="`planning-note-${question.id}`"
          v-model="notes[question.id]"
          rows="2"
          :placeholder="question.options.length ? '补充说明…' : '填写所需信息…'"
        ></textarea>
      </fieldset>
      <button
        v-if="latest"
        class="primary"
        :disabled="busy || !ready"
        type="submit"
      >
        {{
          request.return_node === "executor"
            ? "提交回答并继续"
            : "提交回答并继续规划"
        }}
      </button>
    </form>
  </section>
</template>

<style scoped>
.planning-question {
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 16px;
  margin: 0 0 18px;
  min-width: 0;
}
legend {
  padding: 0 6px;
  font-weight: 600;
}
.planning-option {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 0;
  cursor: pointer;
}
.planning-option input {
  width: 16px;
  height: 16px;
  flex: 0 0 16px;
  margin: 3px 0 0;
  accent-color: var(--green);
}
.planning-option .badge {
  margin-left: 8px;
}
.option-description {
  display: block;
  color: var(--muted);
  margin-top: 3px;
}
</style>
