# chatgpt-review v2

让 ChatGPT 通过已连接的 GitHub 协调与审查任务，让任意本地 Agent 实现、测试和提供证据。默认 GitHub Review，独立 ZIP 是读取受限时的备用。

每个任务使用固定 task_id、功能分支和同一个 Draft PR。每轮绑定完整 commit SHA，保存变更、测试、审查结果和下一步。新聊天重新读取仓库记录，不依赖旧聊天记忆。

## 开始使用

1. 给执行 Agent 安装或直接读取 `skills/chatgpt-review/SKILL.md`。不同 Agent 按各自 skills 目录与语法使用，无固定 IDE、模型或 MCP 依赖。
2. 给 ChatGPT 上传 `CHATGPT_START.md`，或让它通过已连接的 GitHub 读取本仓库同名文件，描述目标与项目仓库。让它生成可以转交的任务书。
3. 把任务书转交执行端。本地分支修改、测试与凭据扫描完成后，执行端给你具体发布范围和 Draft PR 文案。已有对应明确许可才推送及创建 PR。把 PR URL 和 head SHA 交给 ChatGPT，后续继续同一 PR。

```text
按 chatgpt-review v2 担任协调与审查端。
目标：<你的需求>。项目仓库：<owner/repo>。执行 Agent：<名称>。
先给可转交任务书。默认 GitHub Review，审查绑定实际读取的完整 SHA。
执行端先完成本地分支、测试与凭据扫描，在我的对应明确许可后才推送和创建或更新同一 Draft PR。
```

skill 不建立 GitHub 连接，也不提供自动聊天传输。连接器可读内容和版本需要实际核对。ChatGPT 不能查看本地未推送文件；读不到目标 SHA、PR 或日志时补充固定版本材料或备用 ZIP。

## Antigravity Plugin 与独立 Skill

仓库根目录的 plugin.json 直接复用 skills/chatgpt-review，不复制另一套协议。可按 Antigravity 支持的方式从本地目录或 GitHub 安装；其他 Agent 继续读取原 skill 目录。当前提供最小插件包装，不加入 MCP、hooks 或额外运行依赖。

同一 Antigravity 环境已有全局同名 Skill 时，先在隔离工作区验证插件识别及重复加载情况，再决定是否将 Plugin 作为唯一激活入口。验证插件正常不自动授权停用共享 Skill 或更改全局配置。

## 可选 Chrome 协作入口

有本地终端能力的执行 Agent 可按 references/browser.md 使用现有 browser-skill 或 agent-browser。前者通过已有扩展与受控 Agent Window 使用当前 Chromium 登录环境，已有用户标签页须借用；后者默认适合独立浏览器测试。Antigravity 原生浏览器使用独立 profile，不能直接当作日常 Chrome 的登录环境。

此集成是按需流程，不是自动配置器。先验证真实 CLI、扩展、会话与页面操作，遵守宿主工具审批和用户授权，不导出 Cookie、不自动开调试端口。使用浏览器转交需明确目标 ChatGPT 聊天和发送授权；回复不会自动授权代码发布。

## 持久记录与双方续接

每个任务维护以下记录，并在 GitHub 模式下将可分享版本保存到已许可的远端路径：

| 文件 | 内容 |
|---|---|
| HANDOFF.md | 任务状态、仓库、分支、PR、已审查 SHA、决定、遗留问题与下一步 |
| REVIEW_REPORT.md | 变更摘要、发现处置、审查范围与限制 |
| TEST_RESULTS.md | 实际命令、输出、退出码、环境、被测源码版本与凭据扫描 |
| TASK_BRIEF.md | 用户目标、验收、范围、许可来源 |
| CHATGPT_BRIEF.md | 新 ChatGPT 会话入口、未决发现与待转交指令 |

新 ChatGPT 聊天提供仓库、task_id、PR URL、期待 head SHA 和记录路径。它重新读取当前版本，不要求重新开 PR。尚未进入仓库的最新反馈要另外提供。

执行 Agent 换会话则先读同任务记录，再核对本地与远端 SHA、未推送改动和待批准内容。不能用旧审查意见覆盖新代码，不能把对旧 SHA 的通过当作新版本通过。

## 推送许可与测试版本

推送、创建 PR、更新远端文案需要已有覆盖相应操作和内容的明确用户许可。许可可以覆盖一次具体发布，或用户明确限定的多轮操作；若要求逐轮批准则逐轮询问。对已经授权且未变的操作不重复询问。没有许可时停在本地准备完毕，必要时先用 ZIP 审查。

不自动合并、强推、amend、转为 ready 或覆盖用户工作。普通开发请求和 ChatGPT 审查通过不会自动授权远端写入。用户项目的公开与私有设置由用户决定，安装公开 skill 不会把项目变公开。

测试绑定真实被测源码 SHA 或文件哈希清单。后来仅更新任务记录时，核对源码、测试、依赖和锁文件、配置、脚本与资源均未改变，才可保留证据适用性。CI 使用实际运行 SHA。本轮当前 head 从 PR/API 读取，不要求 commit 中的文件保存自身最终 SHA。

## 备用 ZIP 与体积控制

可选 CLI 只需要 Python 3.9+ 标准库，负责本地初始化和打包，不会推送或创建 PR：

```text
python "<skill>/scripts/relay.py" start --project "<project>" --title "<title>"
python "<skill>/scripts/relay.py" pack --project "<project>" --round 1
```

初始化后 Agent 补全五份记录。ZIP 自带 ChatGPT 说明、GitHub 流程、任务状态、选定完整源码和哈希，换聊天可以独立恢复。没有 Python 时使用 Agent 的可用工具按同一契约完成。

已确定的新任务身份可传 start --task-id <id>。已有同 ID 任务直接续接，工具拒绝覆盖。显式 --include 单个文件超限、不可读或被过滤会失败，目录内的安全过滤仍以元数据报告。

每轮用 `--include` 选择完整相关源码、调用方、测试、依赖和资源；用 `--evidence` 选择本轮日志与必要未决反馈，五份记录始终加入。旧已解决历史写入事实摘要，原始历史保留本地。未指定 evidence 时保留当前任务的全部可分享记录。

```text
python "<skill>/scripts/relay.py" pack --project "<project>" --round 3 --include src --include tests --include package.json --evidence logs/round-3 --max-package-mb 25
```

示例路径须替换为实际存在的路径。压缩后默认硬预算为 25 MiB，这是工具预算，不是平台上传限制。超限拒绝生成新最终 ZIP，报告体积与大文件，不静默删材料。相关范围仍超预算时安排带统一身份和索引的分卷上传，工具不自动分卷。

Git 状态与 diff 同样只包含本包允许分享的范围，不会带入相邻项目或被排除的环境变量文件、数据库内容。过滤后的空状态不能证明整个仓库没有改动。分享前仍检查源码、日志、截图及拟推送的新增 commit 历史。

pack 的凭据扫描只覆盖本次 ZIP 暂存内容，不能替代推送前的完整新增 commit 范围检查。无法确认待推送历史及检查结果时保持本地待审查状态。

## 维护与验证

```text
python build_bootstrap.py
python -B -m unittest discover -s tests -v
```

第一条从 chatgpt、github、contract 三份权威参考生成单文件入口。第二条在临时项目验证 CLI，不修改用户项目。

本仓库发布 skill 源码，不制作 skill 发行 ZIP。其忽略规则排除本地开发任务和审查包；在用户项目启用 GitHub Review 时需选择允许跟踪的脱敏任务记录路径。角色流程见 `skills/chatgpt-review/references/`，可复制指令见 `PROMPTS.md`。

chatgpt-review 源于 review-relay 的协议重构，新版独立使用。GitHub 工作方式参考 [GitHub PR quickstart](https://docs.github.com/en/pull-requests/get-started/pull-request-quickstart)。
