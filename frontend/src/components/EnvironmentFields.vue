<script setup lang="ts">
import type { EnvironmentSelection, PythonEnvironment } from "../types";
const props = defineProps<{
  modelValue: EnvironmentSelection;
  prefix: string;
  environments?: PythonEnvironment[];
}>();
const emit = defineEmits<{
  "update:modelValue": [value: EnvironmentSelection];
}>();
function update(key: keyof EnvironmentSelection, event: Event): void {
  emit("update:modelValue", {
    ...props.modelValue,
    [key]: (event.target as HTMLInputElement).value,
  });
}
</script>

<template>
  <label :for="`${prefix}-mode`">环境来源</label>
  <select
    :id="`${prefix}-mode`"
    :value="modelValue.mode"
    @change="update('mode', $event)"
  >
    <option value="default">使用后端默认环境</option>
    <option value="existing">选择已有环境</option>
    <option value="managed">创建或复用项目独立环境</option>
  </select>
  <template v-if="modelValue.mode !== 'default'">
    <label :for="`${prefix}-python`">{{
      modelValue.mode === "managed"
        ? "基础 Python（留空使用默认环境）"
        : "Python 解释器或环境目录"
    }}</label>
    <input
      :id="`${prefix}-python`"
      :value="modelValue.python"
      :required="modelValue.mode === 'existing'"
      :list="`${prefix}-choices`"
      placeholder="绝对路径或已发现的环境名称"
      @input="update('python', $event)"
    />
    <datalist :id="`${prefix}-choices`">
      <option
        v-for="environment in environments || []"
        :key="`${environment.python}:${environment.name}`"
        :value="environment.python"
      >
        {{ environment.name }}
      </option>
    </datalist>
    <label :for="`${prefix}-name`">{{
      modelValue.mode === "managed" ? "独立环境名称" : "显示名称（可选）"
    }}</label>
    <input
      :id="`${prefix}-name`"
      :value="modelValue.name"
      :placeholder="modelValue.mode === 'managed' ? 'venv' : '例如：数据分析'"
      maxlength="80"
      @input="update('name', $event)"
    />
  </template>
</template>
