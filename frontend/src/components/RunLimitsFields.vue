<script setup lang="ts">
import { reactive } from "vue";
import type { ProjectRunSettings } from "../types";
const props = defineProps<{
  modelValue: ProjectRunSettings;
  disabled?: boolean;
}>();
const emit = defineEmits<{
  "update:modelValue": [value: ProjectRunSettings];
}>();
type LimitKey = Exclude<keyof ProjectRunSettings, "compression_threshold">;
const remembered = reactive<Partial<Record<LimitKey, number>>>({});
const groups: {
  title: string;
  fields: {
    key: LimitKey;
    label: string;
    unit: string;
    hint: string;
    fallback: number;
    min: number;
  }[];
}[] = [
  {
    title: "模型容量",
    fields: [
      {
        key: "max_output_tokens",
        label: "单次输出",
        unit: "词元",
        hint: "一次回复的推理与正文共用此额度。较复杂的计划需要更多空间。",
        fallback: 8192,
        min: 1,
      },
      {
        key: "max_context_tokens",
        label: "上下文窗口",
        unit: "词元",
        hint: "一次请求可携带的历史与资料。设置上限时，会自动压缩较早的内容。",
        fallback: 262144,
        min: 1024,
      },
    ],
  },
  {
    title: "每次任务的预算",
    fields: [
      {
        key: "token_budget",
        label: "总词元预算",
        unit: "词元",
        hint: "累计统计所有模型请求的输入与输出，包括规划、摘要和验收。",
        fallback: 100000,
        min: 1,
      },
      {
        key: "max_rounds",
        label: "模型请求次数",
        unit: "次",
        hint: "一次任务最多调用模型的次数，包含网络重试。",
        fallback: 50,
        min: 1,
      },
      {
        key: "max_tool_calls",
        label: "工具调用次数",
        unit: "次",
        hint: "一次任务最多使用工具的次数。",
        fallback: 60,
        min: 1,
      },
    ],
  },
  {
    title: "时间限制",
    fields: [
      {
        key: "llm_timeout",
        label: "模型响应等待时间",
        unit: "秒",
        hint: "等待模型返回数据的最长时间，超时后最多自动重试 3 次。",
        fallback: 180,
        min: 1,
      },
      {
        key: "run_timeout",
        label: "任务执行时间",
        unit: "秒",
        hint: "累计执行时长，包含网络重试，不包含等待你回答或审批的时间。",
        fallback: 600,
        min: 1,
      },
    ],
  },
];
function update(key: keyof ProjectRunSettings, value: number): void {
  if (Number.isFinite(value))
    emit("update:modelValue", { ...props.modelValue, [key]: value });
}
function toggle(key: LimitKey, fallback: number, unlimited: boolean): void {
  if (unlimited) remembered[key] = props.modelValue[key] || fallback;
  update(key, unlimited ? 0 : remembered[key] || fallback);
}
</script>

<template>
  <div class="limit-fields">
    <section v-for="group in groups" :key="group.title" class="settings-group">
      <h3>{{ group.title }}</h3>
      <div v-for="field in group.fields" :key="field.key" class="setting-row">
        <div class="setting-description">
          <label :for="`limit-${field.key}`">{{ field.label }}</label>
          <p>{{ field.hint }}</p>
        </div>
        <div class="setting-value">
          <label class="unlimited-toggle">
            <input
              type="checkbox"
              :aria-label="`${field.label}无上限`"
              :checked="modelValue[field.key] === 0"
              :disabled="disabled"
              @change="
                toggle(
                  field.key,
                  field.fallback,
                  ($event.target as HTMLInputElement).checked,
                )
              "
            />无上限
          </label>
          <div
            class="number-with-unit"
            :class="{ unlimited: modelValue[field.key] === 0 }"
          >
            <input
              :id="`limit-${field.key}`"
              type="number"
              :aria-label="`${field.label}上限（${field.unit}）`"
              :min="field.min"
              max="2147483647"
              step="1"
              required
              placeholder="∞"
              :value="modelValue[field.key] || ''"
              :disabled="disabled || modelValue[field.key] === 0"
              @input="
                update(
                  field.key,
                  ($event.target as HTMLInputElement).valueAsNumber,
                )
              "
            />
            <span>{{ field.unit }}</span>
          </div>
        </div>
      </div>
      <div
        v-if="group.title === '模型容量'"
        class="setting-row compression-setting"
      >
        <div class="setting-description">
          <label for="compression-threshold">上下文压缩时机</label>
          <p>
            {{
              modelValue.max_context_tokens
                ? "保留近期对话，较早的历史整理成摘要。"
                : "上下文无上限时，不进行自动容量压缩。"
            }}
          </p>
        </div>
        <select
          id="compression-threshold"
          aria-label="上下文压缩时机"
          :value="modelValue.compression_threshold"
          :disabled="disabled || !modelValue.max_context_tokens"
          @change="
            update(
              'compression_threshold',
              Number(($event.target as HTMLSelectElement).value),
            )
          "
        >
          <option :value="0.95">使用 95% 时</option>
          <option :value="0.9">使用 90% 时</option>
          <option :value="0.8">使用 80% 时</option>
          <option :value="0.5">使用 50% 时</option>
          <option :value="0">达到窗口容量时</option>
          <option
            v-if="
              ![0, 0.5, 0.8, 0.9, 0.95].includes(
                modelValue.compression_threshold,
              )
            "
            :value="modelValue.compression_threshold"
          >
            使用 {{ Math.round(modelValue.compression_threshold * 100) }}% 时
          </option>
        </select>
      </div>
    </section>
  </div>
</template>
