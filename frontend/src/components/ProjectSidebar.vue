<script setup lang="ts">
import { computed, ref, watch } from "vue";
import type {
  Project,
  Session,
  WorkspaceMode,
  EnvironmentSelection,
} from "../types";
import EnvironmentFields from "./EnvironmentFields.vue";
import Pagination from "./Pagination.vue";
import { statusLabels, waitingStatuses } from "../types";
const props = defineProps<{
  projects: Project[];
  sessions: Session[];
  projectId?: string;
  sessionId?: string;
  busy: boolean;
}>();
const emit = defineEmits<{
  project: [id: string];
  session: [id: string];
  createProject: [
    name: string,
    environment: EnvironmentSelection,
    workspacePath: string,
  ];
  environmentSettings: [];
  createSession: [title: string, workspaceMode: WorkspaceMode];
  deleteProject: [id: string, name: string];
  deleteSession: [id: string, name: string];
}>();
const projectName = ref(""),
  projectPath = ref(""),
  sessionTitle = ref(""),
  search = ref(""),
  page = ref(1);
const environment = ref<EnvironmentSelection>({
  mode: "default",
  python: "",
  name: "",
});
const worktree = ref(true),
  showArchived = ref(false);
const mobileOpen = ref(false);
// 历史会话可能以「主题 · 第2轮」或「主题 · 第2轮验收纠错」命名。
const roundMarker =
  /\s*·\s*第\s*[\d０-９零〇一二三四五六七八九十百千两]+\s*轮\s*/u;
const sessionGroups = computed(() => {
  const groups = new Map<string, Session[]>();
  const seen = new Set<string>();
  for (const item of props.sessions) {
    if (
      seen.has(item.id) ||
      (item.archived_at && !showArchived.value && !item.deleted_at)
    )
      continue;
    seen.add(item.id);
    const title = item.title.split(roundMarker, 1)[0].trim() || "未命名会话";
    const group = groups.get(title);
    if (group) group.push(item);
    else groups.set(title, [item]);
  }
  return groups;
});
const visibleSessions = computed(() => {
  const query = search.value.trim().toLocaleLowerCase();
  const rows = [];
  for (const [title, group] of sessionGroups.value) {
    const members = title.toLocaleLowerCase().includes(query)
      ? group
      : group.filter((member) =>
          member.title.toLocaleLowerCase().includes(query),
        );
    if (!members.length) continue;
    const item =
      members.find((member) => member.id === props.sessionId) ??
      members.find((member) => member.active_run_id && !member.deleted_at) ??
      members.find((member) => !member.deleted_at && !member.archived_at) ??
      members.find((member) => !member.deleted_at) ??
      members[0];
    rows.push({ title, members, item });
  }
  return rows;
});
const pageSessions = computed(() =>
  visibleSessions.value.slice((page.value - 1) * 12, page.value * 12),
);
const currentProject = computed(() =>
  props.projects.find((item) => item.id === props.projectId),
);
watch(
  [search, showArchived, () => props.projectId],
  () => {
    page.value = 1;
  },
  { flush: "sync" },
);
watch(
  () => props.sessionId,
  (id) => {
    if (props.sessions.find((item) => item.id === id)?.archived_at)
      showArchived.value = true;
    search.value = "";
    const visibleIndex = visibleSessions.value.findIndex(
      (group) => group.item.id === id,
    );
    if (visibleIndex >= 0) page.value = Math.floor(visibleIndex / 12) + 1;
  },
);
watch(
  () => visibleSessions.value.length,
  (total) => {
    page.value = Math.min(page.value, Math.max(1, Math.ceil(total / 12)));
  },
);
watch(
  () => props.sessions.find((item) => item.id === props.sessionId)?.archived_at,
  (archived) => {
    if (archived) showArchived.value = true;
  },
);
function createProject(): void {
  if (projectName.value.trim())
    emit(
      "createProject",
      projectName.value.trim(),
      { ...environment.value },
      projectPath.value.trim(),
    );
}
function createSession(): void {
  emit(
    "createSession",
    sessionTitle.value.trim() || "新会话",
    worktree.value ? "worktree" : "isolated",
  );
}
function openSession(id: string): void {
  const item = props.sessions.find((member) => member.id === id);
  if (!item) return;
  if (item.deleted_at) {
    emit("deleteSession", item.id, item.title);
    return;
  }
  emit("session", item.id);
  mobileOpen.value = false;
}
function sessionStatus(item: Session): string {
  if (item.deleted_at) return "删除未完成，请重试";
  if (item.archived_at) return "已归档";
  if (item.active_run_id)
    return statusLabels[item.active_status || ""] || "任务进行中";
  return item.workspace_mode === "worktree" ? "独立工作区" : "普通会话";
}
function memberLabel(item: Session): string {
  const time =
    item.created_at &&
    new Date(item.created_at).toLocaleString("zh-CN", { hour12: false });
  return [
    item.title.split(roundMarker)[1],
    time,
    item.id.slice(0, 8),
    item.deleted_at ? "重试删除" : sessionStatus(item),
  ]
    .filter(Boolean)
    .join(" · ");
}
watch(
  () => props.projectId,
  () => {
    projectName.value = "";
    projectPath.value = "";
  },
);
watch(
  () => props.sessionId,
  () => {
    sessionTitle.value = "";
  },
);
</script>
<template>
  <aside class="sidebar" :class="{ 'mobile-open': mobileOpen }">
    <div class="sidebar-heading">
      <div class="brand">
        <span class="brand-icon" aria-hidden="true">/</span
        ><span>MiniHarness<span class="brand-version"> / 2</span></span>
      </div>
      <button
        class="sidebar-mobile-toggle"
        :aria-expanded="mobileOpen"
        @click="mobileOpen = !mobileOpen"
      >
        {{ mobileOpen ? "收起" : "项目与会话" }}
      </button>
    </div>
    <section class="sidebar-project">
      <div class="section-heading">
        <label for="project-select">项目</label>
        <button
          v-if="currentProject"
          class="icon-button subtle"
          aria-label="删除当前项目"
          title="删除项目"
          :disabled="busy"
          @click="emit('deleteProject', currentProject.id, currentProject.name)"
        >
          ×
        </button>
      </div>
      <select
        id="project-select"
        :value="projectId || ''"
        :disabled="busy"
        aria-label="项目"
        @change="emit('project', ($event.target as HTMLSelectElement).value)"
      >
        <option value="" disabled>选择项目</option>
        <option v-for="item in projects" :key="item.id" :value="item.id">
          {{ item.name }}{{ item.deleted_at ? " · 删除未完成" : "" }}
        </option>
      </select>
      <details class="new-project-details" :open="!projects.length">
        <summary><span aria-hidden="true">＋</span> 新建项目</summary>
        <form class="inline-form" @submit.prevent="createProject">
          <input
            v-model="projectName"
            aria-label="新项目名称"
            placeholder="新项目名称"
            maxlength="100"
          />
          <button
            :disabled="busy || !projectName.trim()"
            title="创建项目"
            aria-label="创建项目"
          >
            ＋
          </button>
        </form>
        <details class="environment-create">
          <summary>新项目目录（可选）</summary>
          <label
            >项目目录<input
              v-model="projectPath"
              aria-label="新项目目录"
              placeholder="例如 E:\Code\my-project"
          /></label>
          <p class="small muted">
            留空时在 workspace
            下创建随机英文名的文件夹。也可以指定已有代码目录。
          </p>
        </details>
        <details class="environment-create">
          <summary>新项目运行环境</summary>
          <EnvironmentFields
            v-model="environment"
            prefix="new-project-environment"
          />
        </details>
      </details>
    </section>
    <section class="sidebar-sessions">
      <div class="section-heading">
        <h2>会话</h2>
        <span class="count">{{ visibleSessions.length }}</span>
      </div>
      <input
        v-if="sessionGroups.size > 5 || search"
        v-model="search"
        class="session-search"
        aria-label="搜索会话"
        placeholder="搜索会话…"
      />
      <nav class="session-list" aria-label="项目会话">
        <div
          v-for="{ title, item, members } in pageSessions"
          :key="title"
          class="session-row"
          :class="{ selected: item.id === sessionId }"
        >
          <button
            class="session-select"
            :aria-label="title"
            :aria-current="item.id === sessionId ? 'page' : undefined"
            :disabled="busy || !!item.deleted_at"
            :title="title"
            @click="openSession(item.id)"
          >
            <span
              class="session-mark"
              :class="{
                live: item.active_run_id,
                waiting: waitingStatuses.has(item.active_status || ''),
              }"
            ></span>
            <span class="session-copy"
              ><span class="session-title">{{ title }}</span>
              <span class="session-meta">{{ sessionStatus(item) }}</span>
            </span>
          </button>
          <label
            v-if="members.length > 1"
            class="session-switch"
            title="切换同名会话"
          >
            切换
            <select
              :aria-label="'切换同名会话：' + title"
              :disabled="busy"
              :value="item.id"
              @change="openSession(($event.target as HTMLSelectElement).value)"
            >
              <option
                v-for="member in members"
                :key="member.id"
                :value="member.id"
              >
                {{ memberLabel(member) }}
              </option>
            </select>
          </label>
          <button
            class="session-delete icon-button"
            :aria-label="'删除会话：' + title"
            :disabled="busy"
            :title="members.length > 1 ? '删除所选会话' : '删除会话'"
            @click="
              emit(
                'deleteSession',
                item.id,
                members.length > 1
                  ? item.title + '（' + item.id.slice(0, 8) + '）'
                  : item.title,
              )
            "
          >
            ×
          </button>
        </div>
        <p v-if="!visibleSessions.length" class="sidebar-empty">
          {{ search ? "没有匹配的会话" : "还没有会话" }}
        </p>
      </nav>
      <Pagination
        v-if="visibleSessions.length > 12"
        :page="page"
        :total="visibleSessions.length"
        :page-size="12"
        :busy="busy"
        label="会话分页"
        @change="page = $event"
      />
      <label
        v-if="sessions.some((item) => item.archived_at)"
        class="toggle archive-toggle"
      >
        <input v-model="showArchived" type="checkbox" />显示已归档会话
      </label>
    </section>
    <section
      v-if="projectId && !currentProject?.deleted_at"
      class="new-session"
    >
      <form class="inline-form" @submit.prevent="createSession">
        <input
          v-model="sessionTitle"
          aria-label="新会话名称"
          :disabled="busy"
          placeholder="新会话名称（可选）"
          maxlength="100"
        />
        <button :disabled="busy" title="创建会话" aria-label="创建会话">
          ＋
        </button>
      </form>
      <label class="toggle small"
        ><input
          v-model="worktree"
          type="checkbox"
          :disabled="busy"
        />工作树模式</label
      >
    </section>
    <button class="environment-settings" @click="emit('environmentSettings')">
      <span aria-hidden="true">⚙</span>
      {{ projectId ? "项目设置" : "Python 环境设置" }}
    </button>
  </aside>
</template>
