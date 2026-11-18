#!/usr/bin/env python3
"audit_snapshot.py — ARS v3.6.7 第 6 步 第 6.1 阶段 字节精确快照辅助工具。\n\n读取 bundle 文件（主要交付物 + 支持性上下文 + 审计模板 +\n可选的先前发现），将其验证为纯文本（拒绝 NUL 字节），\n从内存中的字节计算 SHA-256（每个文件单次读取——无 TOCTOU 窗口），\n输出规范 bundle 清单，并在 stdout 上写入 JSON 摘要供\n包装器消费。\n\n此脚本取代 Bash 侧的 `_snapshot_file` 辅助工具。Bash 版本\n存在三个结构性问题（codex 审查第 1-5 轮暴露了这些问题）：\n\n  F-004（P1，5 轮部分修复）：Bash 命令替换在\n    子 shell 中运行辅助工具。在子 shell 内进行的变量赋值\n    （例如 `_LAST_SNAPSHOT_SHA`）不会传播到父 shell。包装器看到空的 SHA。\n\n  F-018（P1）：Bash 中的 `grep -q $'\\0'` 将 `$'\\0'` 视为空模式，\n    匹配每个非空文件。每个文本文件都被拒绝为二进制。\n\n  F-020（P1）：`$(cat file)` 去除尾部换行并丢弃 NUL 字节。\n    清单 SHA（从内存内容计算）与 `sha256sum file`\n    （从磁盘字节计算）发生漂移。第 3a 步总是报告虚假变更。\n\nPython 实现逐字读取字节，从这些精确字节进行哈希，\n并通过 JSON 将清单和 SHA 都发送给包装器——无子 shell、无\n尾部换行漂移、二进制安全的 NUL 检测。包装器通过 `python3 audit_snapshot.py ... | jq`\n或从 tmp 文件读取来消费 JSON。\n\nCLI 模式：\n\n  --snapshot\n    为 bundle 计算快照。写入：\n      - <output-dir>/<run_id>.manifest.txt（按规范 §3.6）\n      - <output-dir>/<run_id>.prompt.txt（渲染的审计提示）\n      - JSON 摘要到 stdout：{primary_shas, supporting_shas, template_sha,\n        manifest_sha, bundle_files, prompt_path}\n\n  --verify\n    重新计算 bundle 文件的 SHA 并与清单比较。由包装器的第 3a 步\n    使用，以检测快照后的磁盘变更。若无变更则退出 0，\n    否则退出 1 并在 stdout 上输出变更路径。\n\n退出码：\n  0   成功\n  64  EX_USAGE — 参数错误或输入包含 NUL\n  1   verify 模式：检测到变更（路径打印到 stdout，每行一个）\n  2   内部错误（文件未找到、JSON 序列化失败等）"

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def read_bytes_or_die(path: str) -> bytes:
    '读取文件的精确字节。文件缺失或不可读时以退出码2退出。'
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        print(f"audit_snapshot: file not found: {path}", file=sys.stderr)
        sys.exit(2)
    except PermissionError:
        print(f"audit_snapshot: permission denied: {path}", file=sys.stderr)
        sys.exit(2)


def reject_if_binary(path: str, content: bytes) -> None:
    '拒绝包含 NUL 的输入（F-018 关闭）。\n\n    阶段 6.1 审计是 UTF-8 文本交付物（markdown、JSON、YAML）。\n    Bash 命令替换会静默剥离 NUL 字节；Python 的\n    二进制安全 `b"\\0" in content` 测试能干净地捕获它们。'
    if b"\0" in content:
        print(
            f"audit_snapshot: file contains NUL bytes (binary not supported): {path}",
            file=sys.stderr,
        )
        sys.exit(64)


def sha256_hex(content: bytes) -> str:
    '从内存字节计算SHA-256十六进制摘要。'
    return hashlib.sha256(content).hexdigest()


def _extract_template_sections(template_str: str, section_numbers: list[int]) -> str:
    '从审计模板中提取指定的 Section 块。\n\n    该模板使用 `## Section N — Title` 标题和 `---` 分隔符。\n    F-023（P1，R6）：我们用真实的轮次元数据替换 Section 1/2/4/5\n    如上；模板中这些章节的占位版本不得同时嵌入，\n    否则 codex 会在提示中看到 `{N}` / `{git_sha}` 字面量。\n\n    F-026（P2，R7）：最后提取的章节必须在下一个\n    顶层 `## ` 标题处终止（例如 `## Worked example`、`## Cross-references`），\n    而不是在文件末尾终止。没有此保护，Section 7 的提取会拉入\n    附录中的示例——其中嵌入了 synthesis_agent 4(f) 条款——\n    进入为非 synthesis 代理分派的提示，与我们上面替换的真实\n    Section 4(f) 相矛盾。\n\n    PR #78 codex 第 1 轮 P1：标题检测必须跳过围栏代码块（```/~~~）内的标题。\n    v3.7.1 Section 0 Scope Report 包含一个围栏子块，其第一行以\n    `## Codex Audit Round N — Scope Report` 开头；若不感知围栏，\n    该行会被视为下一个 H2 边界，Section 0 会在围栏中途被截断。\n\n    返回拼接后的章节文本（保留开头的 `## Section N` 标题\n    完整，并排除附录）。'
    import re as _re

    # 用空白遮蔽围栏代码块（```...``` 和 ~~~...~~~），以便
    # 其中的标题不计作章节边界。长度为
    # 保留匹配偏移量，使其与原始字符串保持对齐。
    def _mask_fences(s: str) -> str:
        fence_re = _re.compile(
            r"^([ \t]*)(```|~~~)[^\n]*\n.*?^\1\2[ \t]*$",
            _re.DOTALL | _re.MULTILINE,
        )
        def _repl(m: _re.Match[str]) -> str:
            return "".join(" " if ch != "\n" else "\n" for ch in m.group(0))
        return fence_re.sub(_repl, s)

    masked = _mask_fences(template_str)

    # 匹配章节标题或任何其他顶级`## `标题
    # （例如“## Worked example”、“## Cross-references”）；后者终止
    # 最后提取的部分。
    boundary_re = _re.compile(r"^## (?:Section (\d+)( —|$)|.+)", _re.MULTILINE)
    matches = list(boundary_re.finditer(masked))
    if not matches:
        return template_str  # 未找到标题——回退到逐字输出

    # 识别哪些匹配是真正的 Section N 起始（group(1) 已填充）
    section_starts: list[tuple[int, int]] = []  # (章节编号, 匹配索引)
    for i, m in enumerate(matches):
        if m.group(1) is not None:
            section_starts.append((int(m.group(1)), i))

    requested = set(section_numbers)
    if not requested.issubset({n for n, _ in section_starts}):
        missing = requested - {n for n, _ in section_starts}
        raise ValueError(
            f"audit_snapshot: requested template sections {sorted(missing)} not found "
            f"(template has Sections {sorted({n for n, _ in section_starts})})"
        )

    parts: list[str] = []
    for sec_num, match_idx in section_starts:
        if sec_num not in requested:
            continue
        start = matches[match_idx].start()
        # 在下一个任意类型的`## `标题（章节或附录）处结束，
        # 不只是下一个Section。这会捕获Section 7之后的附录。
        end = (
            matches[match_idx + 1].start()
            if match_idx + 1 < len(matches)
            else len(template_str)
        )
        parts.append(template_str[start:end].rstrip() + "\n\n")
    return "".join(parts)


_SECTION_4F_BY_AGENT = {
    "synthesis_agent": (
        "(f) Cross-section consistency check: for every source cited in 2+ "
        "sections of the primary deliverable, verify the source's "
        "characterization is compatible across sections; flag any pair of "
        "sections that pull the source's effect in incompatible directions "
        "(Pattern A1)."
    ),
    "research_architect_agent": (
        "(f) Construct-equivalence test: for every survey item labelled "
        "'reverse-coded', verify it meets the construct-equivalence "
        "definition in `shared/references/psychometric_terminology_glossary.md`."
    ),
    "report_compiler_agent": (
        "(f) Mandatory three-part check: (i) word count = "
        "`len(body.split())` <= publisher cap minus 3-5% buffer per "
        "`shared/references/word_count_conventions.md`; (ii) every entry of "
        "the upstream `protected_hedges` block per "
        "`shared/references/protected_hedging_phrases.md` appears verbatim "
        "in the abstract; (iii) no claim in the abstract is less hedged than "
        "its anchor in the body. Failure of any sub-check is a P1 finding."
    ),
}


def render_prompt(
    audit_template: bytes,
    primary_paths: list[str],
    primary_contents: list[bytes],
    supporting_paths: list[str],
    supporting_contents: list[bytes],
    round_n: int,
    target_rounds: int,
    git_sha: str,
    stage: int,
    agent: str,
    prior_findings: Optional[bytes],
) -> bytes:
    '渲染发送到 codex stdin 的审计提示。\n\n    该提示内嵌每个 bundle 文件在 AT-SNAPSHOT-TIME 的字节。Codex\n    看到的正是其 SHA-256 已写入清单的那些字节——不存在\n    第二次文件读取，也不存在 TOCTOU 窗口。\n\n    F-023（P1，R6）：位于\n    shared/templates/codex_audit_multifile_template.md 的审计模板包含\n    位于 {curly_braces} 中的占位符，由编排器在发送给 codex 之前填充\n    （第 1 节轮次元数据、第 2 节 git_sha、第 4(f) 节 bundle 特定\n    检查）。第 5 轮重新设计最初逐字嵌入模板，\n    导致占位符未被替换——codex 会在提示中看到字面量 `{N}`，\n    并要么完全省略第 4(f) 节，要么产生\n    无法解析的第 6 节裁决。此实现现在进行显式的\n    bundle 特定替换，并围绕真实值重建第 1、2 和 4 节，同时逐字保留模板中的\n    第 3 / 5 / 6 / 7 节（它们描述 codex 的预期输出，而非 bundle）。\n\n    返回 bytes（而非 str）以保留二进制保真度。'
    section_4f = _SECTION_4F_BY_AGENT.get(
        agent,
        f"(f) Bundle-specific check (no agent-specific clause registered for {agent}; "
        "treat 4(f) as N/A and run only (a)-(e)).",
    )

    if round_n <= 1:
        prior_summary = "none (first round, baseline audit)"
    elif prior_findings is not None:
        prior_summary = (
            "(prior findings attached as supporting context — see file listing "
            "below; verify each carries-forward / closes per (a) and (e))"
        )
    else:
        prior_summary = "(no previous-findings file provided)"

    primary_listing = "\n".join(f"- {p}" for p in primary_paths) or "- (none)"
    supporting_listing = (
        "\n".join(f"- {p}" for p in supporting_paths) if supporting_paths else "- (none)"
    )

    # 提取审计模板第0、3、6、7节（逐字——第0节是
    # v3.7.1 D2 Scope Report 块按规范 §3.2 每轮逐字保留；
    # 第3 / 6 / 7节描述codex的预期输出 / 维度 /
    # 反伪造防护）。第1、2、4、5节在下方以真实
    # 值，因此我们跳过模板的占位符版本以避免显示
    # codex 同一节的两份副本（一份带占位符，一份
    # 替换后）— F-023 关闭还要求占位符文本不
    # 出现。第0节包含一个`<N_total>`等占位符集合，即codex
    # 预计在审计时从捆绑库存中填充，因此它随之而来
    # 逐字对照第3节/第6节/第7节（codex第1轮PR #78 P1关闭）。
    template_str = audit_template.decode("utf-8", errors="replace")
    section_0 = _extract_template_sections(template_str, [0])
    section_3_to_7 = _extract_template_sections(template_str, [3, 6, 7])

    rendered_intro = (
        f"# ARS v3.6.7 cross-model audit — round {round_n} of {target_rounds}\n"
        f"# Stage: {stage} | Agent: {agent}\n"
        f"# Git SHA at audit start: {git_sha}\n\n"
        f"{section_0}"
        f"## Section 1 — Round metadata\n\n"
        f"Audit round: {round_n} of {target_rounds}\n"
        f"Previous rounds: {prior_summary}\n"
        f"Bundle scope: Stage {stage} deliverable for {agent}\n\n"
        f"## Section 2 — Bundle inventory\n\n"
        f"Authoritative context (commit {git_sha}):\n\n"
        f"Primary deliverables (audit target):\n{primary_listing}\n\n"
        f"Supporting context (do not audit; reference only):\n{supporting_listing}\n\n"
        f"## Section 4 — Round {round_n} job\n\n"
        f"(a) Verify each round-{round_n - 1} finding closed correctly. List by ID.\n"
        f"(b) Audit for new issues introduced by round-{round_n - 1} corrections (cascade audit).\n"
        f"(c) Run the 7 audit dimensions (§3.1-§3.7) plus the bundle-specific Section 4(f) check on the primary deliverables. Report each finding with the dimension or `4(f)` that surfaced it.\n"
        f"(d) Anchoring-bias residual check on closed findings.\n"
        f"(e) PARTIAL-vs-CLOSED check.\n"
        f"{section_4f}\n\n"
        f"## Section 5 — Convergence target\n\n"
        f"Convergence target: ZERO findings of ANY severity in one round.\n\n"
    )

    parts: list[bytes] = []
    parts.append(rendered_intro.encode("utf-8"))
    parts.append(section_3_to_7.encode("utf-8"))
    parts.append(b"\n\n## Primary deliverables (audit target)\n\n")
    for path, content in zip(primary_paths, primary_contents):
        parts.append(f"--- PRIMARY: {path} ---\n".encode("utf-8"))
        parts.append(content)
        parts.append(b"\n")
    if supporting_paths:
        parts.append(b"\n## Supporting context (reference only)\n\n")
        for path, content in zip(supporting_paths, supporting_contents):
            parts.append(f"--- SUPPORTING: {path} ---\n".encode("utf-8"))
            parts.append(content)
            parts.append(b"\n")
    return b"".join(parts)


def write_manifest(
    out_path: str,
    primary_shas: list[tuple[str, str]],
    supporting_shas: list[tuple[str, str]],
    audit_template_path: str,
    audit_template_sha: str,
) -> str:
    '按§3.6编写规范捆绑清单。\n\n    格式：<role>:<repo-relative-path>:<sha256-hex>，每个文件一行，\n    按(role, path)排序，排序方式等同于`LC_ALL=C sort`。\n    返回清单自身的SHA-256（== `bundle_manifest_sha`）。'
    lines: list[str] = []
    for path, sha in primary_shas:
        lines.append(f"primary:{path}:{sha}")
    for path, sha in supporting_shas:
        lines.append(f"supporting:{path}:{sha}")
    lines.append(f"template:{audit_template_path}:{audit_template_sha}")
    lines.sort()
    manifest_text = "\n".join(lines) + "\n"
    manifest_bytes = manifest_text.encode("utf-8")
    try:
        with open(out_path, "wb") as f:
            f.write(manifest_bytes)
    except OSError as e:
        print(f"audit_snapshot: manifest write failed: {e}", file=sys.stderr)
        sys.exit(2)
    return sha256_hex(manifest_bytes)


def write_prompt(out_path: str, prompt_bytes: bytes) -> None:
    '将渲染后的提示写入包装器提供给 codex stdin 的文件。'
    try:
        with open(out_path, "wb") as f:
            f.write(prompt_bytes)
    except OSError as e:
        print(f"audit_snapshot: prompt write failed: {e}", file=sys.stderr)
        sys.exit(2)


def dedupe_preserving_order(items: list[str]) -> list[str]:
    '在保留首次出现顺序的同时去除重复项（F-021）。'
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


# ---------------------------------------------------------------------------
# CLI 模式
# ---------------------------------------------------------------------------


def cmd_snapshot(args: argparse.Namespace) -> int:
    '快照捆绑文件，写入清单 + 提示，输出 JSON 摘要。'
    primary = list(args.primary or [])
    supporting = list(args.supporting or [])
    if args.previous_findings:
        # 将--previous-findings注入supporting（保留§3.6三角色枚举）。
        # 去重发生在归一化之后（包装器在上游完成归一化）。
        supporting.append(args.previous_findings)
    supporting = dedupe_preserving_order(supporting)

    if not primary:
        print("audit_snapshot: --primary is required", file=sys.stderr)
        return 64
    if not args.audit_template:
        print("audit_snapshot: --audit-template is required", file=sys.stderr)
        return 64
    if not args.output_dir or not args.run_id:
        print("audit_snapshot: --output-dir and --run-id are required", file=sys.stderr)
        return 64

    audit_template_bytes = read_bytes_or_die(args.audit_template)
    reject_if_binary(args.audit_template, audit_template_bytes)
    audit_template_sha = sha256_hex(audit_template_bytes)

    primary_contents: list[bytes] = []
    primary_shas: list[tuple[str, str]] = []
    for p in primary:
        content = read_bytes_or_die(p)
        reject_if_binary(p, content)
        primary_contents.append(content)
        primary_shas.append((p, sha256_hex(content)))

    supporting_contents: list[bytes] = []
    supporting_shas: list[tuple[str, str]] = []
    for s in supporting:
        content = read_bytes_or_die(s)
        reject_if_binary(s, content)
        supporting_contents.append(content)
        supporting_shas.append((s, sha256_hex(content)))

    # F-029（P3，R8）：在将 manifest/prompt 写入磁盘之前渲染提示词。
    # 如果 _extract_template_sections 引发 ValueError（请求的节未
    # 在模板中找到），--output-dir 中不会留下部分产物。
    # F-023（P1，R6）：传递 stage/agent/prior_findings，以便 render_prompt 能够
    # 替换audit-template占位符并选择特定于代理的
    # 第4(f)条条款。
    prior_findings_bytes: Optional[bytes] = None
    if args.previous_findings:
        # PREV_FINDINGS 刚刚作为 supporting 的一部分被快照；定位其内容。
        for path, content in zip([s for s, _ in supporting_shas], supporting_contents):
            if path == args.previous_findings:
                prior_findings_bytes = content
                break

    try:
        prompt_bytes = render_prompt(
            audit_template_bytes,
            [p for p, _ in primary_shas],
            primary_contents,
            [s for s, _ in supporting_shas],
            supporting_contents,
            args.round,
            args.target_rounds,
            args.git_sha or "unknown",
            args.stage,
            args.agent,
            prior_findings_bytes,
        )
    except ValueError as e:
        # F-029：干净退出 64，未留下任何部分产物。
        print(f"audit_snapshot: prompt render failed: {e}", file=sys.stderr)
        return 64

    # 现在可以安全写入——渲染成功，没有回溯路径。
    manifest_path = os.path.join(args.output_dir, f"{args.run_id}.manifest.txt")
    bundle_manifest_sha = write_manifest(
        manifest_path,
        primary_shas,
        supporting_shas,
        args.audit_template,
        audit_template_sha,
    )
    prompt_path = os.path.join(args.output_dir, f"{args.run_id}.prompt.txt")
    write_prompt(prompt_path, prompt_bytes)

    summary = {
        "manifest_path": manifest_path,
        "manifest_sha": bundle_manifest_sha,
        "prompt_path": prompt_path,
        "audit_template_path": args.audit_template,
        "audit_template_sha": audit_template_sha,
        "primary_files": [{"path": p, "sha": sha} for p, sha in primary_shas],
        "supporting_files": [{"path": s, "sha": sha} for s, sha in supporting_shas],
    }
    json.dump(summary, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    '对照快照清单验证捆绑文件。\n\n    读取由 --snapshot 模式写入的清单文件，根据当前磁盘内容重新计算每个\n    所引用文件的 SHA-256，并报告任何不匹配。由包装器的步骤 3a（codex 后变更检测）使用。'
    if not args.manifest:
        print("audit_snapshot: --manifest is required for --verify", file=sys.stderr)
        return 64

    try:
        with open(args.manifest, encoding="utf-8") as f:
            manifest_text = f.read()
    except OSError as e:
        print(f"audit_snapshot: manifest read failed: {e}", file=sys.stderr)
        return 2

    mutated_paths: list[str] = []
    for line in manifest_text.splitlines():
        line = line.strip()
        if not line:
            continue
        # <角色>:<路径>:<sha>
        # 路径中可能包含冒号（POSIX 路径可以包含冒号，尽管
        # rare)；从左侧最多拆分为3部分（角色 + 路径 + sha）
        # 但SHA始终是冒号之后的最后64个十六进制字符。
        if ":" not in line:
            continue
        # 查找SHA后缀：最后一个冒号，然后是64个十六进制字符。
        if len(line) < 65 or line[-65] != ":":
            continue
        path_with_role = line[:-65]
        expected_sha = line[-64:]
        # 拆分角色:路径
        role_sep = path_with_role.find(":")
        if role_sep == -1:
            continue
        # role = path_with_role[:role_sep]  # 验证时不需要
        path = path_with_role[role_sep + 1 :]

        # 从当前磁盘内容重新计算SHA
        try:
            with open(path, "rb") as f:
                actual_sha = sha256_hex(f.read())
        except FileNotFoundError:
            mutated_paths.append(path)
            continue
        if actual_sha != expected_sha:
            mutated_paths.append(path)

    if mutated_paths:
        for p in mutated_paths:
            print(p)
        return 1
    return 0


# ---------------------------------------------------------------------------
# 入口点
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="ARS v3.6.7 audit bundle snapshot helper. "
        "Modes: --snapshot (initial) or --verify (post-codex)."
    )
    sub = p.add_subparsers(dest="mode", required=True)

    snap = sub.add_parser("snapshot", help="Snapshot bundle, write manifest + prompt")
    snap.add_argument("--primary", action="append", required=True, help="Primary deliverable path (repeatable)")
    snap.add_argument("--supporting", action="append", default=[], help="Supporting context path (repeatable)")
    snap.add_argument("--previous-findings", help="Optional --previous-findings path (auto-injected into supporting with dedup)")
    snap.add_argument("--audit-template", required=True, help="Audit template path (single)")
    snap.add_argument("--output-dir", required=True, help="Where to write manifest.txt and prompt.txt")
    snap.add_argument("--run-id", required=True, help="Audit run identifier")
    snap.add_argument("--round", type=int, required=True, help="Current audit round")
    snap.add_argument("--target-rounds", type=int, required=True, help="Total target rounds")
    snap.add_argument("--git-sha", help="Repo HEAD short SHA (informational, embedded in prompt)")
    snap.add_argument("--stage", type=int, required=True, help="Stage transition (1-6) the audit gates")
    snap.add_argument(
        "--agent",
        required=True,
        choices=["synthesis_agent", "research_architect_agent", "report_compiler_agent"],
        help="Which v3.6.7 downstream agent produced the deliverable (selects Section 4(f) clause)",
    )

    ver = sub.add_parser("verify", help="Verify bundle files against snapshot manifest")
    ver.add_argument("--manifest", required=True, help="Path to <run_id>.manifest.txt")

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.mode == "snapshot":
        return cmd_snapshot(args)
    elif args.mode == "verify":
        return cmd_verify(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
