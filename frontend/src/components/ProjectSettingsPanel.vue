<script setup lang="ts">
import { ref, watch } from "vue";
import type { Project } from "../types";
import ProjectBudgetPanel from "./ProjectBudgetPanel.vue";
import ProjectPathPanel from "./ProjectPathPanel.vue";
import EnvironmentPanel from "./EnvironmentPanel.vue";
import VerificationPanel from "./VerificationPanel.vue";
import IntegrationPanel from "./IntegrationPanel.vue";
const props = defineProps<{
  project: Project;
  busy: boolean;
  revision: number;
  initialTab?: string;
}>();
const emit = defineEmits<{ savePath: [path: string]; close: [] }>();
const tab = ref(props.initialTab || "budget");
watch(
  () => props.initialTab,
  (value) => {
    if (value) tab.value = value;
  },
);
const items = [
  { id: "budget", label: "预算与模型", icon: "◷" },
  { id: "directory", label: "项目目录", icon: "⌘" },
  { id: "environment", label: "运行环境", icon: ">_" },
  { id: "verification", label: "必需检查", icon: "✓" },
  { id: "integrations", label: "工具与技能", icon: "◇" },
];
</script>
<template>
  <section class="project-settings">
    <header class="settings-page-header">
      <div>
        <h1>项目设置</h1>
        <p>{{ project.name }}</p>
      </div>
      <button class="text-button" @click="emit('close')">
        返回工作台 <span aria-hidden="true">↗</span>
      </button>
    </header>
    <div class="settings-layout">
      <nav class="settings-nav" aria-label="项目设置分类">
        <button
          v-for="item in items"
          :key="item.id"
          :class="{ selected: tab === item.id }"
          :aria-current="tab === item.id ? 'page' : undefined"
          @click="tab = item.id"
        >
          <span aria-hidden="true">{{ item.icon }}</span
          >{{ item.label }}
        </button>
      </nav>
      <div class="settings-content">
        <ProjectBudgetPanel
          v-show="tab === 'budget'"
          :key="project.id"
          :project-id="project.id"
        />
        <ProjectPathPanel
          v-if="tab === 'directory'"
          :project="project"
          :busy="busy"
          @save="emit('savePath', $event)"
        />
        <EnvironmentPanel
          v-if="tab === 'environment'"
          :project-id="project.id"
          :revision="revision"
        />
        <VerificationPanel
          v-if="tab === 'verification'"
          :project-id="project.id"
        />
        <IntegrationPanel
          v-if="tab === 'integrations'"
          :project-id="project.id"
        />
      </div>
    </div>
  </section>
</template>
