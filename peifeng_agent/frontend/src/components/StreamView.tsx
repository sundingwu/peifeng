/**
 * 对话流：以**轨迹**为主展示（时间线），中间过程收进默认折叠的「思考过程」。
 *
 * 轨迹（一眼可见）：你的需求（右侧气泡）→ 每一步的标题（序号 + worker + 目标）与工具调用
 * → 提示 → 终态结果块。
 * 思考过程（默认折叠）：模型的流式文本与每次工具调用的参数，展开才看。
 *
 * 事件到界面的映射（与内核发布的事件一一对应）：
 *   task_received               → 需求气泡（同时重置轨迹）
 *   plan_ready                  → 顶部计划面板（整份计划）
 *   step_start                  → 时间线上的一个步骤（含 worker）
 *   step_review                 → 计划面板里该步的状态（通过 / 返修）
 *   agent_text_delta            → 折叠区里的流式文本
 *   agent_tool_execution_start  → 轨迹里的一行摘要 + 折叠区里的参数
 *   agent_tool_execution_end    → 摘要行状态变为完成 / 出错
 *   task_result/failed/cancelled→ 终态结果块与提示条
 *
 * 阅读体验的三条约定：
 * - **自动跟随有条件**：只有视图贴着底部时才跟随新内容，往上翻历史不会被拉回；
 *   不在底部时右下角给「回到最新」。
 * - **展开状态由本组件统一持有**：每步的思考过程可独立展开，也可以一键全部展开 / 收起；
 *   状态不随事件重渲染丢失。
 * - **历史任务连续呈现**：按用户提问、助手回答与文件附件展示，不折叠成标题行。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { TaskItem } from "../api/client";
import type { TaskEntry } from "../taskEntries";
import { FileChips } from "./FileChips";
import { Markdown } from "./Markdown";
import { ArrowDownIcon, TaskIcon } from "./icons";

/** 对话流里的一项：由事件增量累积而成（`applyEvent` 在 App 里产出）。 */
export type StreamItem =
  | { id: string; type: "task"; requirement: string }
  | { id: string; type: "plan"; requirement: string; summary: string; steps: PlanStepLine[] }
  | { id: string; type: "step"; index: number; total: number; goal: string; worker: string; group?: number }
  | { id: string; type: "stepReview"; index: number; approved: boolean; summary: string }
  | { id: string; type: "text"; text: string; streaming: boolean }
  | { id: string; type: "tool"; name: string; detail: string; done: boolean; failed: boolean }
  | { id: string; type: "notice"; level: "info" | "warning" | "error"; text: string; terminal?: boolean }
  | { id: string; type: "result"; status: string; content: string; files: string[] };

/** 计划里的一步（`plan_ready` 事件带下来的全量步骤）。 */
export type PlanStepLine = { index: number; goal: string; worker: string; group?: number };

type StepLine = Extract<StreamItem, { type: "step" }>;
type TraceLine = Extract<StreamItem, { type: "text" | "tool" }>;

/** 渲染单元：一个步骤连同它期间的中间过程聚成一组。 */
type Group =
  | { kind: "task"; item: Extract<StreamItem, { type: "task" }> }
  | { kind: "step"; item: StepLine; trace: TraceLine[] }
  | { kind: "loose"; lines: TraceLine[] }
  | { kind: "notice"; item: Extract<StreamItem, { type: "notice" }> }
  | { kind: "result"; item: Extract<StreamItem, { type: "result" }> };

/** 终态 → 胶囊文案与色调（ok / warn / danger / 中性）。 */
const STATUS: Record<string, { text: string; tone: "ok" | "warn" | "danger" | "muted" }> = {
  completed: { text: "已完成", tone: "ok" },
  aborted: { text: "已中止", tone: "warn" },
  cancelled: { text: "已中止", tone: "warn" },
  rejected: { text: "计划被拒", tone: "muted" },
  failed: { text: "失败", tone: "danger" },
};

function statusOf(status: string) {
  return STATUS[status] ?? { text: status || "未知", tone: "muted" as const };
}

function chipClassOf(tone: "ok" | "warn" | "danger" | "muted") {
  return tone === "muted" ? "chip" : `chip ${tone}`;
}



/** 把扁平的增量事件按步骤归组：步骤期间的文本与工具调用收进该步骤的 trace。 */
function groupItems(items: StreamItem[]): Group[] {
  const groups: Group[] = [];
  for (const item of items) {
    // 计划与评审结论只喂计划面板（见 PlanPanel.selectPlan），不进对话流
    if (item.type === "plan" || item.type === "stepReview") continue;
    if (item.type === "step") continue;
    if (item.type === "text" || item.type === "tool") {
      const last = groups[groups.length - 1];
      if (last?.kind === "step") {
        last.trace.push(item);
      } else if (last?.kind === "loose") {
        last.lines.push(item);
      } else {
        groups.push({ kind: "loose", lines: [item] });
      }
      continue;
    }
    if (item.type === "task") {
      groups.push({ kind: "task", item });
      continue;
    }
    if (item.type === "notice") {
      groups.push({ kind: "notice", item });
      continue;
    }
    groups.push({ kind: "result", item });
  }
  return groups;
}

function groupKey(group: Group): string {
  return group.kind === "loose" ? (group.lines[0]?.id ?? "loose") : group.item.id;
}

export function StreamView({
  history,
  items,
  entries,
  onEntry,
  showHome = false,
  sessionKey = "",
}: {
  entries: TaskEntry[];
  onEntry: (entry: TaskEntry) => void;
  history: TaskItem[];
  items: StreamItem[];
  showHome?: boolean;
  /** 当前项目｜会话：变化说明切了会话，此时应停在顶部（先看历史）而不是底部 */
  sessionKey?: string;
}) {
  const boxRef = useRef<HTMLDivElement>(null);
  /** 视图是否贴着底部：只有贴着时才自动跟随新内容 */
  const pinnedRef = useRef(true);
  const [atBottom, setAtBottom] = useState(true);
  const [activeAnchor, setActiveAnchor] = useState("");
  /** 上一次渲染的会话标识：用来判断"切了会话" */
  const lastSession = useRef(sessionKey);
  /** 已展开的思考过程（key 与分组一致）：统一持有，事件重渲染不丢 */
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const groups = useMemo(() => groupItems(items), [items]);
  const traceGroups = useMemo(() => groups.filter((group) => group.kind !== "task"), [groups]);

  const onScroll = useCallback(() => {
    const box = boxRef.current;
    if (!box) return;
    const near = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    pinnedRef.current = near;
    setAtBottom(near);
    const anchors = Array.from(box.querySelectorAll<HTMLElement>("[data-conversation-anchor]"));
    const threshold = box.getBoundingClientRect().top + box.clientHeight * 0.4;
    const active = anchors.filter((element) => element.getBoundingClientRect().top <= threshold).at(-1) ?? anchors[0];
    if (active) setActiveAnchor(active.dataset.conversationAnchor ?? "");
  }, []);

  const toBottom = () => {
    const box = boxRef.current;
    if (box) box.scrollTop = box.scrollHeight;
    pinnedRef.current = true;
    setAtBottom(true);
  };

  // 用户发了新消息：无条件回到底部（不管之前翻到哪儿），否则新任务会开在看不见的地方
  const lastTaskId = useMemo(() => {
    const lastTask = [...items].reverse().find((item) => item.type === "task");
    return lastTask?.id ?? "";
  }, [items]);
  useEffect(() => {
    if (!lastTaskId) return;
    pinnedRef.current = true;
    setAtBottom(true);
    const box = boxRef.current;
    if (box) box.scrollTop = box.scrollHeight;
  }, [lastTaskId]);

  // 内容变化（含流式文本增长）时按需跟随；往上翻历史时 pinned=false，不会被拉回
  useEffect(() => {
    const box = boxRef.current;
    if (box && pinnedRef.current) box.scrollTop = box.scrollHeight;
  }, [items, history.length]);

  // 切换会话：停在**顶部**（先看到历史任务），而不是自动滚到底部把历史滚出视野
  useEffect(() => {
    if (lastSession.current === sessionKey) return;
    lastSession.current = sessionKey;
    const box = boxRef.current;
    if (box) box.scrollTop = 0;
    pinnedRef.current = false;
    setAtBottom(false);
  }, [sessionKey]);

  const expandableKeys: string[] = [];

  if (showHome || (history.length === 0 && items.length === 0)) {
    return (
      <div className="empty-state">
        <div className="welcome-heading"><h1 className="poem-title">故九万里，而风斯在下矣，<br />而后乃今培风。</h1></div>
        <div className="shortcut-grid" aria-label="快捷入口">{entries.map((entry) => <button key={entry.id} type="button" onClick={() => onEntry(entry)}><TaskIcon kind={entry.id.includes("chart") ? "chart" : entry.id.includes("file") ? "folder" : "document"} /><span className="shortcut-copy"><strong>{entry.name}</strong><small>{entry.description}</small></span></button>)}</div>
        <div className="workflow-note" aria-label="工作流程">
          <span><b>01</b> 理解与规划</span><i aria-hidden="true">→</i>
          <span><b>02</b> 协作执行</span><i aria-hidden="true">→</i>
          <span><b>03</b> 检查与交付</span>
        </div>
      </div>
    );
  }

  return (
    <div className="stream">
      <div className="stream-bar">
        <span>轨迹</span>
        <span className="tools">
          <button
            type="button"
            className="ghost-btn"
            disabled={expandableKeys.length === 0}
            onClick={() => setExpanded(new Set(expandableKeys))}
          >
            全部展开
          </button>
          <button
            type="button"
            className="ghost-btn"
            disabled={expanded.size === 0}
            onClick={() => setExpanded(new Set())}
          >
            全部收起
          </button>
        </span>
      </div>

      <div ref={boxRef} onScroll={onScroll} className="stream-scroll">
        {history.length > 0 && (
          <div className="stream-section">
            历史任务 · {history.length}
          </div>
        )}
        {history.map((task) => (
          <HistoryCard key={task.seq} task={task} />
        ))}

        {groups
          .filter((group) => group.kind === "task")
          .map((group) => (
            <div key={groupKey(group)} className="msg-user" data-conversation-anchor={group.item.id}>
              {group.kind === "task" ? group.item.requirement : null}
              {/* 计划面板：紧跟在这次用户消息下面，随对话一起滚动 */}
              {group.kind === "task" && group.item.id === lastTaskId && <TaskPanel items={items} />}
            </div>
          ))}

        {traceGroups.length > 0 && (
          <div className="timeline">
            {traceGroups.map((group) => (
              <GroupView key={groupKey(group)} group={group} />
            ))}
          </div>
        )}
      </div>

      <nav className="conversation-nav" aria-label="对话快捷导航">
        {[...history.map((task) => ({id: `history-${task.seq}`, text: task.original_request || task.requirement})),
          ...items.filter((item): item is Extract<StreamItem, {type: "task"}> => item.type === "task").map((item) => ({id: item.id, text: item.requirement}))].map((entry, index) => {
          const label = Array.from(entry.text).slice(0, 20).join("") || `对话 ${index + 1}`;
          return <button key={entry.id} type="button" className={activeAnchor === entry.id ? "active" : ""} aria-label={label}
            onClick={() => {
              const box = boxRef.current;
              const anchor = box ? Array.from(box.querySelectorAll<HTMLElement>("[data-conversation-anchor]")).find((element) => element.dataset.conversationAnchor === entry.id) : null;
              if (box && anchor) box.scrollTo({top: box.scrollTop + anchor.getBoundingClientRect().top - box.getBoundingClientRect().top - 16, behavior: "smooth"});
              pinnedRef.current = false;
              setActiveAnchor(entry.id);
            }}><i /><span>{label}</span></button>;
        })}
      </nav>
      {!atBottom && (
        <button type="button" className="to-bottom" onClick={toBottom}>
          <ArrowDownIcon />
          回到最新
        </button>
      )}
    </div>
  );
}

function GroupView({ group }: { group: Group }) {
  switch (group.kind) {
    case "loose":
      // 工具调用不再单独成栏：它们按步骤归到 TaskPanel 的展开里
      return null;
    case "notice":
      return (
        <div className="tl-item">
          <div className={`notice ${group.item.level}`}>{group.item.text}</div>
        </div>
      );
    case "result": {
      const status = statusOf(group.item.status);
      return (
        <div className={`tl-item ${status.tone === "ok" ? "done" : status.tone === "danger" ? "failed" : ""}`}>
          <div className={`result ${status.tone === "ok" ? "" : status.tone}`}>
            <div className="result-head">
              <span className="title">任务结束</span>
              <span className={chipClassOf(status.tone)}>{status.text}</span>
            </div>
            <div className="result-body">
              <Markdown text={group.item.content} />
            </div>
            <FileChips files={group.item.files} />
          </div>
        </div>
      );
    }
    default:
      return null;
  }
}

/** 一个步骤：**只有步骤头常显**；工具调用与模型输出一起收进可展开的「过程」面板。 */
function HistoryCard({ task }: { task: TaskItem }) {
  const status = statusOf(task.status);
  return (
    <article className="conversation-turn" data-conversation-anchor={`history-${task.seq}`}>
      <div className="conversation-question"><span>你</span><p>{task.original_request || task.requirement || `任务 ${task.seq}`}</p></div>
      <section className="conversation-answer">
        <div className="conversation-byline"><span className="assistant-mark">培</span><strong>培风</strong><span className={chipClassOf(status.tone)}>{status.text}</span></div>
        <div className="conversation-copy"><Markdown text={task.content || task.summary || "此任务暂无结果记录。"} /></div>
        <FileChips files={task.files ?? []} />
      </section>
    </article>
  );
}

export type StepState = "pending" | "running" | "review" | "done" | "failed";

/** 从事件流推出每一步的状态：评审不通过仍算这一步在跑（返修），不新开一栏。 */
function stepStates(items: StreamItem[]): Map<number, StepState> {
  const states = new Map<number, StepState>();
  for (const item of items) {
    if (item.type === "step") states.set(item.index, "running");
    if (item.type === "stepReview") states.set(item.index, item.approved ? "done" : "running");
    if (item.type === "result" || (item.type === "notice" && item.terminal)) {
      for (const [index, state] of states) {
        if (state !== "done") states.set(index, "failed");
      }
    }
  }
  return states;
}

/** 顶部那一行最多显示多少个字符（可按需调整；超出的截断，始终只占一行）。 */
const STATUS_LINE_CHARS = 100;

/**
 * 顶部一行：正在做什么。
 *
 * 取**整段回执的前 N 个字符**，不用"首行"——有些工具的第一行只有一个 `{` 或空行，
 * 按首行显示会看到一条几乎空白的横线。换行与连续空白先压成单个空格。
 */
function currentLine(items: StreamItem[]): string {
  const last = [...items].reverse().find((item) => item.type === "tool" || item.type === "text");
  if (!last) return "";
  const raw = last.type === "tool" ? last.detail || "" : last.text || "";
  const flat = raw.replace(/\s+/g, " ").trim();
  if (!flat) return last.type === "tool" ? last.name : "";
  const body = flat.length > STATUS_LINE_CHARS ? `${flat.slice(0, STATUS_LINE_CHARS)}…` : flat;
  return last.type === "tool" ? `${last.name} · ${body}` : body;
}

const PHASE: Record<string, { text: string; tone: string }> = {
  plan: { text: "计划中", tone: "plan" },
  running: { text: "执行中", tone: "running" },
  review: { text: "评审中", tone: "review" },
  done: { text: "已完成", tone: "done" },
  stopped: { text: "已中止", tone: "stopped" },
};

/** 当前阶段：计划中 / 执行中 / 评审中（+ 终态）。 */
function phaseOf(items: StreamItem[]): keyof typeof PHASE {
  let phase: keyof typeof PHASE = "plan";
  let sawStep = false;
  for (const item of items) {
    if (item.type === "plan") phase = "plan";
    if (item.type === "step") {
      phase = "running";
      sawStep = true;
    }
    if (item.type === "stepReview") phase = item.approved ? phase : "running";
    if (item.type === "result") phase = "done";
    if (item.type === "notice" && item.terminal) phase = "stopped";
  }
  // 刚起完最后一轮工具、还没收到评审结果时，显示「评审中」
  const lastMeaningful = [...items].reverse().find((item) => item.type !== "text" && item.type !== "tool");
  if (sawStep && lastMeaningful?.type === "step") {
    const lastTool = [...items].reverse().find((item) => item.type === "tool");
    if (lastTool && lastTool.done) phase = "review";
  }
  return phase;
}

/** 任务面板：外框里顶部一行是状态与当前工具/模型输出，下面是逐步的小框。 */
export function TaskPanel({ items }: { items: StreamItem[] }) {
  const plan = [...items].reverse().find((item) => item.type === "plan");
  if (!plan) return null;
  const phase = phaseOf(items);
  // 任务结束就把计划栏收起来：结果与汇总在对话里，留着它只占地方
  if (phase === "done" || phase === "stopped") return null;
  const info = PHASE[phase] ?? PHASE.plan;
  const line = currentLine(items);
  return (
    <div className="task-panel">
      <div className="task-panel-head">
        <span className={`status-dot ${info.tone}`} aria-hidden />
        <span className={`status-phase ${info.tone}`}>{info.text}</span>
        {line && (
          <span className="status-line" title={line}>
            {line}
          </span>
        )}
      </div>
      <StepCards items={items} />
    </div>
  );
}

/** 计划步骤：并排卡片、当前高亮、点开就地看这一段的工具调用与模型输出。 */
export function StepCards({ items }: { items: StreamItem[] }) {
  const plan = [...items].reverse().find((item) => item.type === "plan");
  const [open, setOpen] = useState<Set<number>>(new Set());
  const states = stepStates(items);
  // 工具调用按「步骤边界」归属：遇到 step 就切到那一步，后面的 tool/text 都算它的
  const traces = useMemo(() => {
    const map = new Map<number, TraceLine[]>();
    let current = -1;
    for (const item of items) {
      if (item.type === "step") {
        current = item.index;
        if (!map.has(current)) map.set(current, []);
        continue;
      }
      if (item.type === "tool" || item.type === "text") {
        const list = current >= 0 ? map.get(current) : undefined;
        if (list) list.push(item);
      }
    }
    return map;
  }, [items]);
  if (!plan) return null;
  const toggle = (index: number) => {
    const next = new Set(open);
    if (next.has(index)) next.delete(index);
    else next.add(index);
    setOpen(next);
  };
  return (
    <div className="step-cards">
      {plan.steps.map((step) => {
        const state = states.get(step.index) ?? "pending";
        const trace = traces.get(step.index) ?? [];
        const expanded = open.has(step.index);
        const flag = state === "done" ? "✓" : state === "failed" ? "!" : String(step.index + 1);
        return (
          <div key={step.index} className={`step-card ${state} ${expanded ? "open" : ""}`}>
            <button type="button" className="step-card-head" onClick={() => toggle(step.index)}
              title={`${step.goal}${step.worker ? ` · ${step.worker}` : ""}`}>
              {/* 正在执行的那一步左侧转圈；其余显示 ✓ / ! / 序号 */}
              {state === "running" ? <i className="step-spinner" aria-label="执行中" /> : <b>{flag}</b>}
              <span className="step-card-goal">{step.goal || "（未写目标）"}</span>
              {step.worker && <i className="step-card-worker">{step.worker}</i>}
            </button>
            {expanded && (
              <div className="step-card-body">
                {trace.length === 0 ? (
                  <p className="step-card-empty">这一步还没有工具调用。</p>
                ) : (
                  trace.map((line, order) =>
                    line.type === "tool" ? (
                      <p key={order} className={`step-card-line ${line.failed ? "failed" : ""}`}>
                        <span className="step-card-tool">{line.name}</span>
                        <span title={line.detail}>{(line.detail || "").split("\n")[0]}</span>
                      </p>
                    ) : (
                      <p key={order} className="step-card-text">{(line.text || "").split("\n")[0]}</p>
                    ),
                  )
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

