<script setup lang="ts">
import { ref, watch } from "vue";
import { request } from "../api";
import type { VerificationPolicy } from "../types";

const props = defineProps<{ projectId: string }>();
const policy = ref<VerificationPolicy | null>(null);
const busy = ref(false),
  error = ref(""),
  notice = ref("");
watch(
  () => props.projectId,
  async () => {
    policy.value = null;
    error.value = "";
    notice.value = "";
    try {
      policy.value = await request<VerificationPolicy>(
        `/projects/${props.projectId}/verification`,
      );
    } catch (cause) {
      error.value = (cause as Error).message;
    }
  },
  { immediate: true },
);

async function save(): Promise<void> {
  busy.value = true;
  error.value = "";
  notice.value = "";
  try {
    policy.value = await request<VerificationPolicy>(
      `/projects/${props.projectId}/verification`,
      "PUT",
      policy.value,
    );
    notice.value = "必需检查已保存；后续验收和合并会使用这份清单。";
  } catch (cause) {
    error.value = (cause as Error).message;
  } finally {
    busy.value = false;
  }
}
function addCheck(): void {
  if (!policy.value) return;
  const checks = policy.value.checks;
  let number = checks.length + 1;
  while (checks.some((check) => check.id === `check_${number}`)) number++;
  checks.push({
    id: `check_${number}`,
    kind: "python",
    path: "",
    args: [],
    min_tests: 1,
  });
}
</script>

<template>
  <section class="card">
    <h2>项目必需检查</h2>
    <p>
      入口按计划中的调用样例验收；直接执行时按任务设计独立用例。此处可额外指定固定脚本，至少保留一项
      pytest 测试检查。
    </p>
    <p class="small">
      固定脚本按填写的参数运行，退出码须为
      0。脚本和测试目录均填写项目内相对路径。
    </p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <form v-if="policy" @submit.prevent="save">
      <fieldset
        v-for="(check, index) in policy.checks"
        :key="index"
        :disabled="busy"
        class="conflict-card"
      >
        <legend>必需检查 {{ index + 1 }}</legend>
        <label
          >检查编号<input
            v-model="check.id"
            required
            :aria-label="`检查编号 ${index + 1}`"
        /></label>
        <label
          >类型<select v-model="check.kind" @change="check.args = []">
            <option value="python">运行项目或冒烟脚本</option>
            <option value="pytest">运行 pytest 测试</option>
          </select></label
        >
        <label
          >入口路径<input
            v-model="check.path"
            required
            :aria-label="`检查入口 ${index + 1}`"
        /></label>
        <label v-if="check.kind === 'python'"
          >运行参数（每行一个）<textarea
            :value="check.args.join('\n')"
            @input="
              check.args = ($event.target as HTMLTextAreaElement).value
                .split('\n')
                .filter(Boolean)
            "
          />
        </label>
        <label v-else
          >最少测试数<input
            v-model.number="check.min_tests"
            type="number"
            min="1"
            required
        /></label>
        <button type="button" @click="policy.checks.splice(index, 1)">
          移除检查
        </button>
      </fieldset>
      <label
        >每项超时（秒）<input
          v-model.number="policy.timeout_seconds"
          type="number"
          min="1"
          max="300"
          required
      /></label>
      <div class="actions">
        <button type="button" :disabled="busy" @click="addCheck">
          增加必需检查
        </button>
        <button class="primary" :disabled="busy">
          {{ busy ? "保存中…" : "保存必需检查" }}
        </button>
      </div>
    </form>
  </section>
</template>
