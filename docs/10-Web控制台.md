# Web 控制台设计（P2 · 图形 + 终端双模式）

> 目标：让 CLI 流水线**可看、可操作、可排障**，同时不牺牲 CLI 的原生能力。
> 实现：`specflow/web.py` + `specflow/web_static/index.html`（标准库 `http.server`，零第三方依赖）。

## 1. 参考调研

GitHub 上 "spec-driven development" 类项目很多，但**带可视化控制台**的很少，且没有一个是 P2 这种"9 阶段状态机 + 产物 hash 契约 + 人工门控"的组合：

| 项目 | 形态 | 可借鉴点 | P2 的差异 |
|---|---|---|---|
| [spec-kitty/spec-kitty](https://github.com/spec-kitty/spec-kitty) | CLI + 看板/进度追踪 | 任务看板、阶段进度、spec 治理 | P2 以 **阶段状态机 + 门控**为主轴，产物是唯一可信输入 |
| [buildermethods/agent-os](https://github.com/buildermethods/agent-os) | CLI / Markdown | standards/spec 注入、命令约定 | P2 有产物 front-matter 契约与 hash 校验 |
| [gsd-build/gsd-2](https://github.com/gsd-build/gsd-2) | CLI / TS | 长任务上下文工程、spec-driven | P2 把 TDD/QA 做成可门控阶段 |
| [gemini-cli-extensions/conductor](https://github.com/gemini-cli-extensions/conductor) | 插件 | specify → plan → implement 三阶段 | P2 扩展到 9 阶段，并带归档回灌 |

结论：**没有可直接复用的"specflow 前端"**，因此按 P2 的功能自建；信息架构参考了上述项目的"阶段进度 + 产物 + 门控"三件套。

## 2. 信息架构

```text
┌ 顶部：模式切换（图形界面 / 终端） · 项目路径 · 模式(mock/live) · 刷新/校验/自检/初始化
├ 左栏：9 阶段流水线（当前/已到达/未到达 + 门控状态）
└ 主区（图形模式）
   ├ 概览：当前阶段、产物数、批准数、校验问题、下一步命令、门控批准/打回/修订
   ├ 产物：列表（状态/版本/hash/摘要）→ 点击阅读 Markdown（保留元信息 chips）
   ├ Trace：hooks / stage-*.jsonl 事件明细
   └ 运行：阶段命令 + 实时日志（SSE）
终端模式：内嵌终端（命令历史 ↑/↓、常用命令快捷按钮、实时输出）
```

## 3. 后端接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/state?check=0\|1` | 阶段/门控/产物/问题/命令/Trace 列表聚合 |
| GET | `/api/artifact?name=design.md` | 产物元信息 + Markdown 正文（路径穿越防护） |
| GET | `/api/traces`、`/api/trace?name=...` | Trace 列表 / 事件 |
| GET | `/api/doctor` | 环境自检 |
| POST | `/api/init` | 初始化 `.spec` / `hooks.json` / `AGENTS.md` |
| POST | `/api/check` | 产物字段与 hash 校验 |
| POST | `/api/gate` | `{name, action, note}` 门控动作 |
| GET | `/api/run/stream?cmd=/stage ...` | 执行阶段命令（SSE 日志） |
| GET | `/api/cli/stream?cmd=status --json` | 终端模式：通用 CLI 子命令（SSE 日志） |

## 4. 安全边界

- 默认只绑定 `127.0.0.1`；`--host 0.0.0.0` 需自行承担暴露风险。
- 阶段命令与终端命令均**不经过 shell**；终端只允许 `status / doctor / init / run / gate / restore / unlock / --help`。
- 产物/Trace 名称做白名单式防穿越；静态文件限制在 `specflow/web_static/`。
- Web 端不做鉴权（本地工具定位）；如需远程使用，应放在反向代理 + 认证之后。

## 5. 已知限制

- 终端是"命令 + 流式输出"，不是完整 PTY（无交互式编辑器、无 Tab 补全）。
- 页面 15s 自动轮询状态；命令执行期间只刷新日志。
- 大 Trace 文件前端只展示前 500 条事件。
- Markdown 渲染为轻量实现（标题/列表/代码/表格/引用/链接），不含 HTML 直出。

## TDD Agent 面板

- 入口：顶部 **Agent** 标签；数据源 `.spec/artifacts/tdd-report.jsonl`（`/api/agents`）。
- 展示：轮次、角色（Planner / Test Writer / Executor / Verifier）、事件（plan_ready/test_written/impl_written/fail/pass/needs_human）、steps/files/cases/passed/total/failure_items/tokens/elapsed_ms；顶部汇总轮次、事件数、verdict。
- 说明：Agent 不是常驻进程，只在 `/add` 的 TDD 循环内运行；mock 模式为确定性角色，live 模式走真实模型。
