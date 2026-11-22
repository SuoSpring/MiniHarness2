<script setup lang="ts">
import { ref, watch } from "vue";
import { request } from "../api";
import { display, type MCPServer, type Skill } from "../types";
const props = defineProps<{ projectId: string }>();
const servers = ref<MCPServer[]>([]),
  skills = ref<Skill[]>([]),
  busy = ref(false),
  error = ref(""),
  testResult = ref("");
const editingId = ref(""),
  name = ref(""),
  transport = ref<"stdio" | "http">("stdio"),
  command = ref(""),
  args = ref(""),
  url = ref(""),
  env = ref("{}"),
  headers = ref("{}");
async function load(): Promise<void> {
  error.value = "";
  try {
    [servers.value, skills.value] = await Promise.all([
      request<MCPServer[]>(`/projects/${props.projectId}/mcp`),
      request<Skill[]>(`/projects/${props.projectId}/skills`),
    ]);
  } catch (cause) {
    error.value = (cause as Error).message;
  }
}
async function action(operation: () => Promise<void>): Promise<void> {
  busy.value = true;
  error.value = "";
  try {
    await operation();
    await load();
  } catch (cause) {
    error.value = (cause as Error).message;
  } finally {
    busy.value = false;
  }
}
function reset(): void {
  editingId.value = "";
  name.value = "";
  transport.value = "stdio";
  command.value = "";
  args.value = "";
  url.value = "";
  env.value = "{}";
  headers.value = "{}";
}
function edit(server: MCPServer): void {
  editingId.value = server.id;
  name.value = server.name;
  transport.value = server.transport;
  command.value = server.command || "";
  args.value = (server.args || []).join("\n");
  url.value = server.url || "";
  env.value = display(server.env || {});
  headers.value = display(server.headers || {});
}
function save(): void {
  void action(async () => {
    const data = {
      name: name.value.trim(),
      transport: transport.value,
      command: transport.value === "stdio" ? command.value.trim() : null,
      args: args.value.split("\n").filter(Boolean),
      url: transport.value === "http" ? url.value.trim() : null,
      env: JSON.parse(env.value),
      headers: JSON.parse(headers.value),
      enabled: editingId.value
        ? servers.value.find((item) => item.id === editingId.value)!.enabled
        : true,
    };
    await request(
      editingId.value
        ? `/mcp/${editingId.value}`
        : `/projects/${props.projectId}/mcp`,
      editingId.value ? "PUT" : "POST",
      data,
    );
    reset();
  });
}
function toggleServer(server: MCPServer): void {
  void action(async () => {
    await request(`/mcp/${server.id}`, "PUT", {
      name: server.name,
      transport: server.transport,
      command: server.command,
      args: server.args,
      url: server.url,
      env: server.env,
      headers: server.headers,
      enabled: !server.enabled,
    });
  });
}
function removeServer(server: MCPServer): void {
  void action(async () => {
    await request(`/mcp/${server.id}`, "DELETE");
    if (editingId.value === server.id) reset();
  });
}
function test(server: MCPServer): void {
  void action(async () => {
    const result = await request<{ tools: unknown[] }>(
      `/mcp/${server.id}/test`,
      "POST",
      {},
    );
    testResult.value = `${server.name}：连接成功，${result.tools.length} 个工具\n${display(result.tools)}`;
  });
}
function toggleSkill(skill: Skill): void {
  void action(async () => {
    await request(
      `/projects/${props.projectId}/skills/${encodeURIComponent(skill.name)}`,
      "PUT",
      { enabled: !skill.enabled },
    );
  });
}
watch(
  () => props.projectId,
  () => {
    reset();
    testResult.value = "";
    void load();
  },
  { immediate: true },
);
</script>
<template>
  <section class="card">
    <div class="section-heading">
      <h2>工具与技能</h2>
      <button :disabled="busy" @click="load">重新加载</button>
    </div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <h3>模型上下文协议（MCP）服务器</h3>
    <p class="muted small">连接外部服务器后，智能体可调用其中提供的工具。</p>
    <article v-for="server in servers" :key="server.id" class="integration-row">
      <div>
        <strong>{{ server.name }}</strong>
        <p class="small muted">
          {{ server.transport === "stdio" ? server.command : server.url }}
        </p>
        <span class="badge"
          >{{ server.enabled ? "已启用" : "已停用" }} ·
          {{ server.transport === "stdio" ? "本地进程" : "远程 HTTP" }}</span
        >
      </div>
      <div class="actions">
        <button :disabled="busy" @click="test(server)">测试连接</button
        ><button :disabled="busy" @click="edit(server)">编辑</button
        ><button :disabled="busy" @click="toggleServer(server)">
          {{ server.enabled ? "停用" : "启用" }}</button
        ><button
          class="danger subtle"
          :disabled="busy"
          @click="removeServer(server)"
        >
          删除
        </button>
      </div>
    </article>
    <details v-if="testResult" open>
      <summary>连接测试结果</summary>
      <pre class="output">{{ testResult }}</pre>
    </details>
    <form class="stack-form" @submit.prevent="save">
      <h3>{{ editingId ? "编辑连接" : "新增连接" }}</h3>
      <div class="form-columns">
        <label
          >服务器名称<input
            v-model="name"
            required
            pattern="[a-zA-Z0-9_-]+"
            maxlength="60"
            placeholder="英文标识，例如 local-tools"
            title="仅使用英文字母、数字、下划线或连字符" /></label
        ><label
          >连接方式<select v-model="transport">
            <option value="stdio">本地标准输入输出（stdio）</option>
            <option value="http">远程流式 HTTP</option>
          </select></label
        >
      </div>
      <template v-if="transport === 'stdio'"
        ><label
          >启动命令<input
            v-model="command"
            placeholder="例如：npx"
            required /></label
        ><label
          >参数（每行一项）<textarea
            v-model="args"
            rows="3"
            placeholder="-y&#10;服务器包名"
          ></textarea></label></template
      ><label v-else
        >服务器地址<input
          v-model="url"
          type="url"
          placeholder="https://example.com/mcp"
          required
      /></label>
      <details>
        <summary>环境变量与请求头</summary>
        <div class="form-columns">
          <label
            >环境变量（JSON 对象）<textarea
              v-model="env"
              rows="3"
              spellcheck="false"
            ></textarea></label
          ><label
            >请求头（JSON 对象）<textarea
              v-model="headers"
              rows="3"
              spellcheck="false"
            ></textarea>
          </label>
        </div>
      </details>
      <div class="actions">
        <button class="primary" :disabled="busy">保存连接</button
        ><button v-if="editingId" type="button" @click="reset">取消编辑</button>
      </div>
    </form>
    <div class="section-divider"></div>
    <h3>技能（Skill）</h3>
    <p class="muted small">
      从配置的技能目录读取
      SKILL.md。启用后，智能体会按任务需要加载技能说明与资源。
    </p>
    <p v-if="!skills.length" class="empty">
      尚未发现技能。将技能放入后端配置的技能目录，再点击重新加载。
    </p>
    <article v-for="skill in skills" :key="skill.name" class="integration-row">
      <div>
        <strong>{{ skill.name }}</strong>
        <p>{{ skill.description }}</p>
        <p class="small muted">{{ skill.path }}</p>
      </div>
      <button :disabled="busy" @click="toggleSkill(skill)">
        {{ skill.enabled ? "停用技能" : "启用技能" }}
      </button>
    </article>
  </section>
</template>
