# 培风 · PeiFeng

本地运行的 AI 任务智能体：把一句话需求拆成计划，派给不同专长的 worker 执行，每一步由独立评审验收后再往下走。

面向"要真的把东西做出来"的场景——写文档、处理表格、出图、整理目录、检索资料，而不是只给一段回答。

```
你的一句话
   ↓
Planner  ── 识别意图、判断能不能做、拆成 1~N 步（每步指定 worker 与验收口径）
   ↓
Worker   ── 一步步执行：读素材、跑脚本、生成 docx/xlsx/pptx/png、落盘
   ↓
Critic   ── 每步之后独立核验（只读，不改），通过才进下一步；不通过就打回重做
   ↓
交付文件 + 会话记录（summary.json / detail/）
```

## 特点

- **评审是独立的**：每个 worker 目录里没有写权限给评审，`critic` 只有 `read` / `image_describe` / `office_inspect` 三个只读工具，它必须靠证据判断，不能替 worker 改东西。
- **产出必须落盘**：planner 的每一步都要写清产出文件名，worker 只把内容写在回答里会被判不通过。
- **做不到会重排**：worker 明确"这步做不到"时，编排层让 planner 只重排剩余步骤，已完成的结果保留，不从头再来。
- **并行只在产出不冲突时成立**：计划里标了同组的几步会同时执行、整组只评审一次；如果两步要写同一个文件，会被自动拆成串行。
- **输出不截断**：工具输出上限 30 万字符，超过就把**完整内容**落盘并把路径给模型，由它按需分段读——而不是悄悄砍掉（截断会让模型以为"文件里没有"，然后反复重试）。
- **上下文自动压缩**：占用达到模型窗口的 80% 时，先截超长的工具结果、再成组丢弃最旧的往返，保住任务请求与最近的进展。

## 快速开始

**便携包（推荐）**

1. 完整解压到有写入权限的目录，不要在压缩包内运行
2. 双击 `启动培风.bat`
3. 首次会提示填写 DeepSeek API key，之后浏览器自动打开
4. 关闭服务在终端按 `Ctrl+C`

包内自带 Python 运行时，**无需安装 Python / uv / Node.js**。模型调用需要网络和有效额度。

**从源码运行**

```bash
cd peifeng_agent/frontend
npm install && npm run build     # 构建前端

cd ../..
./runtime/python.exe launcher.py # 或 python launcher.py
```

## 目录结构

```
PeiFeng/
├── launcher.py                 启动入口：环境自检、起服务、开浏览器
├── runtime/                    便携 CPython（不入库）
├── peifeng_agent/
│   ├── .env                    API key 与服务配置（不入库）
│   ├── config.py               配置项与默认值
│   ├── backend/
│   │   ├── agent/              角色定义（目录即能力）
│   │   │   ├── planner/        规划者：只有 read
│   │   │   ├── critic/         评审者：read / image_describe / office_inspect
│   │   │   ├── common/         装配、上下文、工具箱、事务层、文件锁
│   │   │   └── executor/       各 worker，各自带 tool/ 与 skill/
│   │   ├── workflow/           编排：plan → step_loop → 评审 → 收尾
│   │   ├── server/             FastAPI + SSE 事件流
│   │   ├── memory/             会话与任务记录
│   │   ├── utils/              工具实现（读取、Office、图表、检索、看图）
│   │   └── data/               会话数据与缓存（不入库）
│   └── frontend/               React + Vite 页面
└── 使用说明.txt
```

## 角色与能力

**目录即归属**：每个 agent 只扫自己目录下的 `tool/` 与 `skill/`，不读任何元数据——所以 planner / critic 的目录里没有写与执行类工具，它们天然拿不到。

| 角色 | 工具 | 说明 |
|---|---|---|
| `planner` | `read` | 只读探查；输出 `direct` / `clarify` / `unable` / `plan` |
| `critic` | `read` `image_describe` `office_inspect` | 只读核验，判通过/不通过 |
| `coding` | `read` `write` `bash` `delete` `move` | 脚本、数据处理、批处理 |
| `office_docs` | 上面 + `docx` `xlsx` `pptx` `render_pages` `image_describe` | 文档、表格、演示稿 |
| `chart` | `bar_chart` `line_chart` `pie_chart` `scatter_chart` `image_describe` … | 数据图 |
| `file_manager` | `read` `write` `bash` `delete` `move` | 目录整理、归档、清单 |
| `web_search` | `search_web` `fetch_web` `read` `write` … | 检索、抓正文、核对来源 |

## 核心机制

**工具批量参数优先**——每个工具都能一次处理多个目标，减少往返：

```python
read(targets=[{"path": "a.md"}, {"path": "b.md"}, {"path": "c.md"}])   # 一次读三个
write(files=[{"path": "x.md", "content": …}]) # 一次写三个
bash(commands=["dir", "python x.py"])        # 一次跑多条
search_web(queries=["…", "…"])               # 一次搜多组
```

**写文件走事务**：影子副本 → 冲突检测（size + SHA1）→ 备份 → 替换，同一文件跨进程加锁（30 秒超时）。并发改同一文件时用最新内容重做，而不是直接失败。

**Office 文档用专用工具而不是脚本**：

```python
pptx(file="x.pptx", slides=[…], theme={…})     # 生成
pptx(file="x.pptx", inspect=True)               # 看结构：页数、每页对象的位置/字号/自动缩放
pptx(file="x.pptx", script="…")                 # 需要精细控制时跑 python-pptx 代码
```

## 配置

`.env` 放在 `peifeng_agent/` 下：

```ini
api_key=sk-xxx
model=deepseek-flash
base_url=https://api.deepseek.com/v1
```

常用可调项（`config.py` 里都有默认值）：

| 配置 | 默认 | 说明 |
|---|---|---|
| `context_compress_ratio` | `0.8` | 上下文占用到窗口的多少比例时自动压缩，`0` 关闭 |
| `model_context_limit` | 未设 | 模型窗口大小；不配时按 1M 估算 |
| `tool_output_limit` | `300000` | 单次工具输出上限（字符），超过落盘给路径 |
| `max_tool_rounds` | `45` | worker 单步的最大工具轮次 |
| `planner_rounds` / `critic_rounds` | `15` / `15` | 规划与评审各自的工具轮次上限 |
| `step_max_retries` | `3` | 每一步最多尝试几次（含首次） |
| `plan_max_steps` | `8` | 计划步数上限 |
| `stream` | `True` | 是否流式输出 |

## 会话记录

- `summary.json`：跨任务与步骤摘要，下一次规划时可见
- `detail/`：**每步一个文件**，`task{任务号}_step{步骤号}.json`，内容是那一步的完整明细
  （每次尝试、工具调用、评审结论）。单文件十来 KB，整份读即可

```python
read(targets=[{"path": "…/detail/task1_step01.json"}])      # 直接读那一步
read(targets=[{"path": "…/detail", "grep": "task1_step"}])  # 筛出该任务的所有步骤文件
```

## 已知限制

- **PPT/PDF 页面预览**需要外部软件：PPT 渲染要 PowerPoint，PDF 渲染要 Poppler，包内未附带
- **自定义任务需要的 node / git / 其他程序**不在包内，`bash` 用的是你机器上的环境
- `bash` 工具能做的事最多（跑脚本、装包、系统操作），所以"别用它代替专用工具"只能靠工具描述约束，不是硬限制
- 复杂联网检索依赖外部站点：数据源缺失、附件要验证码这类情况，agent 会标注缺口而不是编造数字

## 开发

```bash
# 前端
cd peifeng_agent/frontend
npm run typecheck
npm run dev          # 本地开发
npm run build        # 产出到 dist/

# 环境自检
./runtime/python.exe launcher.py --check

# 调用统计（跑任务后看往返与批量占比）
./runtime/python.exe -m peifeng_agent.report_calls
```

## License

未指定。使用前请自行确认与 DeepSeek API 服务条款的兼容性。
