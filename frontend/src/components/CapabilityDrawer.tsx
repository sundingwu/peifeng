/**
 * 能力面板：三栏——工具 / 技能 / 角色。
 *
 * 分工（与后端一致）：
 * - **工具、技能**两栏是平铺清单，只展示名字与描述：查看、编辑、删除、上传。归属不在这里管。
 * - **角色**栏负责"这个角色能用哪些工具与技能"：勾选后保存即改写 `agent/agents/<角色>.yaml`，
 *   还能编辑提示词、新建与删除 worker。Mentor 固定在第一位、不可删除。
 * - 上传只落盘，不会自动注册到任何角色；要让它生效，去角色栏勾上。
 * - 所有改动**重启服务后生效**（能力只在启动时装配一次）。
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { App as AntApp, Button, Drawer, Empty, Input, Modal, Popconfirm, Select, Space, Tabs, Tag, Typography } from "antd";
import { useRef, useState } from "react";

import {
  agentConfigPath,
  endpoints,
  get,
  post,
  skillDropPath,
  toolDropPath,
  sourcePath,
  type CapabilityAgent,
  type CapabilityIndex,
} from "../api/client";

const { Text, Paragraph } = Typography;

/** 正在编辑的一份源码。 */
type Editor = { kind: "tool" | "skill" | "prompt"; name: string; title: string; path: string; url: string };
/** 角色能力草稿：勾选后点保存才写注册表。 */
type Draft = { tools: string[]; skills: string[] };

export function CapabilityDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const { message: toast } = AntApp.useApp();

  const index = useQuery<CapabilityIndex>({
    queryKey: ["capabilities"],
    queryFn: () => get<CapabilityIndex>(endpoints.capabilities),
    enabled: open,
  });

  const [editor, setEditor] = useState<Editor | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({ name: "", description: "", prompt: "" });
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const toolFile = useRef<HTMLInputElement>(null);
  const skillFile = useRef<HTMLInputElement>(null);

  const tools = index.data?.tools ?? [];
  const skills = index.data?.skills ?? [];
  const agents = index.data?.agents ?? [];
  // Mentor 固定在最前，其余按名字排
  const ordered = [...agents].sort((left, right) => {
    if (left.kind === "core" && right.kind !== "core") return -1;
    if (right.kind === "core" && left.kind !== "core") return 1;
    return left.name.localeCompare(right.name);
  });

  const refresh = () => queryClient.invalidateQueries({ queryKey: ["capabilities"] });
  const fail = (error: unknown) => toast.error(error instanceof Error ? error.message : String(error));

  const draftOf = (agent: CapabilityAgent): Draft => drafts[agent.name] ?? { tools: agent.tools, skills: agent.skills };
  const patchDraft = (agent: CapabilityAgent, patch: Partial<Draft>) =>
    setDrafts((previous) => ({ ...previous, [agent.name]: { ...draftOf(agent), ...patch } }));

  const openSource = async (next: Editor) => {
    setBusy(true);
    try {
      const data = await get<{ content: string; path: string }>(next.url);
      setEditor({ ...next, path: data.path });
      setDraft(data.content);
    } catch (error) {
      fail(error);
    } finally {
      setBusy(false);
    }
  };

  const saveSource = async () => {
    if (!editor) return;
    setBusy(true);
    try {
      await post(endpoints.capabilitySource, { kind: editor.kind, name: editor.name, content: draft });
      await refresh();
      setEditor(null);
      toast.success("已保存；重启服务后新内容才会生效");
    } catch (error) {
      fail(error);
    } finally {
      setBusy(false);
    }
  };

  /** 上传：只落盘，不注册；归属去角色栏勾选。 */
  const upload = async (kind: "tool" | "skill", file: File) => {
    const content = await file.text();
    try {
      if (kind === "tool") {
        await post(endpoints.capabilityTool, { agent: "", filename: file.name, content });
        toast.success(`已上传工具 ${file.name}；去「角色」栏勾选后才会生效`);
      } else {
        const matched = /^---[\s\S]*?^name:\s*(\S+)/m.exec(content);
        if (!matched) {
          toast.warning("SKILL.md 开头需要 frontmatter（至少写 name: <技能名>）");
          return;
        }
        await post(endpoints.capabilitySkill, { agent: "", name: matched[1], content });
        toast.success(`已上传技能 ${matched[1]}；去「角色」栏勾选后才会生效`);
      }
      await refresh();
    } catch (error) {
      fail(error);
    }
  };

  const drop = async (path: string, label: string) => {
    try {
      const response = await fetch(path, { method: "DELETE" });
      if (!response.ok) {
        const payload = (await response.json().catch(() => ({}))) as { error?: string };
        throw new Error(payload.error ?? `删除失败（HTTP ${response.status}）`);
      }
      await refresh();
      toast.success(`已删除 ${label}`);
    } catch (error) {
      fail(error);
    }
  };

  const saveConfig = async (agent: CapabilityAgent) => {
    const current = draftOf(agent);
    setBusy(true);
    try {
      await post(agentConfigPath(agent.name), { tools: current.tools, skills: current.skills });
      setDrafts((previous) => {
        const next = { ...previous };
        delete next[agent.name];
        return next;
      });
      await refresh();
      toast.success(`${agent.name} 的能力已保存；重启服务后生效`);
    } catch (error) {
      fail(error);
    } finally {
      setBusy(false);
    }
  };

  const removeAgent = async (agent: CapabilityAgent) => {
    try {
      const response = await fetch(`${endpoints.capabilityWorker}/${encodeURIComponent(agent.name)}`, { method: "DELETE" });
      if (!response.ok) throw new Error(`删除失败（HTTP ${response.status}）`);
      await refresh();
      toast.success(`已删除角色 ${agent.name}`);
    } catch (error) {
      fail(error);
    }
  };

  const createAgent = async () => {
    if (!form.name.trim() || !form.description.trim()) {
      toast.warning("角色名与职责描述都要填");
      return;
    }
    setBusy(true);
    try {
      await post(endpoints.capabilityWorker, {
        name: form.name.trim(),
        description: form.description.trim(),
        prompt: form.prompt,
      });
      await refresh();
      setCreating(false);
      setForm({ name: "", description: "", prompt: "" });
      toast.success("已新建 worker；重启后生效");
    } catch (error) {
      fail(error);
    } finally {
      setBusy(false);
    }
  };

  // ---------------------------------------------------------------- 工具栏
  const toolsTab = (
    <div className="cap-list">
      <Space style={{ marginBottom: 12 }}>
        <Button size="small" onClick={() => toolFile.current?.click()}>上传新工具</Button>
        <input
          ref={toolFile}
          type="file"
          accept=".py"
          hidden
          onChange={(event) => {
            const file = event.target.files?.[0];
            event.target.value = "";
            if (file) void upload("tool", file);
          }}
        />
      </Space>
      {tools.length === 0 && <Empty description="还没有工具" />}
      {tools.map((tool) => (
        <div className="cap-item" key={tool.name}>
          <div>
            <Text strong>{tool.name}</Text>
            <Paragraph type="secondary" className="cap-desc">{tool.description || "（无描述）"}</Paragraph>
          </div>
          <Space>
            <Button
              size="small"
              loading={busy}
              onClick={() => void openSource({
                kind: "tool",
                name: tool.name,
                title: `工具 ${tool.name}`,
                path: tool.path,
                url: sourcePath("tool", tool.name),
              })}
            >
              编辑
            </Button>
            <Popconfirm
              title={`删除工具 ${tool.name}？会从所有角色上移除并删除文件`}
              okText="删除"
              cancelText="取消"
              onConfirm={() => void drop(toolDropPath(tool.name), `工具 ${tool.name}`)}
            >
              <Button size="small" danger>删除</Button>
            </Popconfirm>
          </Space>
        </div>
      ))}
    </div>
  );

  // ---------------------------------------------------------------- 技能栏
  const skillsTab = (
    <div className="cap-list">
      <Space style={{ marginBottom: 12 }}>
        <Button size="small" onClick={() => skillFile.current?.click()}>上传新技能</Button>
        <input
          ref={skillFile}
          type="file"
          accept=".md"
          hidden
          onChange={(event) => {
            const file = event.target.files?.[0];
            event.target.value = "";
            if (file) void upload("skill", file);
          }}
        />
      </Space>
      {skills.length === 0 && <Empty description="还没有技能" />}
      {skills.map((skill) => (
        <div className="cap-item" key={skill.name}>
          <div>
            <Text strong>{skill.title || skill.name}</Text>
            {skill.title && <Text type="secondary"> · {skill.name}</Text>}
            <Paragraph type="secondary" className="cap-desc">{skill.description || "（无描述）"}</Paragraph>
          </div>
          <Space>
            <Button
              size="small"
              loading={busy}
              onClick={() => void openSource({
                kind: "skill",
                name: skill.name,
                title: `技能 ${skill.title || skill.name}`,
                path: skill.path,
                url: sourcePath("skill", skill.name),
              })}
            >
              编辑
            </Button>
            <Popconfirm
              title={`删除技能 ${skill.title || skill.name}？会从所有角色上移除并删除目录`}
              okText="删除"
              cancelText="取消"
              onConfirm={() => void drop(skillDropPath(skill.name), `技能 ${skill.name}`)}
            >
              <Button size="small" danger>删除</Button>
            </Popconfirm>
          </Space>
        </div>
      ))}
    </div>
  );

  // ---------------------------------------------------------------- 角色栏
  const toolOptions = tools.map((tool) => ({ value: tool.name, label: tool.name }));
  const skillOptions = skills.map((skill) => ({ value: skill.name, label: skill.title ? `${skill.title} (${skill.name})` : skill.name }));

  const agentsTab = (
    <div className="cap-list">
      <Space style={{ marginBottom: 12 }}>
        <Button size="small" onClick={() => setCreating(true)}>新建 worker</Button>
      </Space>
      {ordered.length === 0 && <Empty description="还没有角色" />}
      {ordered.map((agent) => {
        const core = agent.kind === "core";
        const current = draftOf(agent);
        const dirty = drafts[agent.name] !== undefined;
        return (
          <div
            className="cap-role"
            key={agent.name}
            style={core ? { borderColor: "#d4a017", background: "#fffdf5", boxShadow: "0 0 0 1px #f0d9a0 inset" } : undefined}
          >
            <div className="cap-role-head">
              <Text strong style={core ? { fontSize: 16 } : undefined}>{agent.name}</Text>
              {core && <Tag color="gold">核心角色 · 不可删除</Tag>}
              <Tag>{agent.kind}</Tag>
              <Tag>{agent.access}</Tag>
              <Space>
                <Button
                  size="small"
                  loading={busy}
                  onClick={() => void openSource({
                    kind: "prompt",
                    name: agent.name,
                    title: `提示词 ${agent.name}`,
                    path: agent.prompt_path,
                    url: sourcePath("prompt", agent.name),
                  })}
                >
                  编辑提示词
                </Button>
                {!core && (
                  <Popconfirm
                    title={`删除角色 ${agent.name}？注册表与提示词都会被删`}
                    okText="删除"
                    cancelText="取消"
                    onConfirm={() => void removeAgent(agent)}
                  >
                    <Button size="small" danger>删除</Button>
                  </Popconfirm>
                )}
              </Space>
            </div>
            <Paragraph type="secondary" className="cap-desc">{agent.description || "（无描述）"}</Paragraph>

            <div className="cap-section">
              <div className="cap-section-head"><Text type="secondary">工具（{current.tools.length}）</Text></div>
              <Select
                mode="multiple"
                allowClear
                style={{ width: "100%" }}
                placeholder="选择这个角色能用的工具"
                value={current.tools}
                options={toolOptions}
                onChange={(value: string[]) => patchDraft(agent, { tools: value })}
              />
            </div>
            <div className="cap-section">
              <div className="cap-section-head"><Text type="secondary">技能（{current.skills.length}）</Text></div>
              <Select
                mode="multiple"
                allowClear
                style={{ width: "100%" }}
                placeholder="选择这个角色能用的技能"
                value={current.skills}
                options={skillOptions}
                onChange={(value: string[]) => patchDraft(agent, { skills: value })}
              />
            </div>
            <Space style={{ marginTop: 10 }}>
              <Button size="small" type="primary" loading={busy} disabled={!dirty} onClick={() => void saveConfig(agent)}>
                保存能力
              </Button>
              {dirty && (
                <Button
                  size="small"
                  onClick={() =>
                    setDrafts((previous) => {
                      const next = { ...previous };
                      delete next[agent.name];
                      return next;
                    })
                  }
                >
                  撤销
                </Button>
              )}
            </Space>
          </div>
        );
      })}
    </div>
  );

  return (
    <>
      <Drawer title="能力与个性化" open={open} onClose={onClose} width={820} destroyOnHidden>
        <Tabs
          items={[
            { key: "tools", label: "工具", children: toolsTab },
            { key: "skills", label: "技能", children: skillsTab },
            { key: "agents", label: "角色", children: agentsTab },
          ]}
        />
      </Drawer>

      <Modal
        open={editor !== null}
        title={editor?.title ?? ""}
        width={860}
        onCancel={() => setEditor(null)}
        onOk={() => void saveSource()}
        okText="保存"
        cancelText="取消"
        confirmLoading={busy}
        okButtonProps={{ disabled: !draft.trim() }}
      >
        <Paragraph type="secondary" className="cap-desc">{editor?.path}</Paragraph>
        <Input.TextArea
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          autoSize={{ minRows: 14, maxRows: 28 }}
          style={{ fontFamily: "Consolas, monospace", fontSize: 12 }}
        />
      </Modal>

      <Modal
        open={creating}
        title="新建 worker"
        onCancel={() => setCreating(false)}
        onOk={() => void createAgent()}
        okText="创建"
        cancelText="取消"
        confirmLoading={busy}
      >
        <Space direction="vertical" style={{ width: "100%" }}>
          <Input
            placeholder="角色名（小写英文，如 analyst）"
            value={form.name}
            onChange={(event) => setForm({ ...form, name: event.target.value })}
          />
          <Input
            placeholder="职责描述（Mentor 靠它决定派什么活）"
            value={form.description}
            onChange={(event) => setForm({ ...form, description: event.target.value })}
          />
          <Input.TextArea
            placeholder="提示词（可留空，会自动生成骨架）"
            value={form.prompt}
            autoSize={{ minRows: 6, maxRows: 14 }}
            onChange={(event) => setForm({ ...form, prompt: event.target.value })}
          />
        </Space>
      </Modal>
    </>
  );
}
