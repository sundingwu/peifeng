/**
 * 后端接口封装。
 *
 * 类型来自 `schema.d.ts`（由 `npm run gen:api` 从后端的 OpenAPI 生成）——
 * 后端改字段，这里编译期就会报错。
 */
import type { components, paths } from "./schema";

export type Snapshot = components["schemas"]["Snapshot"];
export type ProjectSummary = components["schemas"]["ProjectSummary"];
export type SessionSummary = components["schemas"]["SessionSummary"];
export type TaskItem = components["schemas"]["TaskItem"] & { original_request?: string };

/** 快捷模板的表单字段：`required` 的没填就提交不了，`type` 决定用文本框还是选择器。 */
export type PresetField = {
  key: string;
  label: string;
  type?: "text" | "file" | "dir";
  placeholder?: string;
  default?: string;
  required?: boolean;
};

/** 快捷模板：点一下、填几个字段就直接执行（Plan 是后端预写好的，不走 LLM 规划）。 */
export type Preset = {
  id: string;
  name: string;
  description: string;
  fields: PresetField[];
};

/** 一个工具：被哪些角色注册由 `roles` 给出（空数组表示存在但未注册，不生效）。 */
export type CapabilityTool = {
  name: string;
  description: string;
  path: string;
  exists: boolean;
  roles: string[];
};

/** 一个技能：`agents` 是被哪些角色注册（空数组表示存在但未注册）。 */
export type CapabilitySkill = {
  name: string;
  title?: string;
  description: string;
  path: string;
  exists: boolean;
  agents: string[];
};

/** 一个角色（Mentor 或 worker）。 */
export type CapabilityAgent = {
  name: string;
  kind: string;
  impl: string;
  description: string;
  access: string;
  prompt: string;
  prompt_path: string;
  tools: string[];
  skills: string[];
  errors: string[];
};

/** 能力面板三栏数据（工具 / 技能 / 角色）。 */
export type CapabilityIndex = {
  tools: CapabilityTool[];
  skills: CapabilitySkill[];
  agents: CapabilityAgent[];
};

/** 带路径参数的能力源码地址（GET 读、POST 存共用）。 */
/** 能力源码：kind=tool|skill|prompt，name 是工具名 / 技能名 / 角色名。 */
export function sourcePath(kind: "tool" | "skill" | "prompt", name: string): string {
  return `/api/capabilities/source?kind=${kind}&name=${encodeURIComponent(name)}`;
}

/** 全局删除（从所有角色移除）：工具删文件、技能删目录。 */
export function toolDropPath(name: string): string {
  return `/api/capabilities/tool/${encodeURIComponent(name)}`;
}

export function skillDropPath(name: string): string {
  return `/api/capabilities/skill/${encodeURIComponent(name)}`;
}

/** 保存角色能用哪些工具与技能。 */
export function agentConfigPath(name: string): string {
  return `/api/capabilities/agent/${encodeURIComponent(name)}/config`;
}

/** 内核事件：`seq` 单调递增，用于断线重连后的对账。 */
export type AgentEvent = { seq: number; kind: string; data: Record<string, unknown>; session?: string; project?: string };

/** 动作端点的提交函数：页面用 react-query 的 mutation 提供实现，失败时抛出 ApiError。 */
export type ActionFn = (input: { path: string; body?: unknown }) => Promise<unknown>;

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** 失败一律是 `{ok:false, error}` 或 `{detail}`（FastAPI 校验错误）。
 * 这里统一成 ApiError，调用方只需处理一种形状。
 */
async function toError(response: Response): Promise<ApiError> {
  let message = `请求失败（${response.status}）`;
  try {
    const payload = (await response.json()) as { error?: string; detail?: unknown };
    if (payload.error) message = payload.error;
    else if (typeof payload.detail === "string") message = payload.detail;
  } catch {
    // 非 JSON 响应：保留默认文案
  }
  return new ApiError(message, response.status);
}

export async function get<T>(path: string, params?: Record<string, string>): Promise<T> {
  const query = params ? `?${new URLSearchParams(params)}` : "";
  const response = await fetch(`${path}${query}`);
  if (!response.ok) throw await toError(response);
  return (await response.json()) as T;
}

/** 上传一个文件：请求体是原始字节、文件名走查询参数（导入项目文件用）。 */
export async function upload<T>(path: string, name: string, body: Blob, extra?: Record<string, string>): Promise<T> {
  const query = new URLSearchParams({ name, ...(extra ?? {}) }).toString();
  const response = await fetch(`${path}?${query}`, {
    method: "POST",
    headers: { "Content-Type": "application/octet-stream" },
    body,
  });
  if (!response.ok) throw await toError(response);
  return (await response.json()) as T;
}

export async function post<T>(path: string, body: unknown = {}): Promise<T> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw await toError(response);
  return (await response.json()) as T;
}

/** 产出文件下载地址（服务端只接受文件名，不接受路径）。 */
export function downloadUrl(name: string): string {
  return `/api/file?name=${encodeURIComponent(name)}`;
}

/** 后端端点路径（单独导出，便于在测试里断言不漂移）。 */
export const endpoints = {
  state: "/api/state",
  events: "/api/events",
  presets: "/api/presets",
  workspaceEntries: "/api/workspace/entries",
  capabilities: "/api/capabilities",
  capabilityWorker: "/api/capabilities/worker",
  capabilityTool: "/api/capabilities/tool",
  capabilitySkill: "/api/capabilities/skill",
  capabilitySource: "/api/capabilities/source",
  task: "/api/task",
  input: "/api/input",
  supplement: "/api/supplement",
  cancel: "/api/cancel",
  projectOpen: "/api/project/open",
  projectCreate: "/api/project/create",
  projectImport: "/api/project/import",
  projectRename: "/api/project/rename",
  projectRemove: "/api/project/remove",
  sessionOpen: "/api/session/open",
  sessionCreate: "/api/session/create",
  sessionRename: "/api/session/rename",
  sessionArchive: "/api/session/archive",
  sessionRestore: "/api/session/restore",
  sessionDelete: "/api/session/delete",
  file: "/api/file",
} as const satisfies Record<string, keyof paths>;

/** 历史任务项：`original_request` 是用户实际输入的那句话（优先用于展示）。 */
export type TaskItemView = TaskItem & { original_request?: string };

/** 离线执行一个预设：不调用任何模型，直接产出文件。 */
export async function runOffline(
  preset: string,
  values: Record<string, string>,
): Promise<{ files: string[]; summary: string; notes: string[] }> {
  const response = await fetch("/api/offline", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ preset, values }),
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `离线执行失败（HTTP ${response.status}）`);
  }
  return response.json();
}
