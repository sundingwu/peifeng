/**
 * 左侧栏：项目 → 会话树，以及项目与会话的全部管理动作。
 *
 * 交互约定（与后端一一对应）：
 * - 写操作走弹层或二次确认，不用 `window.prompt`；**失败时弹层保留**，便于改正后重试；
 * - 执行任务期间后端会拒绝切换项目与会话，这里把入口置灰，避免"点了没反应"；
 * - 会话可以重命名、归档（移出列表但数据保留）、从归档恢复，或彻底删除（不可恢复）；
 * - 会话操作都带上所属项目的 `slug`，因此可以直接管理别的项目下的会话，不必先切过去。
 *
 * 视觉：项目是一层可点的行，会话缩进在下面；当前会话左侧有强调条。归档区折叠。
 */
import { App as AntApp, Button, Dropdown, Input, Modal, type MenuProps } from "antd";
import { useState } from "react";

import { endpoints, type ActionFn, type ProjectSummary } from "../api/client";
import { FolderPicker } from "./FolderPicker";
import { ChevronIcon, FolderIcon } from "./icons";

/** 弹层用途：项目的新建 / 重命名，以及会话重命名（选文件夹一律走目录选择器）。 */
type Dialog =
  | { kind: "rename-project"; project: ProjectSummary }
  | { kind: "rename-session"; project: ProjectSummary; session: string };

const DIALOG_TITLE: Record<Dialog["kind"], string> = {
  "rename-project": "重命名项目",
  "rename-session": "重命名会话",
};

const DIALOG_OK: Record<Dialog["kind"], string> = {
  "rename-project": "重命名",
  "rename-session": "重命名",
};

/** 目录选择器的用途：给新项目挑父目录，或挑一个现有文件夹当项目。 */
type Picking = "" | "folder";

/** 行内的 ⋯ 操作入口：外层 `span` 拦冒泡，点菜单不会连带触发整行（切换项目/会话）。 */
function RowMenu({ menu, label, disabled }: { menu: MenuProps; label: string; disabled: boolean }) {
  return (
    <span onClick={(event) => event.stopPropagation()}>
      <Dropdown menu={menu} trigger={["click"]} disabled={disabled}>
        <button type="button" className="row-menu" aria-label={label} title={label}>
          ⋯
        </button>
      </Dropdown>
    </span>
  );
}

export function Sidebar({
  projects,
  defaultParent,
  running,
  onAction,
}: {
  projects: ProjectSummary[];
  defaultParent: string;
  running: boolean;
  onAction: ActionFn;
}) {
  const { message, modal } = AntApp.useApp();
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const [picking, setPicking] = useState<Picking>("");
  const [name, setName] = useState("");
  const [openedArchive, setOpenedArchive] = useState<Set<string>>(new Set());

  const open = (next: Dialog) => {
    setName(next.kind === "rename-project" ? next.project.name : next.kind === "rename-session" ? next.session : "");
    setDialog(next);
  };

  const canSubmit = Boolean(dialog) && Boolean(name.trim());

  /** 提交弹层：每种用途对应一个动作端点；失败时保持弹层打开，便于改正后重试。 */
  const submit = async () => {
    if (!dialog || !canSubmit) return;
    try {
      if (dialog.kind === "rename-project") {
        await onAction({ path: endpoints.projectRename, body: { slug: dialog.project.slug, name: name.trim() } });
      } else {
        await onAction({
          path: endpoints.sessionRename,
          body: { from: dialog.session, to: name.trim(), slug: dialog.project.slug },
        });
      }
    } catch {
      return; // 失败原因已由动作层提示，弹层留着让用户改
    }
    setDialog(null);
  };

  const copyFolder = async (folder: string) => {
    try {
      await navigator.clipboard.writeText(folder);
      void message.success("已复制工作文件夹路径");
    } catch {
      // 剪贴板只在安全上下文可用（局域网用 http 访问时不可用）：把路径显示出来让用户手动复制
      void message.warning(`复制失败，请手动复制：${folder}`);
    }
  };

  const confirmRemoveProject = (project: ProjectSummary) => {
    modal.confirm({
      title: `移除项目「${project.name}」？`,
      content: "只从左侧列表移除，工作文件夹与会话数据都保留；之后可以用「使用现有文件夹」重新加入。",
      okText: "移除",
      okButtonProps: { danger: true },
      cancelText: "取消",
      // 失败原因由动作层提示；这里吞掉异常，避免 antd 把 reject 当成未处理的错误
      onOk: () => onAction({ path: endpoints.projectRemove, body: { slug: project.slug } }).catch(() => undefined),
    });
  };

  const confirmDeleteSession = (project: ProjectSummary, session: string, archived: boolean) => {
    modal.confirm({
      title: `彻底删除会话「${session}」？`,
      content: archived
        ? "会从磁盘删除这个归档会话的全部记录（summary.json 与 detail.json），无法恢复。"
        : "会从磁盘删除这个会话的全部记录（summary.json 与 detail.json），无法恢复；只想让它离开列表，请改用「归档对话」。",
      okText: "彻底删除",
      okButtonProps: { danger: true },
      cancelText: "取消",
      onOk: () =>
        onAction({
          path: endpoints.sessionDelete,
          body: { name: session, slug: project.slug, archived },
        }).catch(() => undefined),
    });
  };

  const projectMenu = (project: ProjectSummary): MenuProps => ({
    items: [
      { key: "session", label: "新建会话" },
      { key: "copy", label: "复制工作文件夹路径" },
      { key: "rename", label: "重命名项目" },
      { type: "divider" },
      { key: "remove", label: "移除项目", danger: true },
    ],
    onClick: ({ key }) => {
      if (key === "session") void onAction({ path: endpoints.sessionCreate, body: { slug: project.slug } });
      else if (key === "copy") void copyFolder(project.folder);
      else if (key === "rename") open({ kind: "rename-project", project });
      else if (key === "remove") confirmRemoveProject(project);
    },
  });

  const sessionMenu = (project: ProjectSummary, session: string): MenuProps => ({
    items: [
      { key: "rename", label: "重命名会话" },
      { key: "archive", label: "归档对话" },
      { type: "divider" },
      { key: "delete", label: "彻底删除", danger: true },
    ],
    onClick: ({ key }) => {
      if (key === "rename") open({ kind: "rename-session", project, session });
      else if (key === "archive")
        void onAction({ path: endpoints.sessionArchive, body: { name: session, slug: project.slug } });
      else if (key === "delete") confirmDeleteSession(project, session, false);
    },
  });

  const archivedMenu = (project: ProjectSummary, session: string): MenuProps => ({
    items: [
      { key: "restore", label: "恢复到会话列表" },
      { type: "divider" },
      { key: "delete", label: "彻底删除", danger: true },
    ],
    onClick: ({ key }) => {
      if (key === "restore")
        void onAction({ path: endpoints.sessionRestore, body: { name: session, slug: project.slug } });
      else if (key === "delete") confirmDeleteSession(project, session, true);
    },
  });

  const toggleArchive = (slug: string) =>
    setOpenedArchive((previous) => {
      const next = new Set(previous);
      if (next.has(slug)) next.delete(slug);
      else next.add(slug);
      return next;
    });


  return (
    <>
      <Button
        className="new-session"
        disabled={running}
        onClick={() => setPicking("folder")}
      ><span aria-hidden="true">＋</span> 新建项目</Button>
      <div className="sidebar-head"><span className="sidebar-title">项目列表</span></div>

      <div className="sidebar-scroll">
        {projects.length === 0 && <div className="tree-hint">还没有项目，请点击上方“新建项目”选择文件夹</div>}

        {projects.map((project) => {
          const sessions = project.sessions ?? [];
          const archived = project.archived ?? [];
          const archiveOpen = openedArchive.has(project.slug);

          return (
            <div key={project.slug} className={project.current ? "project current" : "project"}>
              <div
                className="project-row"
                role="button"
                tabIndex={0}
                onClick={() => void onAction({ path: endpoints.projectOpen, body: { slug: project.slug } })}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    void onAction({ path: endpoints.projectOpen, body: { slug: project.slug } });
                  }
                }}
              >
                <span className="project-folder"><FolderIcon size={17} /></span>
                <span className="project-name" title={project.name}>
                  {project.name}
                </span>
                {project.missing && <span className="chip danger">!</span>}
                <RowMenu menu={projectMenu(project)} label={`项目「${project.name}」的操作`} disabled={running} />
              </div>

              <div className="project-path" title={project.folder}>
                {project.folder}
              </div>
              {project.missing && (
                <div className="project-warn">工作文件夹不存在：移除后可用「使用现有文件夹」重新加入</div>
              )}

              <div className="sessions">
                {sessions.length === 0 && (
                  <div className="tree-hint">
                    {archived.length === 0 ? "还没有会话（⋯ → 新建会话）" : "没有打开的会话"}
                  </div>
                )}

                {sessions.map((session) => (
                  <div
                    key={session.name}
                    className={
                      session.current
                        ? session.running
                          ? "session current running"
                          : "session current"
                        : session.running
                          ? "session running"
                          : "session"
                    }
                    role="button"
                    tabIndex={0}
                    title={session.running ? `${session.name}（执行中）` : session.name}
                    onClick={() =>
                      void onAction({
                        path: endpoints.sessionOpen,
                        body: { name: session.name, slug: project.slug },
                      })
                    }
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        void onAction({
                          path: endpoints.sessionOpen,
                          body: { name: session.name, slug: project.slug },
                        });
                      }
                    }}
                  >
                    {/* 执行中的会话画一个转动的点：并发跑任务时，否则完全看不出哪个会话在干活 */}
                    {session.running && <i className="session-running-dot" aria-label="执行中" />}
                    <span className="label">{session.name}</span>
                    <span className="count">{session.tasks}</span>
                    <RowMenu
                      menu={sessionMenu(project, session.name)}
                      label={`会话「${session.name}」的操作`}
                      disabled={running}
                    />
                  </div>
                ))}

                {archived.length > 0 && (
                  <button type="button" className="archive-toggle" onClick={() => toggleArchive(project.slug)}>
                    <ChevronIcon className={archiveOpen ? "rot90" : ""} />
                    归档 · {archived.length}
                  </button>
                )}

                {archiveOpen &&
                  archived.map((session) => (
                    <div key={`archived-${session}`} className="session" title={session}>
                      <span className="label" style={{ color: "var(--text-faint)" }}>
                        {session}
                      </span>
                      <RowMenu
                        menu={archivedMenu(project, session)}
                        label={`归档会话「${session}」的操作`}
                        disabled={running}
                      />
                    </div>
                  ))}
              </div>
            </div>
          );
        })}
      </div>

      <Modal
        open={dialog !== null}
        title={dialog ? DIALOG_TITLE[dialog.kind] : ""}
        okText={dialog ? DIALOG_OK[dialog.kind] : "确定"}
        cancelText="取消"
        okButtonProps={{ disabled: !canSubmit }}
        onOk={submit}
        onCancel={() => setDialog(null)}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <label className="field">
            <span className="field-label">{dialog?.kind === "rename-session" ? "会话名称" : "项目名"}</span>
            <Input
              autoFocus
              value={name}
              onChange={(event) => setName(event.target.value)}
              onPressEnter={() => canSubmit && submit()}
              placeholder="例如：年会材料"
            />
          </label>

        </div>
      </Modal>

      <FolderPicker
        open={Boolean(picking)}
        initial={defaultParent}
        onCancel={() => setPicking("")}
        onPick={(chosen) => {
          setPicking("");
          void onAction({ path: endpoints.projectOpenFolder, body: { path: chosen } });
        }}
      />
    </>
  );
}
