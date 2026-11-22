import { computed, onUnmounted, ref } from "vue";
import { request, subscribeRun } from "../api";
import {
  terminalStatuses,
  waitingStatuses,
  type PermissionPolicy,
  type PlanningRequest,
  type Project,
  type EnvironmentSelection,
  type Run,
  type RunControlArgs,
  type RunEvent,
  type Session,
  type WorkspaceMode,
} from "../types";

export function useWorkbench() {
  const projects = ref<Project[]>([]),
    sessions = ref<Session[]>([]);
  const project = ref<Project | null>(null),
    session = ref<Session | null>(null);
  const runs = ref<Run[]>([]),
    run = ref<Run | null>(null),
    events = ref<RunEvent[]>([]);
  const error = ref(""),
    busy = ref(false),
    revision = ref(0);
  const latestRunId = computed(() => runs.value[0]?.id);
  // 详情请求仍在途中时，也能在撤回后取消对旧调用的选择。
  let selectedRunId: string | undefined;
  let stream: EventSource | null = null;
  let refreshTimer: ReturnType<typeof setTimeout> | undefined;
  let pollTimer: ReturnType<typeof setInterval> | undefined;
  let projectVersion = 0,
    sessionVersion = 0,
    sessionRefreshVersion = 0,
    runVersion = 0,
    operations = 0;
  let polling = false;

  function saveSelection(): void {
    try {
      localStorage.setItem(
        "miniharness2.selection",
        JSON.stringify({
          project: project.value?.id,
          session: session.value?.id,
        }),
      );
    } catch {
      /* 存储不可用时仍可使用工作台。 */
    }
  }
  function detach(): void {
    stream?.close();
    stream = null;
    clearTimeout(refreshTimer);
    refreshTimer = undefined;
    runVersion++;
  }
  function clearSession(): void {
    detach();
    sessionVersion++;
    selectedRunId = undefined;
    session.value = null;
    run.value = null;
    runs.value = [];
    events.value = [];
  }
  async function act(operation: () => Promise<void>): Promise<boolean> {
    operations++;
    busy.value = true;
    error.value = "";
    try {
      await operation();
      return true;
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause);
      return false;
    } finally {
      busy.value = --operations > 0;
    }
  }
  async function refreshSession(preferredRunId?: string): Promise<void> {
    if (!session.value) return;
    const id = session.value.id,
      version = sessionVersion,
      refresh = ++sessionRefreshVersion;
    const [current, executions] = await Promise.all([
      request<Session>(`/sessions/${id}`),
      request<Run[]>(`/sessions/${id}/runs?summary=true`),
    ]);
    if (
      sessionVersion !== version ||
      session.value?.id !== id ||
      refresh !== sessionRefreshVersion
    )
      return;
    session.value = current;
    sessions.value = sessions.value.map((item) =>
      item.id === id ? current : item,
    );
    const previousLatest = latestRunId.value;
    runs.value = executions.filter((item) => item.status !== "rolled_back");
    const preferred = runs.value.find((item) => item.id === preferredRunId);
    const browsingOlder =
      selectedRunId !== previousLatest &&
      runs.value.some((item) => item.id === selectedRunId);
    const next =
      preferred?.id ?? (browsingOlder ? selectedRunId : latestRunId.value);
    if (preferred || next !== selectedRunId) await selectRun(next);
  }
  async function refreshRun(): Promise<void> {
    if (!run.value) return;
    const id = run.value.id,
      version = runVersion;
    const current = await request<Run>(`/runs/${id}`);
    if (version !== runVersion || run.value?.id !== id) return;
    if (current.status === "rolled_back") {
      await refreshSession();
      revision.value++;
      return;
    }
    const changed = current.status !== run.value.status;
    run.value = current;
    runs.value = runs.value.map((item) => (item.id === id ? current : item));
    if (
      terminalStatuses.has(current.status) ||
      waitingStatuses.has(current.status)
    ) {
      const justStopped = changed || stream !== null;
      stream?.close();
      stream = null;
      if (justStopped) await refreshSession();
      if (version === runVersion && changed) revision.value++;
    } else if (!stream && version === runVersion) {
      // 另一页面恢复任务后，重新接上实时事件。
      await selectRun(id);
    }
  }
  async function selectRun(id?: string): Promise<void> {
    detach();
    selectedRunId = id;
    const version = runVersion,
      owner = session.value?.id;
    events.value = [];
    if (run.value?.id !== id) run.value = null;
    if (!id) return;
    const current = await request<Run>(`/runs/${id}`);
    if (version !== runVersion || owner !== session.value?.id) return;
    if (current.status === "rolled_back") {
      await refreshSession();
      return;
    }
    run.value = current;
    stream = subscribeRun(id, "0", (event) => {
      if (version !== runVersion || run.value?.id !== id) return;
      if (!events.value.some((item) => item.id === event.id))
        events.value = [...events.value.slice(-99), event];
      // 节流而非不断推迟刷新；持续输出时状态仍能及时更新。
      if (!refreshTimer)
        refreshTimer = setTimeout(() => {
          refreshTimer = undefined;
          void refreshRun().catch(() => {
            /* 定时刷新将重试。 */
          });
        }, 200);
    });
    stream.addEventListener("error", (event) => {
      if (
        !(event instanceof MessageEvent) &&
        version === runVersion &&
        run.value &&
        (terminalStatuses.has(run.value.status) ||
          waitingStatuses.has(run.value.status))
      ) {
        stream?.close();
        stream = null;
      }
    });
  }
  async function selectSession(id: string): Promise<void> {
    clearSession();
    const version = sessionVersion,
      owner = project.value?.id;
    const current = await request<Session>(`/sessions/${id}`);
    if (version !== sessionVersion || owner !== project.value?.id) return;
    session.value = current;
    saveSelection();
    await refreshSession();
  }
  async function selectProject(
    id: string,
    preferredSession?: string,
  ): Promise<void> {
    const version = ++projectVersion;
    clearSession();
    sessions.value = [];
    project.value = projects.value.find((item) => item.id === id) || null;
    saveSelection();
    if (!project.value || project.value.deleted_at) return;
    const rows = await request<Session[]>(
      `/projects/${id}/sessions?summary=true`,
    );
    if (version !== projectVersion) return;
    sessions.value = rows;
    const initial =
      rows.find((item) => item.id === preferredSession && !item.deleted_at) ||
      rows.find((item) => !item.archived_at && !item.deleted_at);
    if (initial) await selectSession(initial.id);
  }
  async function createProject(
    name: string,
    environment?: EnvironmentSelection,
    workspacePath?: string,
  ): Promise<void> {
    const created = await request<Project>("/projects", "POST", {
      name,
      environment,
      workspace_path: workspacePath || undefined,
    });
    projects.value = await request<Project[]>("/projects");
    await selectProject(created.id);
    await createSession("新会话");
  }
  async function setProjectPath(workspacePath: string): Promise<void> {
    const id = project.value?.id;
    if (!id) return;
    const updated = await request<{ workspace_path: string }>(
      `/projects/${id}/path`,
      "PUT",
      { workspace_path: workspacePath },
    );
    projects.value = projects.value.map((item) =>
      item.id === id ? { ...item, ...updated } : item,
    );
    if (project.value?.id !== id) return;
    project.value = { ...project.value, ...updated };
    await refreshSession();
    revision.value++;
  }
  async function createSession(
    title: string,
    workspaceMode: WorkspaceMode = "worktree",
  ): Promise<void> {
    const id = project.value?.id;
    if (!id) return;
    const created = await request<Session>(`/projects/${id}/sessions`, "POST", {
      title: title.trim() || "新会话",
      workspace_mode: workspaceMode,
    });
    if (project.value?.id !== id) return;
    sessions.value = await request<Session[]>(
      `/projects/${id}/sessions?summary=true`,
    );
    await selectSession(created.id);
  }
  async function deleteSession(id: string): Promise<void> {
    try {
      await request(`/sessions/${id}`, "DELETE");
    } catch (cause) {
      if (project.value)
        sessions.value = await request<Session[]>(
          `/projects/${project.value.id}/sessions?summary=true`,
        );
      if (
        session.value?.id === id &&
        sessions.value.find((item) => item.id === id)?.deleted_at
      )
        clearSession();
      throw cause;
    }
    sessions.value = sessions.value.filter((item) => item.id !== id);
    if (session.value?.id === id || !session.value) {
      clearSession();
      const next = sessions.value.find(
        (item) => !item.archived_at && !item.deleted_at,
      );
      if (next) await selectSession(next.id);
      else saveSelection();
    }
  }
  async function deleteProject(id: string): Promise<void> {
    try {
      await request(`/projects/${id}`, "DELETE");
    } catch (cause) {
      projects.value = await request<Project[]>("/projects");
      if (project.value?.id === id) {
        project.value = projects.value.find((item) => item.id === id) || null;
        if (project.value?.deleted_at) {
          clearSession();
          sessions.value = [];
        }
      }
      throw cause;
    }
    projects.value = projects.value.filter((item) => item.id !== id);
    if (project.value?.id === id)
      await selectProject(projects.value[0]?.id || "");
  }
  async function sendMessage(
    task: string,
    mode: Run["mode"],
    permission_policy: PermissionPolicy,
  ): Promise<void> {
    const id = session.value?.id;
    if (!id) throw new Error("请选择会话后发送。");
    const selected = run.value?.id;
    let target = session.value?.active_run_id;
    if (target) {
      const current =
        run.value?.id === target
          ? run.value
          : await request<Run>(`/runs/${target}`);
      const question = current.interrupt as PlanningRequest | null;
      if (
        current.status === "awaiting_input" &&
        question?.kind === "plan_clarification" &&
        question.questions.length === 1
      ) {
        await request(`/runs/${target}/resume`, "POST", {
          answer: {
            request_id: question.request_id,
            answers: [
              {
                question_id: question.questions[0].id,
                option_id: "",
                text: task,
              },
            ],
          },
        });
      } else if (
        current.status === "paused" ||
        (current.status === "awaiting_input" &&
          question?.kind !== "plan_clarification")
      ) {
        await request(`/runs/${target}/resume`, "POST", { answer: task });
      } else {
        await request(`/runs/${target}/steer`, "POST", { text: task });
      }
    } else {
      const created = await request<Run>(`/sessions/${id}/runs`, "POST", {
        task,
        mode,
        permission_policy,
      });
      target = created.id;
    }
    if (session.value?.id !== id) return;
    try {
      await refreshSession(run.value?.id === selected ? target : undefined);
      revision.value++;
    } catch {
      // 写入已被服务端确认。稍后重新同步，不能让草稿再次发送。
      error.value = "消息已发送，正在重新同步执行状态。";
    }
  }
  async function retryRun(): Promise<void> {
    if (!run.value) return;
    const owner = session.value?.id;
    const created = await request<Run>(`/runs/${run.value.id}/retry`, "POST");
    if (owner !== session.value?.id) return;
    await refreshSession(created.id);
    revision.value++;
  }
  async function control(
    ...[action, body = {}]: RunControlArgs
  ): Promise<void> {
    const id = run.value?.id,
      owner = session.value?.id;
    if (!id) return;
    const result = await request<Run>(`/runs/${id}/${action}`, "POST", body);
    if (owner !== session.value?.id) return;
    await refreshSession(run.value?.id === id ? result?.id || id : undefined);
    revision.value++;
  }
  async function poll(): Promise<void> {
    if (polling || busy.value || !project.value) return;
    polling = true;
    const id = project.value.id,
      version = projectVersion;
    try {
      const rows = await request<Session[]>(
        `/projects/${id}/sessions?summary=true`,
      );
      if (version !== projectVersion) return;
      sessions.value = rows;
      const selected = rows.find((item) => item.id === session.value?.id);
      const sessionChanged =
        selected &&
        (selected.active_run_id !== session.value?.active_run_id ||
          (selected.revision != null &&
            selected.revision !== session.value?.revision));
      if (sessionChanged) {
        const previous = selectedRunId;
        await refreshSession();
        if (session.value?.id !== selected.id) return;
        revision.value++;
        if (previous !== selectedRunId) return;
      }
      if (
        run.value &&
        (sessionChanged || !terminalStatuses.has(run.value.status))
      )
        await refreshRun();
    } catch {
      /* 网络恢复后继续同步；操作失败由 act 显示。 */
    } finally {
      polling = false;
    }
  }
  async function initialize(): Promise<void> {
    projects.value = await request<Project[]>("/projects");
    let saved: { project?: string; session?: string } | null = null;
    try {
      saved = JSON.parse(
        localStorage.getItem("miniharness2.selection") || "null",
      );
    } catch {
      /* 忽略损坏的偏好。 */
    }
    const initial =
      projects.value.find((item) => item.id === saved?.project) ||
      projects.value[0];
    if (initial) await selectProject(initial.id, saved?.session);
    pollTimer = setInterval(() => void poll(), 4000);
  }
  onUnmounted(() => {
    detach();
    clearInterval(pollTimer);
    projectVersion++;
    sessionVersion++;
  });
  return {
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
    sendMessage,
    retryRun,
    control,
    refreshSession,
  };
}
