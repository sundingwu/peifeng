"""按涉及路径准备副本，并在工具成功后提交文件变更。

两套"副本"分工明确，不要合并：

- **防打断**：``stage/`` 是工作区的临时影子，工具在它上面改；被打断/失败就整体丢弃，
  真实文件因此不会留下半成品
- **可回溯**：提交前把**即将被覆盖或删除的原文件**另存一份到会话目录（见 :mod:`.backup`），
  提交成功后长期保留；提交失败或被打断时整批丢弃

**并发写同一个文件 -> 都生效**：提交时若发现原文件已被别人改过（另一个会话刚提交完），
不是报错放弃，而是**用最新内容重做一遍**（重新复制影子、重新执行工具、再提交）。
所以两个会话各自用 ``edits`` 改同一个文件的不同位置时，两处改动都会落地；
只有"改同一处"这种真冲突才会在重做时说找不到原文。
"""

from __future__ import annotations

import filecmp
import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from peifeng_agent.backend.agent.common.backup import discard, make_backup
from peifeng_agent.backend.agent.common.file_lock import lock_files

#: 提交时发现原文件被别人改过，就重做一遍；重做这么多轮仍冲突才判定失败
MAX_COMMIT_ATTEMPTS = 3

#: 需要事务保护的工具：它们会改工作区里的文件（bash 不在内——它想改什么无法预知）
PROTECTED_TOOLS = frozenset({
    "write",
    "move",
    "delete",
    "docx",
    "xlsx",
    "pptx",
    "bar_chart",
    "line_chart",
    "pie_chart",
    "scatter_chart",
})


def file_version(path):
    info = path.stat()
    return (info.st_size, info.st_mtime_ns, info.st_mode)


def file_fingerprint(path):
    """内容指纹：大小 + SHA1。

    提交前的"有没有被别人改过"要按**内容**判定，不能按 mtime：桌面文件常被同步盘、
    杀毒、Word 之类的东西 touch 一下（内容没变），用 mtime 比会把正常提交误判成冲突
    （实测报过"原文件正在被另一个任务反复修改"，其实没有别的任务）。
    """
    info = path.stat()
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return (info.st_size, digest.hexdigest())


def file_versions(root):
    return {
        str(path.relative_to(root)): file_version(path)
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def reject_links(path: Path, root: Path) -> None:
    """Check lexical paths before resolve can hide a link or directory junction."""
    current = Path(os.path.abspath(path))
    boundary = Path(os.path.abspath(root))
    current.relative_to(boundary)
    while True:
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise RuntimeError(f"修改路径不能包含符号链接或目录联接：{current}")
        if current == boundary:
            return
        current = current.parent


def run_staged(function, payload, stopped, vision=None, *, execute, session_dir=None):
    """Stage supported file mutations; commit only after successful completion.

    This is a workspace transaction boundary, not an OS sandbox for arbitrary code.

    ``session_dir`` 给定时，被覆盖/删除前的原文件会另存一份到会话目录（可回溯）；
    没给（未绑定会话）就只保留事务语义。备份失败一律中止提交。
    """
    tool_name = function.__name__
    workspace = payload.get("base_dir")
    if tool_name not in PROTECTED_TOOLS or workspace is None:
        return execute(function, payload, stopped, vision)
    workspace = Path(os.path.abspath(workspace))
    reject_links(workspace, workspace)
    workspace = workspace.resolve()

    def check_cancel():
        if stopped and stopped():
            raise RuntimeError("工具调用已取消；未提交的副本已丢弃")

    check_cancel()
    for attempt in range(MAX_COMMIT_ATTEMPTS):
        with tempfile.TemporaryDirectory(prefix="peifeng-tool-") as folder:
            folder_path = Path(folder)
            stage = folder_path / "workspace"
            stage.mkdir()
            originals: dict = {}
            path_keys = {"path", "file", "target", "output", "template", "template_file"}

            def copy_file(source, target):
                check_cancel()
                source = Path(source)
                if source.is_symlink() or (hasattr(source, "is_junction") and source.is_junction()):
                    raise RuntimeError(f"副本不支持链接：{source}")
                relative = str(source.relative_to(workspace))
                originals[relative] = file_fingerprint(source)
                return shutil.copy2(source, target)

            def stage_path(value, key):
                check_cancel()
                candidate = Path(value)
                lexical = candidate if candidate.is_absolute() else workspace / candidate
                reject_links(lexical, workspace)
                original = lexical.resolve()
                try:
                    relative = original.relative_to(workspace)
                except ValueError:
                    raise RuntimeError(f"路径必须位于项目工作区内：{value}") from None
                target = stage / relative
                if original.is_dir():
                    if key in {"target", "output"}:
                        target.mkdir(parents=True, exist_ok=True)
                    else:

                        def check_entries(folder, names):
                            check_cancel()
                            for entry in names:
                                reject_links(Path(folder) / entry, workspace)
                            return []

                        shutil.copytree(
                            original, target, copy_function=copy_file, ignore=check_entries, dirs_exist_ok=True
                        )
                elif original.is_file() and not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    copy_file(original, target)
                return str(target) if candidate.is_absolute() else str(relative)

            def translate(value):
                if isinstance(value, dict):
                    result = {}
                    for key, item in value.items():
                        if key in path_keys and isinstance(item, (str, Path)) and str(item):
                            result[key] = stage_path(item, key)
                        elif isinstance(item, (dict, list)):
                            result[key] = translate(item)
                        else:
                            result[key] = item
                    return result
                if isinstance(value, list):
                    return [translate(item) for item in value]
                return value

            staged_payload = translate(payload)
            staged_payload["base_dir"] = stage

            def include_conflicts(destination):
                reject_links(destination, workspace)
                destination = destination.resolve()
                relative = destination.relative_to(workspace)
                if not destination.exists():
                    return
                if destination.is_dir():
                    (stage / relative).mkdir(parents=True, exist_ok=True)
                else:
                    target = stage / relative
                    if not target.exists():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        copy_file(destination, target)
                # Mirror only occupied candidates that the tool's rename policy probes.
                for index in range(1, 1000):
                    check_cancel()
                    candidate = destination.with_name(
                        f"{destination.name}-{index}"
                        if destination.is_dir()
                        else f"{destination.stem}-{index}{destination.suffix}"
                    )
                    reject_links(candidate, workspace)
                    if not candidate.exists():
                        break
                    target = stage / candidate.relative_to(workspace)
                    if candidate.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                    elif not target.exists():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        copy_file(candidate, target)

            if tool_name == "move":
                for item in payload.get("items", []):
                    source = Path(item["path"])
                    source = source if source.is_absolute() else workspace / source
                    destination = Path(item["target"])
                    destination = destination if destination.is_absolute() else workspace / destination
                    if destination.is_dir():
                        destination = destination / source.name
                    include_conflicts(destination)
            elif tool_name == "write" and payload.get("path"):
                destination = Path(payload["path"])
                include_conflicts(destination if destination.is_absolute() else workspace / destination)
            before = file_versions(stage)
            original_directories = [path.relative_to(stage) for path in stage.rglob("*") if path.is_dir()]
            check_cancel()
            result = execute(function, staged_payload, stopped, vision)
            if stopped and stopped():
                raise RuntimeError("工具调用已取消；未提交的副本已丢弃")
            after = file_versions(stage)
            filecmp.clear_cache()
            changed = {
                name
                for name in before.keys() | after.keys()
                if before.get(name) != after.get(name)
                or (
                    name in before
                    and name in after
                    and (workspace / name).is_file()
                    and not filecmp.cmp(stage / name, workspace / name, shallow=False)
                )
            }
            # 提交是临界区：按文件的跨进程锁覆盖 CAS 校验 + 备份 + 替换。
            # 锁只覆盖提交（毫秒级），工具执行在锁外——写不同文件仍然并行。
            with lock_files(workspace / name for name in changed):
                stale = [
                    name
                    for name in changed
                    if (file_fingerprint(workspace / name) if (workspace / name).is_file() else None)
                    != originals.get(name)
                ]
                if stale:
                    if attempt + 1 < MAX_COMMIT_ATTEMPTS:
                        # 别人刚改过这个文件：作废本轮影子，用最新内容重做一遍
                        continue
                    raise RuntimeError(
                        f"原文件正在被另一个任务反复修改，重做 {MAX_COMMIT_ATTEMPTS} 次仍未提交成功："
                        f"{stale[0]}"
                    )
                # 回溯备份：即将被覆盖/删除的原文件先落一份到会话目录。备份失败会在这里抛错，
                # 于是本次提交根本不发生——不允许出现"没有备份的覆盖"。
                history = make_backup(
                    workspace,
                    session_dir,
                    tool_name,
                    [(item, "delete" if item not in after else "overwrite") for item in sorted(changed)],
                )
                # 回滚副本：只服务"提交本身失败"时的原地还原（与 history 分工不同，勿合并）
                rollback = folder_path / "rollback"
                rollback.mkdir()
                for name in changed:
                    original = workspace / name
                    if original.is_file():
                        saved = rollback / name
                        saved.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(original, saved)
                try:
                    # Commit is synchronous and not interrupted halfway by cancellation.
                    for name in sorted(changed):
                        target = workspace / name
                        if name not in after:
                            target.unlink()
                        else:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=".commit-")
                            os.close(handle)
                            try:
                                shutil.copy2(stage / name, temporary)
                                os.replace(temporary, target)
                            finally:
                                Path(temporary).unlink(missing_ok=True)
                    # Preserve empty-directory moves/deletions as well.
                    for relative in sorted(
                        original_directories, key=lambda path: len(path.parts), reverse=True
                    ):
                        original = workspace / relative
                        if original.is_dir() and not (stage / relative).exists():
                            if not any(original.iterdir()):
                                original.rmdir()
                    for directory in stage.rglob("*"):
                        if directory.is_dir():
                            (workspace / directory.relative_to(stage)).mkdir(parents=True, exist_ok=True)
                except OSError:
                    # Restore originals on ordinary commit failures. Hard process death is
                    # outside this cross-file transaction guarantee.
                    for name in changed:
                        target = workspace / name
                        saved = rollback / name
                        if saved.is_file():
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(saved, target)
                        elif target.is_file():
                            target.unlink()
                    for directory in original_directories:
                        (workspace / directory).mkdir(parents=True, exist_ok=True)
                    discard(history)  # 文件已还原：这份备份没有意义，别留在会话目录里
                    raise
                except BaseException:
                    # 取消、Ctrl+C、进程内异常：原文件没动，备份一并丢掉
                    discard(history)
                    raise
            note = history.note() if history is not None else ""
            if isinstance(result, str):
                body = result.replace(str(stage), str(workspace))
                return f"{body}\n{note}" if note else body
            return result
    raise RuntimeError("文件提交未能完成：请稍后重试")
