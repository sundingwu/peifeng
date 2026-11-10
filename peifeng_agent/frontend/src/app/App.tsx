/**
 * 应用外壳：状态快照（TanStack Query） + 内核事件（SSE 增量） + 动作端点。
 *
 * 职责划分：
 * - Query 管"全量快照"：首屏、断线对账、任务边界都重新拉一次；
 * - SSE 管增量：把事件累积成对话流（步骤、流式文本、工具调用、终态）。
 *
 * 这样即使事件丢了一段，界面也只会短暂落后，不会与真实状态永久不一致。
 *
 * 布局固定两栏：左侧栏（项目与会话管理，见 `../components/Sidebar`）常驻显示，不折叠、不抽屉，
 * 内容多了在栏内滚动；右侧是执行流 + 底部输入框。视觉全部走 `styles/app.css` 的类，
 * 这里只负责结构与状态。
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { App as AntApp, Button, Input, Modal } from "antd";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { endpoints, get, post, type AgentEvent, type Snapshot } from "../api/client";
import { AskModal } from "../components/AskModal";
import { CapabilityDrawer } from "../components/CapabilityDrawer";
import { SendIcon, StopIcon } from "../components/icons";
import { TaskEntryModal } from "../components/TaskEntryModal";
import { TASK_ENTRIES, buildTaskRequest, type TaskEntry } from "../taskEntries";
import { runOffline } from "../api/client";
import { Sidebar } from "../components/Sidebar";
import { StreamView, type StreamItem } from "../components/StreamView";
import { useEventStream } from "../hooks/useEventStream";

export function App() {
  const queryClient = useQueryClient();
  const { message: toast } = AntApp.useApp();
  const [items, setItems] = useState<StreamItem[]>([]);
  const [draft, setDraft] = useState("");
  const [showHome, setShowHome] = useState(true);
  const seq = useRef(0);

  const snapshot = useQuery<Snapshot>({
    queryKey: ["state"],
    queryFn: () => get<Snapshot>(endpoints.state),
  });

  const [showCapabilities, setShowCapabilities] = useState(false);
  const [openEntry, setOpenEntry] = useState<TaskEntry | null>(null);
  //: 离线执行：忙碌标记与结果（离线不占会话，结果就地弹出来）
  const [offlineBusy, setOfflineBusy] = useState(false);
  const [offlineReport, setOfflineReport] = useState<
    { name: string; files: string[]; summary: string; notes: string[]; error?: string } | null
  >(null);

  const refresh = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["state"] });
  }, [queryClient]);

  const data = snapshot.data;
  const running = data?.running ?? false;
  //: 除了"正在看的这个会话"，还有几个会话在后台跑（并发时才有值）
  const othersRunning = Math.max(
    0,
    (data?.running_sessions ?? []).filter((name) => name !== (data?.session ?? "")).length,
  );
  useEffect(() => {
    if (running) setShowHome(false);
  }, [running]);
  const pending = (data?.pending?.[0] as Record<string, unknown> | undefined) ?? null;

  // 计划面板与对话流同源：都从事件派生，刷新 / 切换会话后仍然一致

  const action = useMutation({
    mutationFn: ({ path, body }: { path: string; body?: unknown }) => post(path, body ?? {}),
    // 任何动作成功后都重新对账：pending（澄清弹窗）、历史、侧栏状态都来自快照
    onSuccess: () => refresh(),
    onError: (error: Error) => void toast.warning(error.message),
  });

  /** 提交任务后清空对话流，开始记录这一次的过程。 */
  const submit = (text: string) => {
    const trimmed = text.trim();
    if (!trimmed) return;
    setShowHome(false);
    if (running) {
      action.mutate({ path: endpoints.supplement, body: { text: trimmed } });
    } else {
      // 不清空轨迹：新任务的 task_received 到达时会整体重置（旧轨迹保留到那一刻，避免闪白）
      action.mutate({ path: endpoints.task, body: { text: trimmed } });
    }
    setDraft("");
  };

  const onEvent = useCallback(
    (event: AgentEvent) => {
      const payload = event.data ?? {};
      if (event.kind === "task_received") setShowHome(false);
      setItems((current) => applyEvent(current, event));
      // 任务进入终态、需要用户回答、或项目/会话被改动时，都要重新对账（历史、侧栏与 pending 都变了）
      if (
        [
          "agent_context_usage",
          "task_result",
          "task_failed",
          "task_cancelled",
          "task_idle",
          "await_input",
          "input_resolved",
          "session_opened",
          "session_renamed",
          "session_archived",
          "session_restored",
          "session_deleted",
          "project_opened",
          "project_changed",
        ].includes(event.kind)
      ) {
        refresh();
      }
      if (event.kind === "capability_error") {
        void toast.warning(String(payload.message ?? "能力加载失败"));
      }
    },
    [refresh, toast],
  );

  // 带上当前会话：并发时服务端只推这个会话的事件；切会话时依赖变化会自动重连
  const connectionStatus = useEventStream(data?.seq ?? 0, onEvent, refresh, data?.session ?? "");

  // 首次拿到快照时把 seq 对齐，避免首屏就误判跳号
  useEffect(() => {
    if (data?.seq) seq.current = data.seq;
  }, [data?.seq]);

  const projects = data?.projects ?? [];
  const history = useMemo(() => {
    // 轨迹区（内存事件流）只能回放"当前会话最近一个任务"，而且服务重启后是空的。
    // 所以只有在真的有轨迹可回放时，才把最后一个任务留给轨迹区；否则全部进历史卡片，
    // 免得"只有 1 个任务的会话"重启后两边都不显示。
    const tasks = data?.tasks ?? [];
    if (tasks.length === 0) return tasks;
    const trace = (data?.trace ?? []) as AgentEvent[];
    const received = trace.find((event) => event.kind === "task_received");
    const hasTerminal = trace.some((event) => ["task_result", "task_failed", "task_cancelled", "orchestrator_aborted"].includes(event.kind));
    const represented = hasTerminal && received?.data?.requirement === tasks[tasks.length - 1].requirement;
    return represented ? tasks.slice(0, -1) : tasks;
  }, [data?.tasks, data?.trace]);

  // 用快照里的轨迹重建对话流：刷新页面或切换会话后，仍能看到上一次任务的完整过程
  const sessionKey = `${data?.project ?? ""}｜${data?.session ?? ""}`;
  const restoredFor = useRef("");
  useEffect(() => {
    const trace = (data?.trace ?? []) as AgentEvent[];
    const switched = restoredFor.current !== sessionKey;
    if (trace.length === 0) {
      // 空会话（刚新建、或还没跑过任务）：切过来就必须清空轨迹，
      // 否则界面会停在上一个会话的内容上——这正是"新会话看着没切过去"的原因
      if (switched) setItems([]);
      restoredFor.current = sessionKey;
      return;
    }
    if (!switched && items.length > 0) return; // 同会话且有实时流：不覆盖
    restoredFor.current = sessionKey;
    setItems(trace.reduce<StreamItem[]>((acc, event) => applyEvent(acc, event), []));
  }, [data?.trace, sessionKey, items.length]);

  if (snapshot.isLoading) {
    return (
      <div className="boot">
        <span className="status-pill">
          <i className="dot busy" />
          正在连接本地服务…
        </span>
      </div>
    );
  }
  if (snapshot.isError) {
    return (
      <div className="boot">
        <div className="boot-card">
          <h3>无法连接本地服务</h3>
          <p>
            请确认服务已启动：<code>uv run peifeng</code>，然后刷新页面。
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="app">
      <header className="topbar poetic-header">
        <button type="button" className="brand" onClick={() => setShowHome(true)} aria-label="返回主界面"><img src="/site-mark.jpg" alt="" /><span>培风</span></button>
        <p className="header-verse">子非鱼，安知鱼之乐？</p>
        <div className="header-actions">
          <ContextMeter usage={data?.context_usage} />
          <span className="status-pill"><i className={`dot ${running || othersRunning > 0 ? "busy" : connectionStatus === "open" ? "ok" : ""}`} />{connectionStatus === "closed" ? "已断开" : connectionStatus === "connecting" ? "连接中" : running ? "执行中" : othersRunning > 0 ? `另有 ${othersRunning} 个会话在跑` : "就绪"}</span>
          <Button size="small" className="capability-trigger" onClick={() => setShowCapabilities(true)}>能力与个性化</Button>
        </div>
      </header>
      <div className="shell">
        {/* 左侧栏常驻：不折叠、不抽屉；栏内独立滚动，会话再多也不会把输入框顶出去 */}
        <aside className="sidebar" aria-label="项目与会话">
          <Sidebar
            projects={projects}
            defaultParent={data?.default_parent ?? ""}
            running={running}
            onAction={async (request) => {
              const result = await action.mutateAsync(request);
              if ([endpoints.sessionOpen, endpoints.sessionCreate, endpoints.projectOpen].some((path) => path === request.path)) setShowHome(false);
              return result;
            }}
          />
        </aside>

        <main className="main">
          <div className="main-inner">
            <span className="ui-build" title="前端构建版本（用于确认浏览器加载的是新版）">UI v2</span>
            <StreamView
              entries={TASK_ENTRIES}
              onEntry={setOpenEntry}
              history={history}
              items={items}
              showHome={showHome && !running}
              sessionKey={sessionKey}
            />
            <Composer
              running={running}
              draft={draft}
              onDraft={setDraft}
              onSubmit={() => submit(draft)}
            offlineBusy={offlineBusy}
              onCancel={() => action.mutate({ path: endpoints.cancel })}
            />
          </div>
        </main>
      </div>

      <TaskEntryModal entry={openEntry} onCancel={() => setOpenEntry(null)} onSubmit={async (entry, values) => {
        setOpenEntry(null);
        // 有 offline 的入口走离线：不调模型、不占会话，直接产出文件
        if (entry.offline) {
          setOfflineBusy(true);
          try {
            const result = await runOffline(entry.offline, values);
            setOfflineReport({ name: entry.name, ...result });
          } catch (error) {
            setOfflineReport({ name: entry.name, files: [], summary: "", notes: [], error: String(error) });
          } finally {
            setOfflineBusy(false);
          }
          return;
        }
        submit(buildTaskRequest(entry, values));
      }} />
      {/* 离线执行结果：不占会话，就地弹出来 */}
      <Modal
        open={offlineReport !== null}
        title={offlineReport?.name ? `${offlineReport.name}（离线执行）` : "离线执行"}
        onCancel={() => setOfflineReport(null)}
        footer={[<Button key="ok" type="primary" onClick={() => setOfflineReport(null)}>知道了</Button>]}
        destroyOnHidden
      >
        {offlineReport?.error ? (
          <p className="offline-error">失败：{offlineReport.error}</p>
        ) : (
          <>
            <p className="offline-summary">{offlineReport?.summary}</p>
            {offlineReport?.notes?.map((note, index) => (
              <p key={index} className="offline-note">注意：{note}</p>
            ))}
            {offlineReport?.files?.length ? (
              <ul className="offline-files">
                {offlineReport.files.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            ) : null}
          </>
        )}
      </Modal>

      <CapabilityDrawer open={showCapabilities} onClose={() => setShowCapabilities(false)} />
      <AskModal
        pending={pending}
        onAnswer={(id, value) => action.mutate({ path: endpoints.input, body: { id, value } })}
      />
    </div>
  );
}

/** 把一条事件并入对话流。事件 id 用 seq 拼，保证 React key 稳定。 */
function applyEvent(items: StreamItem[], event: AgentEvent): StreamItem[] {
  const data = event.data ?? {};
  const id = (suffix: string) => `${event.seq}-${suffix}`;

  switch (event.kind) {
    case "task_received":
      // 新任务开始：轨迹整体重置（上一个任务的轨迹在此刻被替换）
      return [
        {
          id: id("task"),
          type: "task",
          requirement: String(data.requirement ?? ""),
        },
      ];
    case "plan_ready": {
      const steps = Array.isArray(data.steps) ? (data.steps as Record<string, unknown>[]) : [];
      return [
        ...items,
        {
          id: id("plan"),
          type: "plan",
          requirement: String(data.requirement ?? ""),
          summary: String(data.summary ?? ""),
          steps: steps.map((step, index) => ({
            index: Number(step.index ?? index),
            goal: String(step.goal ?? ""),
            worker: String(step.worker ?? ""),
            // 并行组号：同组步骤会在计划面板里并排显示（后端 plan_ready 已带）
            group: Number(step.group ?? 0),
          })),
        },
      ];
    }
    case "step_review":
      return [
        ...items,
        {
          id: id("review"),
          type: "stepReview",
          index: Number(data.index ?? 0),
          approved: Boolean(data.approved),
          summary: String(data.summary ?? ""),
        },
      ];
    case "step_start":
      return [
        ...items,
        {
          id: id("step"),
          type: "step",
          index: Number(data.index ?? 0),
          total: Number(data.total ?? 0),
          goal: String(data.goal ?? ""),
          worker: String(data.worker ?? ""),
          // 并行组号：同组步骤同时执行，轨迹里用「∥ 并行组 N」标出来
          group: Number(data.group ?? 0),
        },
      ];
    case "context_compressed":
      return [
        ...items,
        {
          id: id("notice"),
          type: "notice",
          level: "info",
          text: `上下文已压缩（${Math.round(Number(data.before_chars ?? 0) / 1024)} KB → ${Math.round(
            Number(data.after_chars ?? 0) / 1024,
          )} KB）：更早的往返已省略，模型需要用工具重新读取。`,
        },
      ];
    case "agent_text_delta": {
      const delta = String(data.delta ?? "");
      if (!delta) return items;
      const last = items[items.length - 1];
      if (last?.type === "text" && last.streaming) {
        return [...items.slice(0, -1), { ...last, text: last.text + delta }];
      }
      return [...items, { id: id("text"), type: "text", text: delta, streaming: true }];
    }
    case "agent_tool_execution_start":
      return [
        // 上一条流式文本到此为止
        ...items.map((item) => (item.type === "text" ? { ...item, streaming: false } : item)),
        {
          id: id("tool"),
          type: "tool",
          name: String(data.name ?? data.tool ?? "工具"),
          detail: typeof data.arguments === "string" ? data.arguments : JSON.stringify(data.arguments ?? {}),
          done: false,
          failed: false,
        },
      ];
    case "agent_tool_execution_end": {
      for (let index = items.length - 1; index >= 0; index -= 1) {
        const item = items[index];
        if (item.type === "tool" && !item.done) {
          const next = [...items];
          next[index] = { ...item, done: true, failed: Boolean(data.is_error) };
          return next;
        }
      }
      return items;
    }
    case "task_result":
      return [
        ...items.map((item) => (item.type === "text" ? { ...item, streaming: false } : item)),
        {
          id: id("result"),
          type: "result",
          status: String(data.status ?? "completed"),
          content: String(data.content ?? ""),
          files: [],
        },
      ];
    case "task_failed":
      return [...items, { id: id("notice"), type: "notice", level: "error", terminal: true, text: String(data.message ?? "任务失败") }];
    case "task_cancelled":
      return [
        ...items,
        { id: id("notice"), type: "notice", level: "warning", terminal: true, text: String(data.message ?? "任务已中止") },
      ];
    case "input_timeout":
      return [...items, { id: id("notice"), type: "notice", level: "warning", text: "等待回答超时，已按未回答继续。" }];
    case "supplement_queued":
      return [...items, { id: id("notice"), type: "notice", level: "info", text: `正在按补充重新规划：${String(data.text ?? "")}` }];
    default:
      return items;
  }
}

/** 输入区：运行中提交的是"补充要求"，空闲时提交的是任务（计划直接执行，无需确认）。 */
function Composer({
  running,
  draft,
  onDraft,
  onSubmit,
  onCancel,
  offlineBusy = false,
}: {
  running: boolean;
  draft: string;
  onDraft: (text: string) => void;
  onSubmit: () => void;
  onCancel: () => void;
  /** 离线执行中：输入框提示一下，避免用户以为没反应 */
  offlineBusy?: boolean;
}) {
  return (
    <div className="composer">
      <div className="composer-box">
        <Input.TextArea
          className="composer-input"
          aria-label={running ? "补充当前任务" : "描述任务"}
          value={draft}
          onChange={(event) => onDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              onSubmit();
            }
          }}
          autoSize={{ minRows: 2, maxRows: 7 }}
          variant="borderless"
          placeholder={offlineBusy ? "离线执行中，请稍候…" : "不知周之梦为胡蝶与，胡蝶之梦为周与？"}
        />
        <div className="composer-bar">
          {running && (
            <Button size="small" danger icon={<StopIcon />} onClick={onCancel}>
              停止
            </Button>
          )}
          <Button type="primary" className="submit-task" icon={<SendIcon size={15} />} onClick={onSubmit} disabled={!draft.trim()}>
            {running ? "提交补充" : "开始任务"}
          </Button>
        </div>
      </div>
    </div>
  );
}

/** 把 token 数写成 K/M：1M 上限下用原始数字既长又难读。 */
function formatTokens(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0";
  if (value >= 1048576) {
    const millions = value / 1048576;
    return `${millions >= 100 ? Math.round(millions) : Number(millions.toFixed(1))}M`;
  }
  if (value >= 1000) {
    const thousands = value / 1000;
    return `${thousands >= 100 ? Math.round(thousands) : Number(thousands.toFixed(1))}K`;
  }
  return `${Math.round(value)}`;
}

function ContextMeter({ usage }: { usage?: Record<string, unknown> }) {
  const tokens = Number(usage?.estimated_tokens ?? 0);
  const limit = Number(usage?.limit ?? 0);
  const percent = limit ? tokens / limit * 100 : null;
  return <section className="context-meter minimal-meter" aria-label="上下文长度" title="上下文占用：优先采用端点返回的真实用量，端点未提供时按字符估算">
    <div className="context-numbers"><span>已用 {formatTokens(tokens)}</span><span>上限 {limit ? formatTokens(limit) : "—"}</span></div>
    <div className="context-track" role="progressbar" aria-label="上下文占用" aria-valuenow={Math.min(percent ?? 0, 100)} aria-valuemin={0} aria-valuemax={100}><i style={{width: `${Math.min(percent ?? 0, 100)}%`}} /></div>
  </section>;
}
