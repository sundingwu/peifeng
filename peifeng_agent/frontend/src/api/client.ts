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
export type DirListing = components["schemas"]["DirListing"];

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

/** 一个角色下的工具（`shared` 表示来自worker共享目录，不能从角色上删）。 */
export type CapabilityTool = {
  name: string;
  description: string;
  path: string;
  shared: boolean;
};

/** 一个角色下的技能。 */
export type CapabilitySkill = {
  name: string;
  description: string;
  path: string;
};

/** 角色（核心角色或worker）及其工具与技能。 */
export type CapabilityRole = {
  name: string;
  kind: "core" | "executor";
  /** worker的职责描述（来自 spec.py），页面上直接显示 */
  description: string;
  path: string;
  exists: boolean;
  deletable: boolean;
  tools: CapabilityTool[];
  skills: CapabilitySkill[];
};

/** 内核事件：`seq` 单调递增，用于断线重连后的对账。 */
export type AgentEvent = { seq: number; kind: string; data: Record<string, unknown> };

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
  capabilityExecutor: "/api/capabilities/executor",
  capabilityTool: "/api/capabilities/tool",
  capabilitySkill: "/api/capabilities/skill",
  preferences: "/api/preferences",
  task: "/api/task",
  input: "/api/input",
  supplement: "/api/supplement",
  cancel: "/api/cancel",
  projectOpen: "/api/project/open",
  projectCreate: "/api/project/create",
  projectOpenFolder: "/api/project/open-folder",
  projectRename: "/api/project/rename",
  projectRemove: "/api/project/remove",
  sessionOpen: "/api/session/open",
  sessionCreate: "/api/session/create",
  sessionRename: "/api/session/rename",
  sessionArchive: "/api/session/archive",
  sessionRestore: "/api/session/restore",
  sessionDelete: "/api/session/delete",
  fsDirs: "/api/fs/dirs",
  file: "/api/file",
} as const satisfies Record<string, keyof paths>;

/** 历史任务项：`original_request` 是用户实际输入的那句话（优先用于展示）。 */
export type TaskItemView = TaskItem & { original_request?: string };
