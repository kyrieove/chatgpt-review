# 任务书：chatgpt-review 自审与插件化（第二轮实施与验证）

- task_id: 20261015-chatgpt-review-self-review
- 目标仓库: kyrieove/chatgpt-review
- 输入基线: c6ce0fa3f4e65d5dadbb61bf792e7d9f8b996b22
- 本地分支: feature/chatgpt-review-self-review
- 远端 PR: Draft PR #1 (https://github.com/kyrieove/chatgpt-review/pull/1)
- 远端当前 Head SHA: a928ee26776fb82ed80c43df131234c7af2f67c8
- 状态: AWAITING_USER_REVIEW（本地实施与 E2E 验证已完成，未推送新提交、未合并 PR）

## 用户与 ChatGPT 第二轮目标

1. 修复 F-006：使五份任务记录与远端真实 PR 状态（PR #1, head `a928ee26776fb82ed80c43df131234c7af2f67c8`）一致，纠正前序汇报中的散列散乱与自引用问题。
2. 补齐跨平台验证：39 项测试全量通过（含 8 项 browser_relay 测试），明确 37 PASS 与 2 SKIP；并在隔离环境实际验证 plugin 识别与同名 Skill 激活风险。
3. 设计并实现最小 `browser-relay` 原型，优先复用现有 `bsk` 连接，完成真实端到端无敏感信息交互测试（固定 SHA、request_id 防重、响应抽取、自动归还标签页）。
4. 更新同一任务的五份记录，完整记录真实交互日志、人工介入点与脱敏证据。
5. 完成凭据检查并生成安全审查备用 ZIP，未获明确许可不向远端执行新写入。

## 验收项与状态

| ID | 检查项 | 目标 | 状态 | 证据 |
|---|---|---|---|---|
| A-001 | F-006 远端 PR 状态校准 | 五份记录完整记录 PR #1、Base `c6ce0fa...`、Head `a928ee2...`，保留历史身份 | PASS | `gh pr view 1` 与五份记录比对一致，修正前序 SHA 偏差 |
| A-002 | 完整测试套件与隔离验证 | 运行 39 项测试，明确 PASS/SKIP；隔离环境验证插件识别与同名冲突评估 | PASS | `python -B -m unittest discover -s tests -v` 39 项（37 PASS，2 SKIP）；`agy plugin validate` 隔离通过 |
| A-003 | 最小 browser-relay 原型 | 优先复用 bsk，支持 request_id 幂等、送达确认、响应完成轮询与自动归还 | PASS | `skills/chatgpt-review/scripts/browser_relay.py` 实现并通过 `tests/test_browser_relay.py` 8 项测试 |
| A-004 | 真实端到端中继测试 | 无敏感信息交互，发送固定 SHA 任务，轮询提取完整回复并保存 | PASS | `feedback/round-2-chatgpt.txt` 成功提取，标签页已调用 `tab return` 归还 |
| A-005 | 备用交付 ZIP 生成 | 重新生成安全审查 ZIP，附带完整元数据与 SHA256SUMS | PASS | `relay.py pack` 输出 review-artifacts ZIP |

## 授权边界

- 远端写入：Draft PR #1 已于首轮获授权创建并推送；本轮新增本地代码（browser-relay 及记录更新）**未经用户明确许可不得向远端推送**，严禁自动合并、强推或覆盖。
- 浏览器权限：严格遵循标签页借用确认，超时或拒绝时退回人工转交，严禁规避授权。
