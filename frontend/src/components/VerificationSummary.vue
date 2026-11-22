<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { request } from "../api";
import type { VerificationResult } from "../types";
const props = defineProps<{
  result?: VerificationResult | null;
  projectId?: string;
}>();
type CaseResult = {
  id: string;
  description: string;
  status: string;
  error?: string;
};
const cases = ref<Record<string, CaseResult[] | null> | null>(null);
const loading = ref(false),
  error = ref("");
// 默认 tests 缺失时，这条记录引用同一份独立测试报告。
const checks = computed(() =>
  (props.result?.checks ?? []).filter((check) => !check.origin),
);
const reusedErrors = computed(() =>
  (props.result?.checks ?? []).filter(
    (check) =>
      check.origin &&
      check.error &&
      !checks.value.some((item) => item.error === check.error),
  ),
);
const counts = computed(() => {
  const reported = checks.value.filter(
    (check) => check.tests_collected != null,
  );
  if (!reported.length) return null;
  return {
    passed: reported.reduce((sum, check) => sum + (check.tests_passed ?? 0), 0),
    total: reported.reduce((sum, check) => sum + check.tests_collected!, 0),
  };
});
let generation = 0;
watch([() => props.result?.id, () => props.projectId], () => {
  generation++;
  cases.value = null;
  loading.value = false;
  error.value = "";
});
async function loadCases() {
  if (
    !props.projectId ||
    !props.result?.id ||
    cases.value ||
    loading.value ||
    !checks.value.some((check) =>
      ["pytest", "independent"].includes(check.kind),
    )
  )
    return;
  const version = generation;
  loading.value = true;
  error.value = "";
  try {
    const value = await request<Record<string, CaseResult[] | null>>(
      `/projects/${props.projectId}/verifications/${props.result.id}/cases`,
    );
    if (version === generation) cases.value = value;
  } catch (cause) {
    if (version === generation) error.value = (cause as Error).message;
  } finally {
    if (version === generation) loading.value = false;
  }
}
const outcomes: Record<string, string> = {
  passed: "通过",
  failed: "失败",
  skipped: "跳过",
  incomplete: "记录不完整",
  stale: "结果已失效",
  running: "进行中",
  cancelled: "已取消",
  error: "出错",
};
</script>

<template>
  <section
    v-if="result && (result.id || result.error)"
    aria-label="自动验收结果"
  >
    <details
      :key="result.id || result.error"
      class="verification-report"
      @toggle="($event.currentTarget as HTMLDetailsElement).open && loadCases()"
    >
      <summary>
        <strong
          >{{
            result.scope
              ? result.scope.final
                ? "最终验收"
                : "阶段验收"
              : "自动验收"
          }}
          · {{ outcomes[result.status] || result.status }}</strong
        >
        <span v-if="counts" class="small muted"
          >通过 {{ counts.passed }} / {{ counts.total }} 项测试</span
        >
      </summary>
      <p v-if="result.scope && !result.scope.final" class="small muted">
        第 {{ (result.scope.from_step || 0) + 1 }}–{{
          result.scope.through_step
        }}
        步
      </p>
      <p v-if="result.error" class="error">{{ result.error }}</p>
      <p v-if="loading" class="small">正在读取测试报告…</p>
      <p v-if="error" class="error" role="alert">
        {{ error }} <button @click="loadCases">重试</button>
      </p>
      <div v-for="check in checks" :key="check.id" class="check-report">
        <ul v-if="cases?.[check.id]?.length" class="case-list">
          <li v-for="item in cases[check.id]" :key="item.id">
            <span
              >{{ outcomes[item.status] || item.status }} ·
              {{ item.description || item.id }}</span
            >
            <code
              v-if="item.description && item.description !== item.id"
              class="case-id"
              >{{ item.id }}</code
            >
            <pre v-if="item.error" class="output">{{ item.error }}</pre>
          </li>
        </ul>
        <p v-else-if="check.kind === 'python'" class="small">
          {{ outcomes[check.status] || check.status }} ·
          {{ check.exit_code != null ? "运行 " : "" }}{{ check.path }}
          <span v-if="check.exit_code != null" class="muted"
            >（退出码 {{ check.exit_code }}）</span
          >
        </p>
        <p v-else-if="!loading" class="small">
          {{ outcomes[check.status] || check.status }} ·
          {{
            check.kind === "independent" ? "验收用例" : check.path || check.id
          }}
          <span v-if="check.tests_collected != null" class="muted"
            >（通过 {{ check.tests_passed ?? 0 }} /
            {{ check.tests_collected }}）</span
          >
        </p>
        <p v-if="check.error" class="error">{{ check.error }}</p>
        <details v-if="check.stdout || check.stderr" class="check-output">
          <summary>
            {{
              check.kind === "independent" ? "验收用例" : check.path || check.id
            }}
            · 运行输出
          </summary>
          <pre v-if="check.stdout" class="output">{{ check.stdout }}</pre>
          <pre v-if="check.stderr" class="output">{{ check.stderr }}</pre>
        </details>
      </div>
      <p v-for="check in reusedErrors" :key="check.id" class="error">
        {{ check.path }} · {{ check.error }}
      </p>
      <slot />
    </details>
  </section>
</template>
