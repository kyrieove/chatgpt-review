# Antigravity 单端自动协作首版实施文档 (2026-10-15)

## 1. 架构与实施目标

实现 Antigravity 单端自动协作流程，让用户直接在 Antigravity 提需求，真实已登录 ChatGPT（如 GPT-4o、o1）负责方案规划与审查验收，Antigravity 本地实施代码、运行真实测试并记录证据，全程无需人工复制粘贴。

### 核心设计原则
1. **真实边界与零盲从**：
   - Antigravity 本地编写代码、执行真实测试；
   - `workflow.py` 仅负责状态追踪与通信管理，**严禁自动执行网页返回的任意 shell 命令**；
   - 审查意见为咨询性质，不赋予任何外部发布（push、PR）权限。
2. **状态机与证据闭环**：
   - 角色推进顺序：`WAITING_PLAN`（待计划） ➔ `WAITING_EXECUTION`（待执行） ➔ `WAITING_REVIEW`（待审查） ➔ `WAITING_FIX_OR_DIAGNOSIS`（待修复或诊断） ➔ `WAITING_ACCEPTANCE_DONE`（待验收完成），及明确 `BLOCKED` 状态；
   - 推进只在证据与身份核验后发生。`record-work` 必须提供当前 Git 真实存在的 40 位 commit SHA 与非空测试证据。
3. **已登录 Chrome 自动化往返 (BrowserRelay)**：
   - 借用受控 ChatGPT 标签页，严格校验 HTTPS origin（`https://chatgpt.com`）与精确 conversation ID；
   - 防 TOCTOU 重定向：借用后、注入前、点击发送前连续校验当前 URL；
   - 校验实际选定的模型（如 GPT-4o、o1）与思考模式（Thinking mode），若无法可靠核对或发现降级则立即置为 `BLOCKED_MODEL_MISMATCH`，禁止静默降级；
   - 提示词内嵌确定性 `request_id`，注入并提交后，通过 DOM 节点校验正式 user turn 存在且 SHA-256 哈希匹配后才认定已送达；
   - 完整提取 assistant turn 中的所有 markdown/prose 文本块，提取结尾的机器可读 JSON 区块。
4. **防御性异常处理**：
   - 同一发现连续两轮修复无改善自动转为诊断模式（`is_diagnosis = True`）；
   - 最大修复轮次保守限制为 5 轮，超限自动置为 `PAUSED_LIMIT_EXCEEDED` 并暂停，请求用户介入；
   - JSON 状态采用临时文件原子替换（`os.replace`），保证进程重启后状态完好；重试时校验已激活请求 ID 与内容哈希，杜绝盲目重复注入。

---

## 2. 实施文件与边界清单

本任务全部实施在隔离 worktree `D:/2/dev/review-relay/chatgpt-review-antigravity-v1` 完成，严格遵守文件白名单：
- `skills/chatgpt-review/scripts/workflow.py`: 持久任务流程 CLI 核心实现。
- `skills/chatgpt-review/scripts/browser_relay.py`: 增加模型/思考模式校验、新建聊天及与流程的集成。
- `skills/chatgpt-review/references/workflow.md`: 工作流状态机与 CLI 契约规范说明。
- `skills/chatgpt-review/references/chatgpt.md`: 增加机器可读响应 JSON 代码块规范与单端协作支持。
- `skills/chatgpt-review/references/executor.md`: 增加 Antigravity 单端工作流指令与多 Agent 备用路径。
- `skills/chatgpt-review/references/browser.md`: 增加自动化往返与模型核验说明。
- `skills/chatgpt-review/SKILL.md`: 更新本地工具速查，引导默认走 workflow CLI。
- `README.md` 与 `PROMPTS.md`: 增加单端流程说明与提示词模板。
- `CHATGPT_START.md`: 通过 `python build_bootstrap.py` 重新生成以保持严格一致。
- `tests/test_workflow.py`: 13 项全量工作流状态机与异常防御自动化测试。
- `tests/test_browser_relay.py`: 增加 4 项真实 Node DOM 模型/思考模式与浏览器交互测试。
- `docs/2026-10-15-antigravity-v1.md`: 本设计与实施报告。

---

## 3. 准备 Live 独立验收材料

由于本轮会话受隔离原则与安全基线约束，**禁止向真实聊天或其他外部目标发送未经验收的实时消息**。
因此真实 live 往返验收单独标为 `NOT RUN`，由协调端 Codex 按下列脱敏步骤与已绑定的具体 Git 版本独立执行验证：

### Live 验收步骤与脱敏材料

1. **环境核验**：
   ```bash
   bsk status --json
   ```
   确认 Chrome 实例已连接，并打开 ChatGPT 标签页（推荐选择 GPT-4o 或 o1 模式）。

2. **第 1 轮：启动任务并获取实施计划 (Live Plan)**：
   - 目标项目路径：`D:/2/dev/review-relay/chatgpt-review-antigravity-v1`
   - 执行命令：
     ```bash
     python skills/chatgpt-review/scripts/workflow.py start --project . --title "Live Verification Task" --model gpt-4o
     python skills/chatgpt-review/scripts/workflow.py exchange --project . --chat-url "https://chatgpt.com/c/<your-live-conversation-id>"
     ```
   - 注入内容（已由 exchange 自动组装）：
     包含当前 Git HEAD SHA、任务目标与要求返回 `PLAN_READY` 的机器可读 JSON 区块。
   - 预期结果：
     ChatGPT 返回计划，末尾附带 `decision: "PLAN_READY"`；工作流推进至 `WAITING_EXECUTION`。

3. **第 2 轮：本地提交与审查复核 (Live Review)**：
   - 本地微调改动并 commit（例如在 docs 中添加 live 标识），运行单元测试。
   - 执行命令：
     ```bash
     python skills/chatgpt-review/scripts/workflow.py record-work --project . --test-evidence "Ran tests: 80 PASS"
     python skills/chatgpt-review/scripts/workflow.py exchange --project .
     ```
   - 预期结果：
     ChatGPT 针对新 commit SHA 审查，返回 `decision: "ACCEPTED"` 或 `"CHANGES_REQUESTED"`。若返回 `ACCEPTED`，流程推进至 `WAITING_ACCEPTANCE_DONE`。

4. **第 3 轮：修复复审或换聊天续接 (Live Resume)**：
   - 打开新的 ChatGPT 会话，执行：
     ```bash
     python skills/chatgpt-review/scripts/workflow.py resume --project . --chat-url "https://chatgpt.com/c/<new-conversation-id>"
     ```
   - 预期结果：
     自动重构前序会话的任务 ID、SHA 与历史决策摘要，在新会话无缝续接。
