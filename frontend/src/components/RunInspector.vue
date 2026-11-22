<script setup lang="ts">
import {
  computed,
  defineAsyncComponent,
  nextTick,
  onUnmounted,
  ref,
  watch,
} from "vue";
import { apiUrl, request } from "../api";
import AcceptancePanel from "./AcceptancePanel.vue";
import RunConversation from "./RunConversation.vue";
import ReadableValue from "./ReadableValue.vue";
const MarkdownText = defineAsyncComponent(() => import("./MarkdownText.vue"));
import Pagination from "./Pagination.vue";
import {
  display,
  displaySummary,
  statusLabels,
  type Message,
  type Run,
  type RunEvent,
  type Session,
  type VerificationResult,
  type PageResult,
} from "../types";
type HistoryRequestKind = "page" | "refresh";
const props = defineProps<{
  run: Run | null;
  runs: Run[];
  events: RunEvent[];
  session: Session;
  busy: boolean;
  revision: number;
}>();
const emit = defineEmits<{ select: [id: string] }>();
const tab = ref("result");
const wholeSession = ref(false),
  rawTrace = ref(false),
  followLatest = ref(true);
const selectedIndex = computed(() =>
  props.runs.findIndex((item) => item.id === props.run?.id),
);
const visibleEventLabels = computed(() =>
  Object.fromEntries(
    Object.entries(eventLabels).filter(
      ([key]) => rawTrace.value || key !== "tool_result",
    ),
  ),
);
const trace = ref<RunEvent[]>([]),
  messages = ref<Message[]>([]);
const tracePage = ref(1),
  traceTotal = ref(0),
  historyPage = ref(1),
  historyTotal = ref(0);
const traceKind = ref(""),
  traceThrough = ref(""),
  newEvents = ref(false);
const traceBusy = ref(false),
  historyRequest = ref<HistoryRequestKind>(),
  traceError = ref(""),
  historyError = ref("");
const logPath = ref(""),
  logText = ref(""),
  logError = ref(""),
  logBusy = ref(false),
  logLoaded = ref(false);
const logPage = ref(0),
  logStarts = ref([0]),
  logNext = ref(0),
  logTotal = ref(0);
const catalog = ref<{ path: string; size: number }[]>([]);
let traceVersion = 0,
  historyVersion = 0,
  logVersion = 0,
  catalogVersion = 0;
let updateTimer: ReturnType<typeof setTimeout> | undefined;
const phase = computed(() => String(props.run?.state?.next_node || ""));
const progressPause = computed(() => {
  const value = props.run?.interrupt as {
    kind?: string;
    reason?: string;
    feedback?: string;
  } | null;
  return props.run?.status === "paused" &&
    value?.kind &&
    ["no_progress", "model_format", "test_design"].includes(value.kind)
    ? value
    : null;
});
const validation = computed(
  () =>
    props.run?.state?.model_retry as
      | { role?: string; attempts?: number; error?: string }
      | undefined,
);
const feedback = computed(
  () =>
    validation.value?.error ||
    (phase.value === "planner" && props.run?.state?.feedback
      ? String(props.run.state.feedback)
      : ""),
);
const verification = computed(
  () =>
    (props.run?.result as { verification?: VerificationResult } | null)
      ?.verification ??
    (props.run?.state?.verification as VerificationResult | undefined),
);
const conversation = ref<InstanceType<typeof RunConversation> | null>(null);
function navigate(event: Event): void {
  const select = event.target as HTMLSelectElement;
  if (tab.value === "result") {
    conversation.value?.jumpTo(select.value);
    select.value = "";
  } else {
    emit("select", select.value);
  }
}
async function showLatest(): Promise<void> {
  tab.value = "result";
  await nextTick();
  conversation.value?.scrollToLatest();
}
defineExpose({ showLatest });
const roleLabels: Record<string, string> = {
  user: "你",
  assistant: "智能体",
  tool: "工具结果",
  system: "系统记录",
  intent: "理解任务",
  planner: "规划",
  executor: "执行",
  reflection: "验收",
  test_designer: "测试设计",
  memory_consolidation: "记忆整理",
  summary: "摘要",
};
const eventLabels: Record<string, string> = {
  model_validation: "输出校验",
  permission_decision: "操作审批",
  plan_adjusted: "计划已调整",
  log_resolution: "执行错误已解决",
  verification_started: "开始验收",
  verification: "验收结果",
  no_progress: "无进展暂停",
  acceptance_contract_repaired: "修复验收协议",
  test_design: "设计测试",
  test_design_validation: "测试契约纠错",
  test_compiled: "编写测试",
  test_diagnosis: "核查失败原因",
  test_correction: "修正测试",
  state: "状态变化",
  tool: "工具调用",
  tool_result: "工具结果",
  tool_reused: "复用已有读取",
  usage: "预算用量",
  plan: "规划",
  review: "验收",
  result: "执行结果",
  finished: "执行结束",
  interrupted: "等待输入",
  error: "错误反馈",
  message: "消息",
};
const phaseLabels: Record<string, string> = {
  intent: "理解任务",
  planner: "规划",
  executor: "执行",
  tools: "调用工具",
  reflection: "验收",
  control: "检查任务进度",
  input: "读取新消息",
  user_input: "读取新消息",
  approval: "等待计划审批",
  apply_plan: "应用计划",
  finish: "收尾",
  end: "结束",
  test_designer: "设计测试",
  test_compiler: "生成测试",
  verification: "运行检查",
};
function eventDescription(event: RunEvent): string {
  if (event.type === "permission_decision") {
    const labels: Record<string, string> = {
      approve: "单次批准",
      approve_similar: "已记住同类操作",
      auto_approved: "已按本次任务的授权自动批准",
      reject: "已拒绝",
    };
    return [event.data.tool, labels[String(event.data.decision)]]
      .filter(Boolean)
      .join(" · ");
  }
  if (["model_validation", "test_design_validation"].includes(event.type))
    return String(event.data.error || "");
  if (event.type === "state") {
    const status = String(event.data.status || "");
    const next = String(event.data.next_node || event.data.phase || "");
    return [
      statusLabels[status] || status || "状态更新",
      next ? "下一步：" + (phaseLabels[next] || next) : "",
    ]
      .filter(Boolean)
      .join(" · ");
  }
  return display(
    event.data.name ||
      event.data.tool ||
      event.data.node ||
      statusLabels[String(event.data.status)] ||
      event.data.status,
  );
}
function eventTitle(event: RunEvent): string {
  if (event.type === "interrupted" && event.data.status === "paused")
    return "任务暂停";
  return eventLabels[event.type] || event.type;
}
function toolStatus(event: RunEvent): string {
  const result = event.data.result as
    | {
        ok?: boolean;
        exit_code?: number;
        timed_out?: boolean;
        result_unknown?: boolean;
        status?: string;
      }
    | undefined;
  if (result?.result_unknown) return "结果未知";
  return result?.ok === false ||
    ["failed", "error", "cancelled"].includes(result?.status || "") ||
    result?.timed_out ||
    (result?.exit_code != null && result.exit_code !== 0)
    ? "失败"
    : "完成";
}
function pageTrace(page: number): void {
  followLatest.value = false;
  void loadTrace(page);
}
const logPaths = computed(() => {
  const found = new Set(catalog.value.map((item) => item.path));
  function visit(value: unknown): void {
    if (!value || typeof value !== "object") return;
    for (const [key, child] of Object.entries(value)) {
      if (
        typeof child === "string" &&
        ["stdout_log", "stderr_log", "log_path"].includes(key)
      )
        found.add(child);
      else if (typeof child === "object") visit(child);
    }
  }
  props.events.forEach((event) => visit(event.data));
  visit(props.run?.result);
  return [...found];
});
function messageContent(message: Message): unknown {
  const envelope = message.content as {
    text?: unknown;
    data?: { content?: unknown };
    content?: unknown;
  } | null;
  let body =
    envelope?.text ??
    envelope?.data?.content ??
    envelope?.content ??
    message.content;
  if (
    Array.isArray(body) &&
    body.every(
      (part) =>
        typeof part === "string" ||
        (part?.type === "text" && typeof part.text === "string"),
    )
  )
    body = body
      .map((part) => (typeof part === "string" ? part : part.text))
      .join("\n");
  if (body === "" && envelope?.data) return envelope.data;
  if (
    typeof body === "string" &&
    message.role !== "user" &&
    /^\s*[[{]/.test(body)
  ) {
    try {
      return JSON.parse(body);
    } catch {
      /* 普通正文可用括号开头。 */
    }
  }
  return message.role === "assistant" && typeof body === "string"
    ? displaySummary(body)
    : body;
}
const historyItems = computed(() =>
  messages.value.map((message) => ({
    ...message,
    body: messageContent(message),
  })),
);
function formatTime(value?: string): string {
  return value
    ? new Date(value).toLocaleString("zh-CN", { hour12: false })
    : "";
}
async function loadTrace(page = 1, fresh = false): Promise<void> {
  if (!props.run) return;
  const version = ++traceVersion,
    id = props.run.id;
  traceBusy.value = true;
  traceError.value = "";
  const through = fresh ? "" : traceThrough.value;
  try {
    const data = await request<PageResult<RunEvent>>(
      `/runs/${id}/timeline?page=${page}&page_size=25&view=${rawTrace.value ? "raw" : "activity"}&kind=${encodeURIComponent(traceKind.value)}${through ? "&through=" + through : ""}`,
    );
    if (version !== traceVersion || props.run?.id !== id) return;
    trace.value = data.items;
    traceTotal.value = data.total;
    tracePage.value = page;
    traceThrough.value = data.through || "";
    newEvents.value = false;
  } catch (cause) {
    if (version === traceVersion) traceError.value = (cause as Error).message;
  } finally {
    if (version === traceVersion) traceBusy.value = false;
  }
}
async function loadHistory(
  page = 1,
  kind: HistoryRequestKind = "page",
): Promise<void> {
  if (!wholeSession.value && !props.run && props.runs.length) return;
  const version = ++historyVersion,
    id = props.session.id;
  historyRequest.value = kind;
  historyError.value = "";
  try {
    const data = await request<PageResult<Message>>(
      `/sessions/${id}/messages/page?page=${page}&page_size=25${!wholeSession.value && props.run ? "&run_id=" + props.run.id : ""}`,
    );
    if (version !== historyVersion || props.session.id !== id) return;
    const lastPage = Math.max(1, Math.ceil(data.total / data.page_size));
    if (page > lastPage) return await loadHistory(lastPage, kind);
    messages.value = data.items;
    historyTotal.value = data.total;
    historyPage.value = page;
  } catch (cause) {
    if (version === historyVersion)
      historyError.value = (cause as Error).message;
  } finally {
    if (version === historyVersion) historyRequest.value = undefined;
  }
}
function refreshHistory(): void {
  if (!historyRequest.value) void loadHistory(historyPage.value, "refresh");
}
async function loadCatalog(): Promise<void> {
  if (!props.run) return;
  const id = props.run.id,
    version = ++catalogVersion;
  try {
    const values = await request<{ path: string; size: number }[]>(
      `/runs/${id}/logs/index`,
    );
    if (version === catalogVersion && props.run?.id === id)
      catalog.value = values;
  } catch (cause) {
    if (version === catalogVersion) logError.value = (cause as Error).message;
  }
}
async function readLog(page = 0): Promise<void> {
  if (!props.run || !logPath.value.trim()) return;
  const id = props.run.id,
    path = logPath.value.trim(),
    version = ++logVersion;
  const offset =
    page === logPage.value + 1 ? logNext.value : logStarts.value[page] || 0;
  logBusy.value = true;
  logError.value = "";
  try {
    const data = await request<{
      content: string;
      next_offset: number;
      total_bytes: number;
    }>(`/runs/${id}/logs?path=${encodeURIComponent(path)}&offset=${offset}`);
    if (
      version !== logVersion ||
      props.run?.id !== id ||
      logPath.value.trim() !== path
    )
      return;
    logText.value = data.content;
    logPage.value = page;
    logStarts.value[page] = offset;
    logNext.value = data.next_offset;
    logTotal.value = data.total_bytes ?? data.next_offset;
    logLoaded.value = true;
  } catch (cause) {
    if (version === logVersion) logError.value = (cause as Error).message;
  } finally {
    if (version === logVersion) logBusy.value = false;
  }
}
function resetLog(): void {
  logVersion++;
  logText.value = "";
  logError.value = "";
  logPage.value = 0;
  logStarts.value = [0];
  logNext.value = 0;
  logTotal.value = 0;
  logLoaded.value = false;
  logBusy.value = false;
}
function refreshLogs(): void {
  logError.value = "";
  void loadCatalog();
  if (logPath.value) void readLog(logPage.value);
}
watch(logPath, resetLog, { flush: "sync" });
watch(
  () => props.runs[0]?.id,
  (latest) => {
    if (!latest) tab.value = "result";
    else if (wholeSession.value && tab.value === "history") refreshHistory();
  },
);
watch(
  () => props.run?.id,
  () => {
    traceVersion++;
    historyVersion++;
    logVersion++;
    catalogVersion++;
    trace.value = [];
    tracePage.value = 1;
    traceTotal.value = 0;
    traceThrough.value = "";
    traceError.value = "";
    traceBusy.value = false;
    historyRequest.value = undefined;
    messages.value = [];
    historyPage.value = 1;
    historyTotal.value = 0;
    historyError.value = "";
    followLatest.value = true;
    newEvents.value = false;
    catalog.value = [];
    logPath.value = "";
    resetLog();
    if (tab.value === "trace") void loadTrace(1, true);
    if (tab.value === "logs") void loadCatalog();
    if (tab.value === "history") void loadHistory(1);
  },
);
watch(traceKind, () => void loadTrace(1, true));
watch(rawTrace, () => {
  if (!rawTrace.value && traceKind.value === "tool_result")
    traceKind.value = "";
  void loadTrace(1, true);
});
watch(wholeSession, () => {
  messages.value = [];
  void loadHistory(1);
});
watch(tab, (value) => {
  const latest = props.session.active_run_id;
  if (value === "result" && latest && latest !== props.run?.id)
    emit("select", latest);
  if (value === "trace") void loadTrace(tracePage.value);
  if (value === "history") refreshHistory();
  if (value === "logs") void loadCatalog();
});
watch([() => props.events.at(-1)?.id, () => props.revision], () => {
  newEvents.value = true;
  if (updateTimer) return;
  updateTimer = setTimeout(() => {
    updateTimer = undefined;
    if (
      tab.value === "trace" &&
      followLatest.value &&
      tracePage.value === 1 &&
      !traceBusy.value
    )
      void loadTrace(1, true);
    if (tab.value === "history") refreshHistory();
    if (tab.value === "logs") void loadCatalog();
  }, 500);
});
onUnmounted(() => {
  traceVersion++;
  historyVersion++;
  logVersion++;
  catalogVersion++;
  clearTimeout(updateTimer);
});
</script>
<template>
  <section
    class="card inspector"
    :class="{ 'is-empty': !runs.length, 'is-conversation': tab === 'result' }"
  >
    <div v-if="runs.length" class="inspector-toolbar">
      <div class="tabs" role="tablist" aria-label="执行详情">
        <button
          v-for="item in [
            { id: 'result', label: '对话' },
            { id: 'trace', label: '执行轨迹' },
            { id: 'history', label: '完整对话' },
            { id: 'context', label: '上下文' },
            { id: 'logs', label: '运行日志' },
          ]"
          :key="item.id"
          role="tab"
          :aria-controls="'detail-' + item.id"
          :aria-selected="tab === item.id"
          @click="tab = item.id"
        >
          {{ item.label }}
        </button>
      </div>

      <select
        v-if="runs.length > 1"
        class="turn-jump"
        :aria-label="tab === 'result' ? '跳转到轮次' : '选择执行记录'"
        :disabled="tab !== 'result' && busy"
        :value="tab === 'result' ? '' : run?.id || ''"
        @change="navigate"
      >
        <option value="" disabled>
          {{ tab === "result" ? "跳转" : "选择执行记录" }}
        </option>
        <option v-for="(item, index) in runs" :key="item.id" :value="item.id">
          第 {{ runs.length - index }} 轮 · {{ item.task.slice(0, 40) }} ·
          {{ statusLabels[item.status] || item.status }}
          {{ formatTime(item.created_at) }}
        </option>
      </select>
    </div>
    <div
      v-if="tab !== 'result' && run && runs.length > 1"
      class="run-navigation"
      aria-label="执行记录切换"
    >
      <span class="small muted"
        >正在查看第 {{ runs.length - selectedIndex }} 次执行
        <span class="badge" :data-status="run.status">{{
          statusLabels[run.status] || run.status
        }}</span></span
      >
      <div class="actions">
        <button
          :disabled="
            busy || selectedIndex < 0 || selectedIndex >= runs.length - 1
          "
          @click="emit('select', runs[selectedIndex + 1].id)"
        >
          上一次执行</button
        ><button
          :disabled="busy || selectedIndex <= 0"
          @click="emit('select', runs[selectedIndex - 1].id)"
        >
          下一次执行
        </button>
      </div>
    </div>

    <slot v-if="tab !== 'result' && run" :item="run" />
    <RunConversation
      v-if="tab === 'result'"
      ref="conversation"
      id="detail-result"
      role="tabpanel"
      aria-label="对话"
      :run="run"
      :runs="runs"
      :session="session"
    >
      <template #activity>
        <div
          v-if="run && !run.result"
          class="run-phase"
          :data-status="run.status"
          role="status"
        >
          <span class="phase-indicator" aria-hidden="true"></span>
          <strong>{{
            phaseLabels[phase] || statusLabels[run.status] || "等待执行"
          }}</strong>
          <span class="muted small">{{
            statusLabels[run.status] || run.status
          }}</span>
        </div>
        <div v-if="feedback" class="validation-feedback" role="status">
          <strong
            >{{
              validation?.role && validation.role !== "planner"
                ? "输出需要修正"
                : "计划需要修正"
            }}<span v-if="validation?.attempts">
              · 第 {{ validation.attempts }} 次校验</span
            ></strong
          >
          <p>{{ feedback }}</p>
        </div>
        <div v-if="progressPause" class="validation-feedback" role="status">
          <strong>{{
            progressPause.kind === "model_format"
              ? "模型报告待修正，继续后重试当前节点"
              : progressPause.kind === "test_design"
                ? "测试契约待修正，继续后重试测试设计"
                : "检测到重复空转，任务已暂停"
          }}</strong>
          <p>{{ progressPause.reason }}</p>
          <details v-if="progressPause.feedback">
            <summary>查看待处理的修复反馈</summary>
            <p>{{ progressPause.feedback }}</p>
          </details>
        </div>
      </template>
      <template #default="{ item }"><slot :item="item" /></template>
    </RunConversation>
    <div
      v-else-if="tab === 'trace'"
      id="detail-trace"
      role="tabpanel"
      aria-label="执行轨迹"
      class="tab-content"
    >
      <div class="section-heading">
        <label class="filter-field"
          >事件类型<select v-model="traceKind" aria-label="事件类型">
            <option value="">全部事件</option>
            <option
              v-for="(label, value) in visibleEventLabels"
              :key="value"
              :value="value"
            >
              {{ label }}
            </option>
          </select></label
        >
        <button :disabled="traceBusy || !run" @click="loadTrace(1, true)">
          {{ newEvents ? "查看最新记录" : "刷新轨迹" }}
        </button>
      </div>
      <div class="trace-options">
        <label class="toggle small"
          ><input v-model="rawTrace" type="checkbox" />显示原始事件</label
        ><label class="toggle small"
          ><input v-model="followLatest" type="checkbox" />实时更新第一页</label
        ><span class="muted small">按时间从新到旧排列</span>
      </div>
      <p v-if="traceError" class="error" role="alert">{{ traceError }}</p>
      <p v-if="!trace.length && !traceError" class="empty">
        {{ traceBusy ? "正在加载…" : "暂无执行事件。" }}
      </p>
      <div class="trace-list" :aria-busy="traceBusy">
        <details
          v-for="(event, index) in trace"
          :key="event.id"
          :open="event.type === 'error' || event.type === 'model_validation'"
        >
          <summary>
            <span class="trace-index">{{
              (tracePage - 1) * 25 + index + 1
            }}</span>
            <strong>{{ eventTitle(event) }}</strong>
            <span class="trace-description muted">{{
              eventDescription(event)
            }}</span>
            <span
              v-if="event.type === 'tool'"
              class="badge"
              :data-status="
                toolStatus(event) === '失败' ? 'failed' : 'completed'
              "
              >{{ toolStatus(event) }}</span
            >
            <time class="muted small">{{ formatTime(event.created_at) }}</time>
          </summary>
          <div v-if="event.type === 'tool' && !rawTrace">
            <p v-if="event.data.role" class="small muted">
              事件 #{{ event.id }} ·
              {{ roleLabels[String(event.data.role)] || event.data.role }} ·
              文件修订 {{ event.data.file_revision }}
            </p>
            <p v-if="event.data.operation_id" class="small muted">
              调用编号：{{ event.data.operation_id }}
            </p>
            <h4>调用参数</h4>
            <pre class="output">{{ display(event.data.arguments || {}) }}</pre>
            <h4>返回结果</h4>
            <pre class="output">{{ display(event.data.result) }}</pre>
          </div>
          <div v-else-if="event.type === 'state' && !rawTrace">
            <p class="small">
              当时状态：{{
                statusLabels[String(event.data.status)] ||
                event.data.status ||
                "状态更新"
              }}
            </p>
            <p v-if="event.data.step_index != null" class="small">
              已完成 {{ event.data.step_index }} 步
            </p>
            <details>
              <summary>查看原始状态数据</summary>
              <pre class="output">{{ display(event.data) }}</pre>
            </details>
          </div>
          <pre v-else class="output">{{ display(event.data) }}</pre>
        </details>
      </div>
      <Pagination
        :page="tracePage"
        :total="traceTotal"
        :page-size="25"
        :busy="traceBusy"
        label="轨迹分页"
        @change="pageTrace($event)"
      />
    </div>
    <div
      v-else-if="tab === 'history'"
      id="detail-history"
      role="tabpanel"
      aria-label="完整对话"
      class="tab-content history-list"
    >
      <div class="section-heading">
        <label class="toggle small"
          ><input v-model="wholeSession" type="checkbox" />查看整个会话</label
        ><span class="small muted">{{
          wholeSession || !run ? "当前会话的全部消息" : "本次执行的消息"
        }}</span
        ><button :disabled="!!historyRequest" @click="refreshHistory">
          刷新对话
        </button>
      </div>
      <p v-if="historyError" class="error" role="alert">{{ historyError }}</p>
      <p v-if="!messages.length && !historyError" class="empty">
        {{ historyRequest ? "正在加载…" : "会话中尚无消息。" }}
      </p>
      <article
        v-for="(message, index) in historyItems"
        :key="message.id || index"
      >
        <header>
          <strong>{{ roleLabels[message.role] || message.role }}</strong
          ><time class="muted small">{{ formatTime(message.created_at) }}</time>
        </header>
        <MarkdownText
          v-if="typeof message.body === 'string' && message.role !== 'tool'"
          :text="message.body"
        />
        <ReadableValue v-else :value="message.body" />
      </article>
      <Pagination
        :page="historyPage"
        :total="historyTotal"
        :page-size="25"
        :busy="historyRequest === 'page'"
        label="对话分页"
        @change="loadHistory($event)"
      />
    </div>
    <div
      v-else-if="tab === 'context'"
      id="detail-context"
      role="tabpanel"
      aria-label="上下文"
      class="tab-content"
    >
      <h3>本次执行的固定要求</h3>
      <pre class="output">{{
        display(run ? run.state?.pinned : session.pinned) || "暂无固定要求。"
      }}</pre>
      <h3>本次执行的摘要</h3>
      <pre class="output">{{
        run
          ? display(run.state?.summary) || "本次执行没有摘要。"
          : session.summary || "暂无摘要。"
      }}</pre>
      <details v-if="run?.state">
        <summary>所选执行记录的状态</summary>
        <pre class="output">{{ display(run.state) }}</pre>
        <AcceptancePanel
          v-if="!verification?.id && !verification?.error"
          :key="run.id"
          :run-id="run.id"
          :session-id="session.id"
        />
      </details>
    </div>
    <div
      v-else
      id="detail-logs"
      role="tabpanel"
      aria-label="运行日志"
      class="tab-content"
    >
      <div class="log-selector">
        <label
          >选择日志
          <select
            v-model="logPath"
            aria-label="选择日志"
            :disabled="!logPaths.length"
            @change="readLog(0)"
          >
            <option value="" disabled>
              {{ logPaths.length ? "选择要查看的日志" : "暂无日志" }}
            </option>
            <option v-for="path in logPaths" :key="path" :value="path">
              {{ path }}
            </option>
          </select>
        </label>
        <button type="button" :disabled="!run || logBusy" @click="refreshLogs">
          {{ logBusy ? "读取中…" : "刷新日志" }}
        </button>
      </div>
      <p v-if="logError" class="error" role="alert">{{ logError }}</p>
      <pre class="output log-output">{{
        logLoaded ? logText || "这份日志是空的。" : "选择日志后查看内容。"
      }}</pre>
      <nav v-if="logLoaded" class="pagination" aria-label="日志分页">
        <span class="muted small"
          >第 {{ logPage + 1 }} 页 ·
          {{ logStarts[logPage].toLocaleString() }}–{{
            logNext.toLocaleString()
          }}
          / {{ logTotal.toLocaleString() }} 字节</span
        >
        <div class="actions">
          <button :disabled="logBusy || logPage === 0" @click="readLog(0)">
            首页
          </button>
          <button
            :disabled="logBusy || logPage === 0"
            @click="readLog(logPage - 1)"
          >
            上一页
          </button>
          <button
            :disabled="
              logBusy || logNext >= logTotal || logNext <= logStarts[logPage]
            "
            @click="readLog(logPage + 1)"
          >
            下一页
          </button>
        </div>
      </nav>
      <div class="actions">
        <a
          v-if="run && logPath"
          class="text-link"
          :href="
            apiUrl(
              `/runs/${run.id}/logs/download?path=${encodeURIComponent(logPath)}`,
            )
          "
          download
          >下载完整日志</a
        >
        <a
          v-if="run?.patch_available"
          class="text-link"
          :href="apiUrl(`/runs/${run.id}/patch`)"
          download
          >下载本次差异补丁</a
        >
      </div>
    </div>
  </section>
</template>
