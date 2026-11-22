<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { request } from "../api";
import type { AcceptanceSuiteRecord } from "../types";
import AcceptanceSuiteDetails from "./AcceptanceSuiteDetails.vue";

const props = defineProps<{
  runId: string;
  sessionId: string;
  verificationId?: string;
}>();
const suites = ref<AcceptanceSuiteRecord[]>([]);
const current = computed(() =>
  suites.value.filter(
    (suite) =>
      suite.used_in_verification === true && suite.from_current_run !== false,
  ),
);
const regression = computed(() =>
  suites.value.filter(
    (suite) =>
      suite.used_in_verification === true && suite.from_current_run === false,
  ),
);
const history = computed(() =>
  suites.value
    .filter(
      (suite) =>
        suite.used_in_verification !== true && suite.from_current_run !== false,
    )
    .reverse(),
);
const hasRecord = computed(
  () =>
    Boolean(props.verificationId) ||
    suites.value.some(
      (suite) => typeof suite.used_in_verification === "boolean",
    ),
);

const loaded = ref(false),
  busy = ref(false),
  error = ref("");
let version = 0;
watch(
  () => [props.runId, props.verificationId] as const,
  ([runId], [previousRun]) => {
    const reload = runId === previousRun && (loaded.value || busy.value);
    version++;
    suites.value = [];
    loaded.value = busy.value = false;
    error.value = "";
    if (reload) void load();
  },
);
async function load(): Promise<void> {
  const requested = ++version;
  busy.value = true;
  error.value = "";
  const query = props.verificationId
    ? `?verification_id=${encodeURIComponent(props.verificationId)}`
    : "";
  try {
    const values = await request<AcceptanceSuiteRecord[]>(
      `/runs/${props.runId}/acceptance${query}`,
    );
    if (requested === version) {
      suites.value = values;
      loaded.value = true;
    }
  } catch (cause) {
    if (requested === version) error.value = (cause as Error).message;
  } finally {
    if (requested === version) busy.value = false;
  }
}
</script>

<template>
  <section aria-label="独立测试契约">
    <button :disabled="busy" @click="load">
      {{ busy ? "加载中…" : "查看独立测试契约与代码" }}
    </button>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <template v-if="loaded">
      <p v-if="!hasRecord" class="small muted">本次调用尚无验收结果。</p>
      <p v-else-if="!current.length && !regression.length" class="small muted">
        这份验收结果没有关联的独立用例。
      </p>
      <template v-if="current.length">
        <h4>本次调用采用的用例</h4>
        <AcceptanceSuiteDetails
          v-for="suite in current"
          :key="suite.id"
          :suite="suite"
          :session-id="sessionId"
        />
      </template>
      <details v-if="regression.length">
        <summary>本次验收复用的回归用例（{{ regression.length }} 组）</summary>
        <p class="small muted">
          这些用例来自更早的调用，在本次文件版本上重新执行。
        </p>
        <AcceptanceSuiteDetails
          v-for="suite in regression"
          :key="suite.id"
          :suite="suite"
          :session-id="sessionId"
        />
      </details>
      <details v-if="history.length">
        <summary>
          {{ hasRecord ? "本次调用的用例设计历史" : "本次调用的用例设计" }}（{{
            history.length
          }}
          组）
        </summary>
        <AcceptanceSuiteDetails
          v-for="suite in history"
          :key="suite.id"
          :suite="suite"
          :session-id="sessionId"
        />
      </details>
      <p v-if="current.length || regression.length" class="small muted">
        修复后重跑同一组用例时，仍按原先的输入和预期结果判断。
      </p>
    </template>
  </section>
</template>
