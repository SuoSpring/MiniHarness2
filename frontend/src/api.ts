import type { Run, RunEvent } from "./types";
export const apiBase = (
  import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8001/api/v1"
).replace(/\/$/, "");
export function apiUrl(path: string): string {
  return `${apiBase}${path}`;
}
export async function request<T>(
  path: string,
  method: string = "GET",
  body?: unknown,
): Promise<T> {
  const response = await fetch(apiUrl(path), {
    method,
    headers:
      body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const raw = await response.text();
    let detail = "";
    try {
      const data = JSON.parse(raw);
      detail =
        typeof data.detail === "string"
          ? data.detail
          : Array.isArray(data.detail)
            ? data.detail.map((item: { msg: string }) => item.msg).join("；")
            : "";
    } catch {
      /* 网关可能返回 HTML，不把整页错误暴露到工作台。 */
    }
    throw new Error(detail || `请求失败（${response.status}），请稍后重试。`);
  }
  return response.status === 204
    ? (undefined as T)
    : (response.json() as Promise<T>);
}
// 离开面板只断开订阅，后台执行继续；持久事件编号可用于重新订阅。
export function subscribeRun(
  runId: Run["id"],
  after: string,
  receive: (event: RunEvent) => void,
): EventSource {
  const source = new EventSource(
    apiUrl(
      `/runs/${runId}/events?live=true&after=${encodeURIComponent(after)}`,
    ),
  );
  for (const type of [
    "message",
    "state",
    "usage",
    "tool",
    "tool_result",
    "tool_reused",
    "plan",
    "plan_adjusted",
    "review",
    "verification_started",
    "verification",
    "test_design",
    "test_compiled",
    "result",
    "finished",
    "interrupted",
    "permission_decision",
    "log_resolution",
    "error",
  ]) {
    source.addEventListener(type, ((event: Event) => {
      if (!(event instanceof MessageEvent)) return;
      const data = JSON.parse(event.data) as Record<string, unknown>;
      receive({
        id: event.lastEventId,
        type:
          event.type === "message" ? String(data.type || "state") : event.type,
        data,
      });
    }) as EventListener);
  }
  return source;
}
