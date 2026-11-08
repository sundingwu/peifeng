/**
 * 能力与偏好面板：两个页签——「worker管理」与「个性化提示词」。
 *
 * 几条与后端一致的约定：
 * - 只列worker（planner / critic 是流程固定角色，不在页面上管）；
 * - 共享工具集中展示一次，每个专属工具展示用途简介；
 * - 每个worker卡片里直接管它自己的工具与技能：上传、删除，不用先选角色；
 * - 所有改动**重启后生效**（能力只在启动时装配一次）。
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { App as AntApp, Button, Drawer, Empty, Input, Modal, Popconfirm, Space, Tabs, Typography } from "antd";
import { useEffect, useState } from "react";

import { endpoints, get, type CapabilityRole, type CapabilityTool } from "../api/client";

const { Text, Paragraph } = Typography;

export function CapabilityDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [viewedTool, setViewedTool] = useState<CapabilityTool | null>(null);
  const { message: toast } = AntApp.useApp();

  const roles = useQuery<{ roles: CapabilityRole[] }>({
    queryKey: ["capabilities"],
    queryFn: () => get<{ roles: CapabilityRole[] }>(endpoints.capabilities),
    enabled: open,
  });
  const preferences = useQuery<{ system_append: string }>({
    queryKey: ["preferences"],
    queryFn: () => get<{ system_append: string }>(endpoints.preferences),
    enabled: open,
  });

  const call = useMutation({
    mutationFn: async ({ method, path, body }: { method: "POST" | "DELETE"; path: string; body?: unknown }) => {
      const response = await fetch(path, {
        method,
        headers: { "Content-Type": "application/json" },
        body: method === "DELETE" ? undefined : JSON.stringify(body ?? {}),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error ?? `请求失败（${response.status}）`);
      return payload;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["capabilities"] });
      void queryClient.invalidateQueries({ queryKey: ["preferences"] });
    },
    onError: (error: Error) => void toast.warning(error.message),
  });

  // 新建worker
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [prompt, setPrompt] = useState("");

  // 个性化
  const [append, setAppend] = useState<string | null>(null);
  const effectiveAppend = append ?? preferences.data?.system_append ?? "";
  useEffect(() => {
    if (!open) setAppend(null); // 关掉再打开时重新读服务端的值
  }, [open]);

  const workers = (roles.data?.roles ?? []).filter((role) => role.kind === "executor");
  const sharedTools = [...new Map(workers.flatMap((role) => role.tools.filter((tool) => tool.shared)).map((tool) => [tool.name, tool])).values()];

  /** 读本地文件成文本（工具/技能都是纯文本，不需要 multipart）。 */
  const readFile = async (file: File) => ({ name: file.name, content: await file.text() });

  const uploadTool = async (agent: string, file: File) => {
    const { name: filename, content } = await readFile(file);
    call.mutate(
      { method: "POST", path: endpoints.capabilityTool, body: { agent, filename, content } },
      { onSuccess: () => toast.success(`已上传工具 ${filename}，重启后生效`) },
    );
  };

  const uploadSkill = async (agent: string, file: File) => {
    const { content } = await readFile(file);
    // 技能目录名必须与 frontmatter 的 name 一致，这里直接从前缀里取
    const matched = /^---[\s\S]*?^name:\s*(\S+)/m.exec(content);
    if (!matched) {
      toast.warning("SKILL.md 开头需要 frontmatter（至少写 name: <技能名>）");
      return;
    }
    call.mutate(
      { method: "POST", path: endpoints.capabilitySkill, body: { agent, name: matched[1], content } },
      { onSuccess: () => toast.success(`已上传技能 ${matched[1]}，重启后生效`) },
    );
  };

  const executorTab = (
    <div className="cap-list">
      {!creating ? (
        <Button type="dashed" block onClick={() => setCreating(true)}>
          + 新建worker
        </Button>
      ) : (
        <div className="cap-create">
          <div className="preset-field">
            <label className="preset-label">worker名（英文小写，目录名即名字）</label>
            <Input value={name} onChange={(event) => setName(event.target.value)} placeholder="如 ocr_reader" />
          </div>
          <div className="preset-field">
            <label className="preset-label">职责描述（Planner 靠它决定派谁）</label>
            <Input.TextArea
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              autoSize={{ minRows: 2, maxRows: 4 }}
              placeholder="如：识别扫描件与图片里的文字，输出可检索的纯文本"
            />
          </div>
          <div className="preset-field">
            <label className="preset-label">提示词（留空用内置骨架，之后可改文件）</label>
            <Input.TextArea
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              autoSize={{ minRows: 2, maxRows: 6 }}
            />
          </div>
          <Space>
            <Button
              type="primary"
              disabled={!name.trim() || !description.trim()}
              onClick={() =>
                call.mutate(
                  { method: "POST", path: endpoints.capabilityExecutor, body: { name, description, prompt } },
                  {
                    onSuccess: () => {
                      setName("");
                      setDescription("");
                      setPrompt("");
                      setCreating(false);
                      toast.success("已创建，重启后生效");
                    },
                  },
                )
              }
            >
              创建
            </Button>
            <Button onClick={() => setCreating(false)}>取消</Button>
          </Space>
        </div>
      )}

      {roles.isLoading && <Text type="secondary">加载中…</Text>}
      {!roles.isLoading && workers.length === 0 && <Empty description="还没有worker" />}

      {sharedTools.length > 0 && <section className="cap-role cap-shared-tools">
        <div className="cap-role-head"><Text strong>共享工具</Text><Text type="secondary">所有worker可用</Text></div>
        {sharedTools.map((tool) => <div className="cap-item cap-tool" key={tool.name}><ToolName tool={tool} /><Button size="small" onClick={() => setViewedTool(tool)}>查看描述</Button></div>)}
      </section>}
      {workers.map((role) => {
        // 专属工具在所属worker中展示，共享工具在上方集中展示。
        const ownTools = role.tools.filter((tool) => !tool.shared);
        return (
          <div key={role.name} className="cap-role">
            <div className="cap-role-head">
              <Space size={6} align="center">
                <Text strong>{role.name}</Text>
                <Text type="secondary">
                  {ownTools.length} 专属工具 · {role.skills.length} 技能
                </Text>
              </Space>
              <Popconfirm
                title={`删除worker ${role.name}？`}
                description="会连同它的工具与技能目录一起删除，重启后生效。"
                okText="删除"
                okButtonProps={{ danger: true }}
                cancelText="取消"
                onConfirm={() => call.mutate({ method: "DELETE", path: `${endpoints.capabilityExecutor}/${role.name}` })}
              >
                <Button size="small" danger autoInsertSpace={false}>
                  删除
                </Button>
              </Popconfirm>
            </div>

            {role.description && <p className="cap-desc">{role.description}</p>}

            <div className="cap-section">
              <div className="cap-section-head">
                <Text type="secondary">工具</Text>
                <label className="cap-upload">
                  上传 .py
                  <input
                    type="file"
                    accept=".py"
                    onChange={(event) => {
                      const file = event.target.files?.[0];
                      if (file) void uploadTool(role.name, file);
                      event.target.value = ""; // 允许连续上传同一个文件名
                    }}
                  />
                </label>
              </div>
              {ownTools.length === 0 && <Text type="secondary">（没有专属工具，只用共享的 read / write / move / delete / bash）</Text>}
              {ownTools.map((tool) => (
                <div key={`t-${tool.name}`} className="cap-item cap-tool">
                  <ToolName tool={tool} />
                  <div className="cap-tool-actions"><Button size="small" onClick={() => setViewedTool(tool)}>查看描述</Button>
                  <Popconfirm
                    title={`删除工具 ${tool.name}？`}
                    okText="删除"
                    okButtonProps={{ danger: true }}
                    cancelText="取消"
                    onConfirm={() =>
                      call.mutate({ method: "DELETE", path: `${endpoints.capabilityTool}/${role.name}/${tool.name}` })
                    }
                  >
                    <Button size="small" type="link" danger className="cap-delete" autoInsertSpace={false}>
                      删除
                    </Button>
                  </Popconfirm></div>
                </div>
              ))}
            </div>

            <div className="cap-section">
              <div className="cap-section-head">
                <Text type="secondary">技能</Text>
                <label className="cap-upload">
                  上传 SKILL.md
                  <input
                    type="file"
                    accept=".md"
                    onChange={(event) => {
                      const file = event.target.files?.[0];
                      if (file) void uploadSkill(role.name, file);
                      event.target.value = "";
                    }}
                  />
                </label>
              </div>
              {role.skills.length === 0 && <Text type="secondary">（没有技能）</Text>}
              {role.skills.map((skill) => (
                <div key={`s-${skill.name}`} className="cap-item">
                  <Text>
                    <span className="cap-kind">技能</span>
                    {skill.name}
                  </Text>
                  <Popconfirm
                    title={`删除技能 ${skill.name}？`}
                    okText="删除"
                    okButtonProps={{ danger: true }}
                    cancelText="取消"
                    onConfirm={() =>
                      call.mutate({
                        method: "DELETE",
                        path: `${endpoints.capabilitySkill}/${role.name}/${skill.name}`,
                      })
                    }
                  >
                    <Button size="small" type="link" danger className="cap-delete" autoInsertSpace={false}>
                      删除
                    </Button>
                  </Popconfirm>
                </div>
              ))}
            </div>
          </div>
        );
      })}

      <Paragraph type="secondary" style={{ marginTop: 4 }}>
        共享工具（read / write / move / delete / bash）每个worker都有，不在这里展示也删不掉。所有改动**重启服务后生效**。
      </Paragraph>
    </div>
  );

  const preferenceTab = (
    <div>
      <Paragraph type="secondary">
        这段会追加进每个角色的 system prompt（<code>通用规则</code> 之后），用于输出风格与个人偏好；
        与角色职责冲突时以角色职责为准。改动在重启服务后生效。
      </Paragraph>
      <Input.TextArea
        value={effectiveAppend}
        onChange={(event) => setAppend(event.target.value)}
        autoSize={{ minRows: 10, maxRows: 20 }}
        placeholder={"如：\n- 结论先行，正文不超过三句话\n- 涉及数字时保留两位小数并标注单位\n- 不要使用「我们」这类主语"}
      />
      <div style={{ marginTop: 10 }}>
        <Button
          type="primary"
          onClick={() =>
            call.mutate(
              { method: "POST", path: endpoints.preferences, body: { system_append: effectiveAppend } },
              { onSuccess: () => toast.success("已保存，重启后生效") },
            )
          }
        >
          保存
        </Button>
      </div>
    </div>
  );

  return (
    <><Drawer title="能力与个性化" open={open} onClose={onClose} width={620} destroyOnHidden>
      <Tabs
        items={[
          { key: "workers", label: "worker管理", children: executorTab },
          { key: "prefs", label: "个性化提示词", children: preferenceTab },
        ]}
      />
    </Drawer>
    <Modal open={viewedTool !== null} title={viewedTool ? `${viewedTool.name} · 工具描述` : "工具描述"}
      onCancel={() => setViewedTool(null)} footer={<Button onClick={() => setViewedTool(null)}>关闭</Button>}>
      <p className="tool-description-content">{viewedTool?.description?.trim() || "该工具尚未提供描述。"}</p>
    </Modal></>
  );
}

function ToolName({ tool }: { tool: CapabilityTool }) {
  return <div className="cap-tool-info"><Text><span className="cap-kind">{tool.shared ? "共享" : "工具"}</span>{tool.name}</Text></div>;
}
