# 持久任务工作流 (Workflow)

本文档说明 Antigravity 单端自动协作流程的持久状态机、`workflow.py` CLI 及契约。

## 设计目标与原则

1. **单端入口**：用户在 Antigravity 提需求，真实 ChatGPT 负责规划、审查与验收；Antigravity 执行代码实现、运行真实测试并记录证据，无需人工复制粘贴。
2. **证据闭环**：传输成功不等于业务完成。状态推进只基于本地真实 Git 提交 SHA、测试证据与 ChatGPT 机器可读核验。
3. **安全边界**：CLI 只管理状态与通信，禁止自动执行网页返回的任意命令。不改变用户授权，无外部静默推送。
4. **模型核对**：保存并核对实际选定的模型与思考模式（如 GPT-4o、o1），无法可靠核对则 BLOCKED，不得静默降级。
5. **防御性推进**：两轮修复无改善自动转诊断；最大 5 轮修复限制，超限暂停请用户决策。

---

## 状态机推进顺序

状态推进严格按角色生命周期流转：

```text
[start]
   │
   ▼
WAITING_PLAN (待计划)
   │  exchange (发送计划请求，绑定输入基线 SHA)
   ▼
WAITING_EXECUTION (待执行) ──[ChatGPT 返回 PLAN_READY]
   │
   │  Antigravity 本地实现、运行测试
   │  record-work (提交真实 commit SHA，附加测试结果证据)
   ▼
WAITING_REVIEW (待审查)
   │  exchange (发送审查请求，绑定 reviewed_head_sha 与测试证据)
   ▼
   ├─► [CHANGES_REQUESTED] ──► WAITING_FIX_OR_DIAGNOSIS (待修复或诊断)
   │                             │ (同问题连续 2 轮未改善 ──► 诊断模式)
   │                             │ (修复轮次 >= 5 ──► PAUSED_LIMIT_EXCEEDED)
   │                             ▼
   │                         Antigravity 修复/诊断并测试 ──► record-work ──► WAITING_REVIEW
   │
   ├─► [ACCEPTED] ───────────► WAITING_ACCEPTANCE_DONE (待验收完成)
   │
   └─► [NEEDS_INPUT / BLOCKED / 格式异常 / 版本不匹配] ──► BLOCKED (阻塞暂停)
```

---

## 机器可读响应契约

ChatGPT 的所有计划与审查回复末尾必须包含严格符合下述结构的机器可读 JSON 代码块：

````markdown
```json:chatgpt-review
{
  "task_id": "20261015-120000-task",
  "round": 1,
  "reviewed_head_sha": "4ddaf599ee95c473d0502c154ae78da05a6351dc",
  "decision": "PLAN_READY",
  "findings": [],
  "next_action": "在本地功能分支实施改动并运行测试"
}
```
````

### 字段说明与规则
- `task_id`: 必须与当前任务 ID 严格一致。
- `round`: 必须与当前轮次整数严格一致。
- `reviewed_head_sha`: 必须与当前被审查的 40 位 Git HEAD SHA 严格一致（计划轮绑定输入基线 SHA）。
- `decision`: 严格限定为 `PLAN_READY`、`CHANGES_REQUESTED`、`ACCEPTED`、`NEEDS_INPUT`、`BLOCKED` 之一。
- `findings`: 发现项数组，包含 `id` (如 `F-001`)、`description` 等。
- `next_action`: 具体的下一动作描述。

**防作弊与防推测规则**：
- 缺字段、错版本、错轮次、歧义或非 JSON 格式均判定为 `BLOCKED`，原文完整保存至 `logs/`，严禁依据正文中出现“通过”等文字假定验收。

---

## CLI 命令速查

```bash
# 1. 启动任务（自动建立 handoffs 记录、记录当前 git HEAD、初始化 WAITING_PLAN）
python skills/chatgpt-review/scripts/workflow.py start --project . --title "任务标题" --model gpt-4o

# 2. 查看当前状态（stage、round、commit SHA、下一动作）
python skills/chatgpt-review/scripts/workflow.py status --project .

# 3. 往返交互（自动准备提示词、校验模型、借用标签页发送、等待并校验机器可读回复）
python skills/chatgpt-review/scripts/workflow.py exchange --project . --chat-url "https://chatgpt.com/c/xxx"

# 4. 记录工作成果（校验 commit SHA 真实存在，核实非空测试证据，推进至 WAITING_REVIEW）
python skills/chatgpt-review/scripts/workflow.py record-work --project . --head-sha <sha> --test-evidence "测试证据内容或结果文件路径"

# 5. 换聊天续接与重启恢复（跨会话重构上下文摘要，保留旧会话身份追踪）
python skills/chatgpt-review/scripts/workflow.py resume --project . --chat-url "https://chatgpt.com/c/yyy"

# 6. 超限人工裁决恢复（超限暂停 PAUSED_LIMIT_EXCEEDED 后，由用户明确裁决恢复或终止）
python skills/chatgpt-review/scripts/workflow.py resolve --project . --decision CONTINUE --notes "用户授权继续修复"
```

---

## 异常与诊断保护

1. **同问题两轮无改善转诊断**：
   当初次提出发现后，两次实际修复复审仍存在该发现 ID 时，状态自动置为 `is_diagnosis = True`，下一轮进入最小假设排查与实验模式，不再机械盲改。
2. **超限暂停与人工裁决**：
   默认累计最多 5 轮修复。达到上限后状态置为 `PAUSED_LIMIT_EXCEEDED`，严格锁定 `record-work` 与 `exchange`，只能通过用户运行 `workflow.py resolve` 明确裁决恢复或终止。
3. **进程重启与事务幂等**：
   发送前落盘持久事务 `active_transaction`。未知送达时先核对 DOM 中原正式 user turn，确认存在才收取回复，杜绝重发与虚假 retry ID。
4. **单写者跨进程文件锁**：
   通过 `TaskLock`（Windows `msvcrt` / Unix `fcntl`）提供进程级互斥锁，多进程并发操作同一任务时立即报告 busy (exit code 5)。
5. **结构化测试证据绑定**：
   `record-work` 必须提供结构化证据（包含执行命令、exit_code 0、tested_sha 及时间戳），且工作区 git status 干净无未提交修改。若提交后代码变动或 HEAD 移动，审查与验收自动失效。
