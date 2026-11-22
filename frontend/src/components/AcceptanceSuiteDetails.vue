<script setup lang="ts">
import type { AcceptanceSuiteRecord } from "../types";
import ReadableValue from "./ReadableValue.vue";

defineProps<{
  suite: AcceptanceSuiteRecord;
  sessionId: string;
}>();
</script>

<template>
  <details>
    <summary>
      <template v-if="suite.from_current_run === false">
        <template v-if="suite.source_session?.id === sessionId"
          >同一对话的较早调用</template
        >
        <template v-else-if="suite.source_session"
          >来自对话「{{ suite.source_session.title }}」</template
        >
        <template v-else>项目保留的回归用例</template>
        ·
      </template>
      <template v-if="suite.status === 'draft'">设计草稿 · </template>
      <template v-if="suite.task_revision"
        >需求第 {{ suite.task_revision }} 版 ·
      </template>
      <template v-if="suite.scope.plan_version"
        >计划第 {{ suite.scope.plan_version }} 版 ·
      </template>
      <template v-if="!suite.scope.final && suite.scope.through_step">
        第 {{ (suite.scope.from_step || 0) + 1 }}–{{ suite.scope.through_step }}
        步 ·
      </template>
      {{ suite.contract.cases?.length ?? 0 }} 个用例
    </summary>
    <p
      v-if="
        suite.from_current_run === false && suite.run_id && suite.source_session
      "
      class="small muted"
    >
      来源调用 <code>{{ suite.run_id }}</code>
    </p>
    <p v-if="!suite.active" class="small">
      已停用 {{ suite.retirement_reason }}
    </p>
    <p v-if="suite.contract.rationale">{{ suite.contract.rationale }}</p>
    <ol v-if="suite.contract.cases?.length" class="case-list">
      <li v-for="item in suite.contract.cases" :key="item.id">
        <strong>{{ item.description || item.id }}</strong>
        <template v-if="item.driver">
          <ReadableValue
            v-if="item.inputs != null"
            name="输入"
            :value="item.inputs"
          />
          <pre class="output"><code>{{ item.driver }}</code></pre>
          <details v-if="item.reference">
            <summary>独立预期计算</summary>
            <pre class="output"><code>{{ item.reference }}</code></pre>
          </details>
        </template>
        <template v-else>
          <ReadableValue name="probe" :value="item.probe" />
          <ReadableValue name="assertions" :value="item.assertions" />
        </template>
      </li>
    </ol>
    <ReadableValue v-else :value="suite.contract" />
    <details v-if="suite.source">
      <summary>生成的测试代码</summary>
      <pre class="output"><code>{{ suite.source }}</code></pre>
    </details>
  </details>
</template>
