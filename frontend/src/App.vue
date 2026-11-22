<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import ProjectSidebar from "./components/ProjectSidebar.vue";
import RunComposer from "./components/RunComposer.vue";
import RunUsage from "./components/RunUsage.vue";
import PlanReview from "./components/PlanReview.vue";
import PlanningQuestions from "./components/PlanningQuestions.vue";
import ToolApproval from "./components/ToolApproval.vue";
import RunInspector from "./components/RunInspector.vue";
import ChangePanel from "./components/ChangePanel.vue";
import FilePanel from "./components/FilePanel.vue";
import MemoryPanel from "./components/MemoryPanel.vue";
import EnvironmentPanel from "./components/EnvironmentPanel.vue";
import ConfirmDialog from "./components/ConfirmDialog.vue";
import ProjectSettingsPanel from "./components/ProjectSettingsPanel.vue";
import { request } from "./api";
import { useWorkbench } from "./composables/useWorkbench";
import type { PermissionPolicy, Run } from "./types";

const {
  projects,
  sessions,
  project,
  session,
  runs,
  run,
  events,
  error,
  busy,
  revision,
  latestRunId,
  act,
  initialize,
  selectProject,
  selectSession,
  selectRun,
  createProject,
  setProjectPath,
  createSession,
  deleteProject,
  deleteSession,
  retryRun,
  sendMessage,
  control,
  refreshSession,
} = useWorkbench();
const section = ref("work");
const inspector = ref<InstanceType<typeof RunInspector> | null>(null);
async function send(
  task: string,
  mode: Run["mode"],
  permissions: PermissionPolicy,
): Promise<boolean> {
  const owner = session.value?.id;
  const sent = await act(() => sendMessage(task, mode, permissions));
  if (sent && session.value?.id === owner) await inspector.value?.showLatest();
  return sent;
}
const settingsTab = ref("budget");
function openSettings(tab = "budget"): void {
  settingsTab.value = tab;
  section.value = project.value ? "project" : "environment";
}
const confirmation = ref<{
  kind: "project" | "session" | "cancel";
  id: string;
  name: string;
} | null>(null);
const preservedPath = ref("");
async function confirmDelete(
  kind: "project" | "session",
  id: string,
  name: string,
): Promise<void> {
  error.value = "";
  preservedPath.value = "";
  if (kind === "project") {
    const ok = await act(async () => {
      const preview = await request<{ preserved_path?: string | null }>(
        `/projects/${id}/deletion-preview`,
      );
      preservedPath.value = preview.preserved_path || "";
    });
    if (!ok) return;
  }
  confirmation.value = { kind, id, name };
}
async function confirmAction(): Promise<void> {
  const target = confirmation.value;
  if (!target) return;
  const ok = await act(() =>
    target.kind === "project"
      ? deleteProject(target.id)
      : target.kind === "session"
        ? deleteSession(target.id)
        : control("cancel"),
  );
  if (ok) {
    confirmation.value = null;
    section.value = "work";
  }
}
const latest = computed(() => run.value?.id === latestRunId.value);
const activeRun = computed(() => {
  const id = session.value?.active_run_id;
  if (!id) return undefined;
  return run.value?.id === id
    ? run.value
    : runs.value.find((item) => item.id === id);
});
const canRollback = computed(
  () => !!run.value && session.value?.undo_run_id === run.value.id,
);
async function changed(runId?: Run["id"]): Promise<void> {
  await refreshSession(runId ?? run.value?.id);
  revision.value++;
}
onMounted(() => {
  void act(initialize);
});
</script>

<template>
  <div class="workbench">
    <ProjectSidebar
      :projects="projects"
      :sessions="sessions"
      :project-id="project?.id"
      :session-id="session?.id"
      :busy="busy"
      @project="(id) => act(() => selectProject(id))"
      @session="(id) => act(() => selectSession(id))"
      @create-project="
        (name, environment, workspacePath) =>
          act(async () => {
            await createProject(name, environment, workspacePath);
            section = 'work';
          })
      "
      @environment-settings="openSettings()"
      @create-session="
        (title, mode) =>
          act(async () => {
            await createSession(title, mode);
            section = 'work';
          })
      "
      @delete-project="(id, name) => confirmDelete('project', id, name)"
      @delete-session="(id, name) => confirmDelete('session', id, name)"
    />
    <main :class="{ 'settings-open': section === 'project' }">
      <header class="workspace-header">
        <div>
          <p class="eyebrow">
            <span class="breadcrumb-mark" aria-hidden="true">/</span
            >{{ project?.name || "MiniHarness"
            }}<span v-if="session" class="breadcrumb-separator">/ 会话</span>
          </p>
          <h1>
            {{ session?.title || (project ? "新建会话" : "项目工作台") }}
          </h1>
          <span v-if="session?.archived_at" class="badge">已归档</span>
          <span v-else-if="session?.workspace_mode === 'worktree'" class="badge"
            >独立工作区</span
          >
        </div>
        <span v-if="session" class="header-label"
          >{{ runs.length }} 次执行</span
        >
      </header>
      <button
        v-if="project"
        class="project-directory-link"
        :title="project.workspace_path || '选择本地代码目录'"
        @click="openSettings('directory')"
      >
        {{ project.workspace_path || "设置项目目录" }}
      </button>
      <div
        v-if="error && !confirmation"
        class="error global-error"
        role="alert"
      >
        <strong>操作未完成</strong><span>{{ error }}</span
        ><button aria-label="关闭错误提示" @click="error = ''">×</button>
      </div>
      <nav v-if="project" class="section-tabs" aria-label="工作台导航">
        <button
          v-for="item in [
            { id: 'work', label: '工作台' },
            { id: 'changes', label: '变更审阅' },
            { id: 'files', label: '文件' },
            { id: 'memory', label: '记忆' },
          ]"
          :key="item.id"
          :class="{ selected: section === item.id }"
          :aria-pressed="section === item.id"
          :disabled="
            !!project.deleted_at ||
            (!session && !['project', 'environment'].includes(item.id))
          "
          @click="section = item.id"
        >
          {{ item.label }}
        </button>
      </nav>
      <ProjectSettingsPanel
        v-if="section === 'project' && project"
        :key="project.id"
        :project="project"
        :busy="busy"
        :revision="revision"
        :initial-tab="settingsTab"
        @save-path="(path) => act(() => setProjectPath(path))"
        @close="section = 'work'"
      />
      <EnvironmentPanel
        v-if="section === 'environment'"
        :project-id="project?.id"
        :revision="revision"
      />
      <template v-if="session && project">
        <div v-show="section === 'work'" class="work-area">
          <p v-if="session.archived_at" class="notice">
            会话已归档，可以查看历史、文件和验收记录。
          </p>
          <div class="compose-column">
            <RunComposer
              v-if="!session.archived_at"
              :key="session.id"
              :run="run"
              :latest="latest"
              :busy="busy"
              :has-active="!!session.active_run_id"
              :active-run="activeRun"
              :enabled="!!session"
              :session-id="session.id"
              :send="send"
              @control="(...args) => act(() => control(...args))"
              @retry="act(retryRun)"
              @cancel="
                confirmation = { kind: 'cancel', id: run!.id, name: run!.task }
              "
            />
            <RunUsage v-else-if="run" :run="run" />
          </div>
          <div class="execution-column">
            <RunInspector
              ref="inspector"
              :key="session.id"
              :run="run"
              :runs="runs"
              :events="events"
              :session="session"
              :busy="busy"
              :revision="revision"
              @select="(id) => act(() => selectRun(id))"
            >
              <template #default="{ item }">
                <PlanningQuestions
                  :run="item"
                  :busy="busy"
                  :latest="latest && item.id === run?.id"
                  @answer="(body) => act(() => control('resume', body))"
                /><PlanReview
                  v-if="item.status === 'awaiting_approval'"
                  :run="item"
                  :busy="busy"
                  :latest="latest && item.id === run?.id"
                  @approve="(body) => act(() => control('approve', body))"
                />
                <details
                  v-else-if="
                    item.state?.plan?.steps?.length ||
                    item.state?.pending_plan?.steps?.length
                  "
                  class="saved-plan"
                >
                  <summary>查看执行计划</summary>
                  <PlanReview
                    :run="item"
                    :busy="busy"
                    :latest="latest && item.id === run?.id"
                    @approve="(body) => act(() => control('approve', body))"
                  />
                </details>
                <ToolApproval
                  :run="item"
                  :busy="busy"
                  :latest="latest && item.id === run?.id"
                  @approve="(body) => act(() => control('approve-tool', body))"
                />
              </template>
            </RunInspector>
          </div>
        </div>
        <ChangePanel
          v-if="section === 'changes'"
          :key="session.id"
          :session-id="session.id"
          :project-id="session.project_id"
          :run="run"
          :can-rollback="canRollback"
          :archived="!!session.archived_at"
          :has-active="!!session.active_run_id"
          @changed="(id) => act(() => changed(id))"
        />
        <FilePanel
          v-if="section === 'files'"
          :key="session.id"
          :project-id="project.id"
          :session-id="session.id"
          :revision="revision"
        />
        <MemoryPanel
          v-if="section === 'memory'"
          :key="session.id"
          :project-id="project.id"
          :session-id="session.id"
          :revision="revision"
        />
      </template>
      <section
        v-else-if="!['environment', 'project'].includes(section)"
        class="welcome card"
      >
        <span class="welcome-mark" aria-hidden="true">[ / ]</span>
        <h2>
          {{
            project?.deleted_at
              ? "项目清理未完成"
              : project
                ? "还没有会话"
                : "新建项目"
          }}
        </h2>
        <p>
          {{
            project?.deleted_at
              ? "请再次点击左侧的项目删除按钮，继续清理。"
              : project
                ? "新建会话，描述你想完成的任务。"
                : "在左侧填写项目名称，开始工作。"
          }}
        </p>
      </section>
    </main>
    <ConfirmDialog
      v-if="confirmation"
      :title="
        confirmation.kind === 'cancel'
          ? '停止当前任务？'
          : `删除${confirmation.kind === 'project' ? '项目' : '会话'}「${confirmation.name}」？`
      "
      :message="
        confirmation.kind === 'cancel'
          ? '正在执行的请求会停止，未提交的修改不会发布。之后可以重新尝试。'
          : confirmation.kind === 'project'
            ? `永久删除项目记录、会话工作区、日志、补丁和检查点。${preservedPath ? '已合并代码保留在：' + preservedPath : '不会删除其他项目的文件。'}`
            : '永久删除该会话的工作区、日志、补丁、执行记录和检查点。其他会话以及已合并到项目的内容会保留。'
      "
      :action="confirmation.kind === 'cancel' ? '确认停止' : '确认删除'"
      :busy="busy"
      :error="error"
      @close="
        confirmation = null;
        error = '';
      "
      @confirm="confirmAction"
    />
  </div>
</template>
