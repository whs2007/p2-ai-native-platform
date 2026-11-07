# 11 · 把 specflow 嵌进其他项目（附 Claude Code 接入）

> 版本：v1.1 ｜ 更新：2026-11-01
> 前提：已按 README 装好 Python ≥3.11 与 specflow。
> 目标：讲清 specflow 怎么作为**外部工具**服务一个真实仓库，以及接上 **Claude Code Hooks** 后，人和 AI 各自看到什么。全文用 `crm-service` 一个例子贯穿。

---

## 1. 先把关系讲清楚

specflow **不是 Claude Code 插件，也不是要复制进目标项目的框架代码**。它更像 `git`：

- 工具本身装在系统里（一个 Python 包 + 一堆 skills/templates）；
- 每个仓库里只留下**这次工作产生的数据**（`.spec/`、`AGENTS.md`）。

```text
   specflow 平台（装一次，多处复用）
   CLI 命令 · 9 个阶段 skills · 产物模板 · 门控规则 · hooks 实现
                    │
                    │ 只读平台文件，只写目标仓库
                    ▼
   C:\work\crm-service\        ← 任意一个你自己的项目
   ├── .spec/                  这次做了什么：状态、产物、trace
   ├── AGENTS.md               本仓库积累下来的工程规则（会越来越厚）
   ├── hooks.json              宿主 hooks 模板（init 生成的样板）
   └── .claude/settings.json   实际生效的 Claude Code 配置
```

一句话原则：

> **平台仓库管"怎么做"，目标仓库管"这次做了什么"。**

这条原则决定了两件事：平台升级不影响历史项目；目标项目的规则越用越贴合自己的代码库。

---

## 2. 三种嵌法，怎么选

| 方式 | 做法 | 推荐度 | 什么时候选它 |
|---|---|---|---|
| **CLI 安装** | `pip install -e <平台路径>`，目标项目里 `specflow init` | ★★★★★ | 默认就选这个。平台独立升级，目标项目里只多出运行资产 |
| **Git submodule** | 目标项目加 `tools/specflow` 子模块，再装它 | ★★★☆☆ | 团队要**锁定**平台版本，或 CI 里要复现同一版本 |
| **整包复制** | 把 `specflow/` 包拷进目标项目，走 `PYTHONPATH` | ★★☆☆☆ | 完全离线环境、或目标项目禁止装外部包 |

不推荐把整个平台仓库复制进目标项目。后果很具体：`tests/`、`fixtures/`、`.demo/` 会和业务代码混在一个目录树里，`git log` 被平台提交刷屏；等你想升级平台时，两边代码已经各自改过，合并基本靠手工挑。

---

## 3. 例子：crm-service 的标签导入需求

目标项目长这样：

```text
C:\work\crm-service\
├── src\crm\customer_tags.py
├── tests\test_customer_tags.py
└── docs\prd\customer-tags.md     ← 需求文档，先手写
```

需求本身很小，但足够跑完整个流程：**把运营下发的客户标签 CSV 导入系统，要求按 `customer_id` 去重、跳过空行、缺表头时报错。**

选这个小需求是有意的——它有三个"看起来简单、写起来容易漏"的点（去重、空行、异常），正好能体现 TDD 循环和门控的价值。

平台路径（下文用 `<平台>` 指代）：

```text
C:\Users\whs\Desktop\storymaker\projects\p2-ai-native-platform
```

---

## 4. 接入四步

### 第 1 步：把平台装成全局 CLI（一次）

在平台目录执行 `pip install -e ".[dev]"`，然后验证 `specflow --help` 和 `specflow doctor` 都能跑。

`specflow` 这个命令来自 `pyproject.toml` 里的一行声明（`[project.scripts]` → `specflow.cli:entrypoint`），所以它本质上就是"用当前 Python 解释器跑平台的 CLI 入口"。

> **最容易踩的坑**：Claude Code 启动的 shell 不一定继承你装包的那个环境。如果目标项目有自己的 venv，请在**同一个 venv** 里装平台，否则 hooks 会报"command not found"。

### 第 2 步：在目标项目初始化（每个项目一次）

```powershell
cd C:\work\crm-service
specflow init
```

`init` 干四件事，都是幂等的（已存在就不覆盖，加 `--force` 才重写）：

1. 建 `.spec/`（`state.json` + `artifacts/`、`traces/`、`locks/`、`backup/`、`index/` 五个目录）；
2. 把平台的 `hooks.json` 复制到项目根，当模板用；
3. 写一份 `AGENTS.md` v0.1（骨架规则，后续会被阶段产物逐步加厚）；
4. 往 `.gitignore` 追加运行期垃圾：`.spec/traces/`、`.spec/locks/`、`.spec/cache/`、`.env`、`.demo/`、`__pycache__/`。

**哪些该提交**：`.spec/state.json`、`.spec/artifacts/`、`AGENTS.md`、`hooks.json`、`.claude/settings.json`。
**哪些不该提交**：`.spec/traces/`、`.spec/locks/`、`.spec/cache/`、`.env`——`init` 已经帮你写进 `.gitignore` 了。

判断标准很简单：**产物和规则是团队资产，轨迹和锁是运行时垃圾。**

### 第 3 步：接 Claude Code（关键一步）

`specflow init` 在项目根生成的 `hooks.json` **只是模板**，Claude Code 不会读它。真正生效的是：

```text
<目标项目>\.claude\settings.json
```

所以要合并进去。如果这个文件里已经有 `permissions` 之类的配置，**只加 `hooks` 字段**，别整个覆盖——这一步弄坏别人的配置最常见。

四个事件各自分工，可以理解成公司里的四个岗位：

| 事件 | 时机 | 干什么 | 通俗说法 |
|---|---|---|---|
| `SessionStart` | 会话开始/恢复/compact 后 | 注入 AGENTS.md 的规则（≤20 条）、模板索引、当前阶段与门控状态 | **入职发手册** |
| `UserPromptSubmit` | 用户每敲一句 | 解析开头的 `/命令`，检查门控；不通过直接 `exit 2` | **门口放行** |
| `PreToolUse` | 工具执行前 | 拦 `rm -rf`、`git reset --hard`、`sudo` 这类命令，给出替代方案 | **危险操作劝退** |
| `PostToolUse` | 工具执行后 | 登记新产物、刷新索引 | **归档登记** |

完整的可复制配置在 [附录 A](#附录-a最小-claudesettingsjson)，需要时直接贴。

合并完一定要**先手工验证协议通不通**，再去开 Claude Code——省得把"配置没生效"和"逻辑不对"两件事混在一起排查：

```powershell
# 在目标项目目录里执行，能输出一行 JSON（或空）且退出码 0 就算通
'{"session_id":"t","cwd":"C:/work/crm-service","hook_event_name":"SessionStart","source":"startup"}' | specflow hook SessionStart
```

> **Windows 的 PATH 问题**：如果上面这条能跑、但 Claude Code 里没反应，就是 shell 找不到 `specflow`。用 `(Get-Command specflow).Source` 拿到绝对路径（形如 `C:\...\Scripts\specflow.exe`），把配置里的 `command` 换成它，或者改用 `python -m specflow hook <Event>`（要求两边 Python 环境一致）。

### 第 4 步：加一个 `CLAUDE.md` 做桥接

这一步最容易漏，但漏了整套东西就废了一半。

**原因**：hooks 只负责门控、注入、拦截，**它不会替 Claude 执行阶段**。用户敲 `/prd-input docs/prd/customer-tags.md`，如果没人告诉 Claude 该怎么办，它会直接开始"实现需求"——跳过 PRD 结构化、跳过设计、跳过任务拆解，因为它并不知道 `/prd-input` 是个 CLI 命令。

所以在目标项目根加一份 `CLAUDE.md`：

```markdown
@AGENTS.md

## specflow 执行约定

当用户输入以 `/` 开头的 specflow 阶段命令时：

1. 原样通过 Bash 执行：`specflow run "<用户输入的完整命令>"`；
2. 不要自己重新实现阶段逻辑；
3. 命令被 hook 以 exit 2 阻断时，把 stderr 的原因转述给用户，不要绕过。
```

`@AGENTS.md` 是 Claude Code 支持的导入语法，这样它会自动读到平台生成的仓库规则。三句话解决三个问题：命令怎么执行、不要自作聪明、被拦住时不许翻墙。

> 如果你们用的 Claude Code 版本支持自定义 slash command，也可以把 `/prd-input` 等封装成 `.claude/commands/*.md`；但 hooks + `CLAUDE.md` 的组合已经够用了。

---

## 5. 走一遍完整流程

以下命令都在 `C:\work\crm-service` 下执行。阶段号是平台内部的 `S0A ~ S7`。

### 5.1 写需求 → 建上下文 → 结构化

```powershell
specflow run "/project-init"      # S0A 扫仓库：语言、测试命令、入口、模板
specflow run "/domain-input"      # S0B 业务域规则（术语、边界、不能碰的东西）
specflow run "/prd-input docs/prd/customer-tags.md"   # S1
```

`/project-init` 的价值在于**让后续阶段知道这个仓库怎么跑测试**。比如它扫出来 `pytest -q tests/`，TDD 阶段的 Executor 才知道该执行什么命令，而不是瞎猜。

`/prd-input` 会把你的大白话需求变成带编号的结构化条目，后面所有阶段都靠这些编号回溯：

| 编号 | 需求 | 验收标准 |
|---|---|---|
| REQ-01 | CSV 解析为 CustomerTag 列表 | 合法 CSV 逐行对应 |
| REQ-02 | 按 `customer_id` 去重、跳过空行 | 重复只保留首次；空行不产生记录 |
| REQ-03 | 缺表头/空文件抛 ValueError | 空文件、表头不符都报错 |

这一步是整条链路的"防漂移锚点"：如果模型在后面某一轮把"跳过空行"理解成"跳过全空列"，你能立刻对着 REQ-02 指出来。

### 5.2 探索 → 设计 → **人工门**

```powershell
specflow run "/explore"       # S2  看代码现状、可复用点、风险
specflow run "/tech-design"   # S3  方案对比 + 改动面 + 回滚预案
specflow gate approve design --as 你的名字 --note "方案通过"
```

设计写完不会自动往下走——`design` 是一道**必须由人开的闸**。开闸之前硬推任务拆解会直接被拦：

```powershell
specflow run "/plan-task"
# exit 2：design 尚未 approve（当前 pending）
```

这不是多余的仪式。设计错了，后面 TDD 写得再漂亮也是白费力气，所以平台故意把这个点设成"人说了才算"。

### 5.3 拆任务

```powershell
specflow run "/plan-task"     # S4
```

产物是 `tasks.md`，每条任务都必须写清**改哪些文件**、**是否测试先行**、**验收点是什么**：

| 任务 | 文件范围 | 测试先行 | 验收点 |
|---|---|---|---|
| T1 | `src/crm/customer_tags.py`、`tests/test_customer_tags.py` | 是 | pytest 全绿；重复 id 只留首次；空行跳过；缺表头/空文件 ValueError |

「文件范围」不只是文档里的一句话——`PreToolUse` 会拿它当白名单：T1 之外的文件你写不进去，Claude 想"顺手优化一下周边代码"会被挡回来。这是防止 AI 无限扩张改动面的关键机制。

顺带还会生成 `FEAT-xxxx` 需求号，用于串起后续所有产物；希望每个需求一个独立分支的话，设 `SPECFLOW_GIT_BRANCH=1` 即可。

### 5.4 TDD 四角色循环

```powershell
specflow run "/add --task T1"   # S5
```

内部是四个角色轮流上场，Verifier 的失败项会回流给 Planner：

```text
Planner（这轮怎么改） → Test Writer（先写测试） → Executor（写实现） → Verifier（跑测试）
                              ↑                                        │
                              └──────────── failure_items ─────────────┘
```

真实跑起来大概是这样（示意，具体轮次取决于模型）：

| 轮次 | 角色 | 结果 |
|---|---|---|
| 1 | Verifier | 3/5 失败：漏了去重、没跳空行 |
| 2 | Planner | 针对失败重规划；Test Writer 把用例扩到 7 条 |
| 2 | Verifier | 7/7 通过 |

**"先写测试"不是建议，是流程本身**：Executor 开工前测试文件已经存在，所以"实现完了再补测试"这种糊弄法在这里不成立。轮次超限时 `needs_human` 置真，停下来等人，而不是无限重试烧 token。

产物三件套：`tdd-plan.md`（每轮计划）、`tdd-report.jsonl`（逐事件，Web 控制台 Agent 面板的数据源）、`tdd-report.md`（汇总 verdict）。

### 5.5 QA 三段证据 → 归档 → 规则回灌

```powershell
specflow run "/qa-validate"   # S6
specflow run "/archive"       # S7
specflow gate approve agents-md --as 你的名字 --note "同意回灌"
```

QA 要的不是"我觉得没问题"，而是三条可查的证据：

| 证据 | 来源 | 说明 |
|---|---|---|
| 单测 | 目标仓库 pytest | 真的跑过，不是声称跑过 |
| pipeline | 平台 `pipeline_trigger/status` | CI 状态（mock 或 HTTP） |
| rpc | 平台 `rpc_call` | Staging 接口契约 |

任一条 `fail`，`/archive` 就会被门控拦住（exit 2）——**不能带病归档**。

最后一步是这套系统最有复利的地方：`/archive` 会从这次需求里提炼候选规则（比如「CSV 导入按业务唯一键去重，保留首次出现在前」），你批准后写进 `AGENTS.md`。**下个需求开工时，SessionStart 会把这条规则自动注入给 Claude**——同一个坑不用踩第二次，这是人工写文档几乎做不到的。

```powershell
specflow status --check
# 阶段：S7
# 产物：prd / exploration / design / tasks / tdd-plan / tdd-report / qa-report / archive-record
# 门控：design=approved, agents-md=approved
```

---

## 6. 接上之后，会话里实际长什么样

同样是敲 `/plan-task`，有 hooks 和没 hooks 的区别就看这一段：

```text
$ cd C:\work\crm-service && claude

## 基线规则（AGENTS.md v0.2，节选）      ← SessionStart 注入
- R-01 CSV 导入按 customer_id 去重，保留首次
## 当前阶段：S4 /plan-task｜design=approved

> /plan-task
[UserPromptSubmit] 门控通过，注入上游摘要
[Claude] Bash: specflow run "/plan-task"
[PostToolUse] 产物已登记：tasks.md

> （若 design 还没批准）
[UserPromptSubmit] exit 2：design 尚未 approve（当前 pending）
# 这条 prompt 不会进入模型，Claude 只能看到阻断原因，绕不过去
```

**最关键的是最后三行**：`exit 2` 发生在 prompt 进入模型之前，所以 Claude 连"要不要绕过"这个念头都不会产生。这比在提示词里写"请不要跳过设计阶段"可靠得多——**约束写在流程里，而不是写在道德劝说里。**

---

## 7. 边界与坑

1. **非 Python 项目只能覆盖前半段**
   `/project-init` 到 `/plan-task` 与语言无关；但 `/add` 的 Verifier 和 QA 单测目前固定跑 pytest。Java/Node 项目要跑完整 TDD 闭环，得先加语言 adapter，否则你要么改断言逻辑，要么只能手工接管测试环节。

2. **务必记住 `hooks.json` 是模板**
   Claude Code 读的是 `.claude/settings.json`。不同 Claude Code 版本的 hooks 字段可能略有差异，以平台 `hooks.json` 为基线测试；升级 Claude Code 后如果 hook 不触发，先怀疑这里。

3. **hooks 是可选增强，不是前置条件**
   没有 Claude Code 也能用：`specflow run "/tech-design"` 走同一套门控、产物、状态机。差别只是少了自动注入（Claude 得自己去读 `AGENTS.md`）和自动阻断（拦不拦得住全看模型自觉）。

4. **一个项目一份 `.spec`**
   Monorepo 多服务只有两种做法：根目录一份，或每个服务独立 `specflow init`。平台没有跨项目注册表，也不会自动发现子项目。

5. **密钥别进 git**
   `LLM_API_KEY` 放目标项目 `.env`（`init` 已忽略）或系统环境变量。别写进 `.claude/settings.json`——那玩意儿是要提交的。

6. **mock 和 live 差别很大**
   `LLM_MODE=mock` 时阶段输出是**确定性样例**，完全不读你的 PRD 内容。所以第一次跑觉得"怎么答非所问"，八成是还在 mock 模式。要真实效果就在 `.env` 里配 `LLM_MODE=live` + `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`。

7. **企业集成目前是适配层**
   docs-shuttle、pipeline、RPC、SSO 现在都是 Mock + HTTP 适配层 + 本地 stub。真要对接内部网关，需要自己配 endpoint 和 token。

8. **其他宿主同理**
   `hooks.json` 是 Claude Code 风格的事件契约。Cursor、Codex 之类如果支持等价的事件钩子，可以按同一协议对接；不支持的话，就退化成"只用 CLI"，行为见第 3 条。

---

## 8. 十分钟验收清单

```powershell
# 平台侧（一次）
cd <平台>; pip install -e .; specflow --help

# 目标项目侧
cd C:\work\crm-service
specflow init
specflow run "/project-init"
specflow status

# hook 协议
'{"session_id":"t","cwd":"C:/work/crm-service","hook_event_name":"SessionStart","source":"startup"}' | specflow hook SessionStart
```

逐项打勾：

- [ ] `specflow --help` 有输出，`specflow doctor` 没报环境错误
- [ ] 目标项目里出现 `.spec/`、`AGENTS.md`、`hooks.json`
- [ ] `.claude/settings.json` 里能看到四个 hook 事件
- [ ] 启动 Claude Code 时，第一屏能看到注入的规则和当前阶段
- [ ] 门控未通过时敲 `/plan-task`，prompt 被拦下并给出原因（exit 2）
- [ ] `/add --task T1` 之后，`.spec/artifacts/tdd-report.jsonl` 里有四个角色的事件
- [ ] `gate approve agents-md` 之后，`AGENTS.md` 多出新规则

---

## 附录 A：最小 `.claude/settings.json`

放在 `<目标项目>\.claude\settings.json`。已有配置就只把 `hooks` 这个键并进去。

```json
{
  "hooks": {
    "SessionStart": [
      { "matcher": "startup|resume|clear|compact",
        "hooks": [{ "type": "command", "command": "specflow hook SessionStart", "timeout": 10 }] }
    ],
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command", "command": "specflow hook UserPromptSubmit", "timeout": 10 }] }
    ],
    "PreToolUse": [
      { "matcher": "Bash|Shell|Write|Edit|MultiEdit",
        "hooks": [{ "type": "command", "command": "specflow hook PreToolUse", "timeout": 5 }] }
    ],
    "PostToolUse": [
      { "matcher": "Write|Edit|MultiEdit|Bash",
        "hooks": [{ "type": "command", "command": "specflow hook PostToolUse", "timeout": 5 }] }
    ]
  }
}
```

`specflow` 不在 PATH 时，把 `command` 换成绝对路径或 `python -m specflow hook <Event>`。

## 附录 B：PRD 输入模板

`/prd-input` 接受任意 Markdown/文本，但包含这四节时，模型生成的 `prd.md` 明显更稳定：

```markdown
# 需求：<一句话>

## 背景
为什么做；现在怎么做；痛点是什么。

## 需求
1. ...
2. ...

## 验收标准
- ...

## 范围外
- ...
```

「范围外」这一节别省——它是防止需求在后面几轮里悄悄膨胀的唯一护栏。
