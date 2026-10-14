---
name: chatgpt-review
description: Coordinate ChatGPT and external agents through GitHub review with fixed commit identities, user-approved pushes and one Draft PR per task. Use for delegated implementation, evidence-based review, fix rounds and cross-chat continuation. Self-contained upload ZIPs are the fallback when GitHub content cannot be read.
---

# chatgpt-review

让 ChatGPT 协调目标、执行任务、审查与验收，让具备本地能力的 Agent 实现并提供证据。默认由用户转交任务书、反馈与版本入口。此 skill 是协作协议，不提供聊天平台连接，也不赋予额外操作授权。

v2 默认 `GitHub Review`：ChatGPT 用已连接的 GitHub 读取指定版本，执行端先在本地功能分支工作，测试与凭据扫描完成后，取得对应用户明确许可再推送和创建或更新同一个 Draft PR。工具或内容不可读时自动准备安全 ZIP 备用，不自动执行远端写入。

## 选择当前角色

根据用户请求与当前可用能力确定角色，勿把模型名称当作能力证明。

- **ChatGPT 协调与审查端**：可检查实际返回的 GitHub 内容及当前聊天可读上传材料，始终不能假定可以访问本地项目。读取 [ChatGPT 流程](references/chatgpt.md)。可从需求直接生成首轮任务书。
- **执行端**：在用户授权的项目中查看文件、实现、测试、保存记录与打包。读取 [执行端流程](references/executor.md)。Codex、Claude Code、Antigravity 或其他 Agent 均可承担此角色。
- **续接任一端**：先读取当前输入中的任务状态与身份，按对应角色的续接流程核对后继续。

执行端维护的任务目录是持久记录。ChatGPT 的结论只属于已读 GitHub commit 或上传快照，不是未推送的本地项目。新决策与反馈须由执行端保存，获得对应许可后进入同一 PR 或下一备用包。

## 交换契约

GitHub 模式先读取 [GitHub Review 流程](references/github.md)，固定任务、功能分支和同一个 Draft PR，每轮绑定完整 commit SHA。所有任务维护 HANDOFF、REVIEW_REPORT、TEST_RESULTS，并保留 TASK_BRIEF 和 CHATGPT_BRIEF 支持独立续接。

创建、审查或修复备用 ZIP 前读取 [文件与版本契约](references/contract.md)。每轮有固定 `task_id`、递增 `round` 和唯一 `package_id`。每个包独立包含五份任务记录、相关完整文件、测试证据和使用说明。

长期任务按当前工作项选择完整相关文件与本轮证据，历史留在本地并写成便携摘要。压缩后大小超预算则停止打包，明确重新收集范围，避免静默漏文件。具体规则见交换契约的“控制每轮大小”。

ChatGPT 输出可直接转交的任务书或反馈；执行端回传可上传的 ZIP。没有传输工具时，输出材料和转交动作。只有真实工具可用且用户明确授权时才发送；执行端反馈不能替代用户授权。

## 完成与停止

- 未运行的检查标为 `NOT RUN`，环境限制标为 `BLOCKED`。区分 ChatGPT 本轮直接检查、执行端报告和推断。
- 缺材料时生成具体收集任务；基于旧包的反馈先核对当前代码，禁止直接覆盖新改动。
- 同一问题连续两轮未改善时，下一轮改为诊断任务，记录被否定的假设与缺少的证据。
- 所有验收项有证据、范围内缺陷已处理、剩余风险得到明确处置后才建议 `DONE`。打包、测试通过或未发现问题各自都不等于任务完成。
- 推送、创建或更新 PR 必须已有覆盖对应操作和内容的明确用户许可，否则完成本地准备后等待。审查通过不构成许可。不自动合并、发布、强推、amend、删除或覆盖现有用户工作。

## 本地工具与模板

可选工具使用 Python 3.9+ 标准库，在执行端运行：

```text
python "<skill>/scripts/relay.py" start --project "<project>" --title "<task title>"
python "<skill>/scripts/relay.py" pack --project "<project>" --round 1
```

首轮包含新建任务目录，不自动填写需求、执行测试、推送、创建 PR 或发送 ZIP。此 CLI 只初始化本地记录和打包，GitHub 流程由当前执行 Agent 的真实工具承担。调用前读取执行端流程。没有 Python 时，用可用工具实现相同记录与备用包契约。

模板按需使用：[任务书](templates/TASK_BRIEF.template.md)、[持久状态](templates/HANDOFF.template.md)、[变更与审查报告](templates/REVIEW_REPORT.template.md)、[测试结果](templates/TEST_RESULTS.template.md)、[ChatGPT 续接摘要](templates/CHATGPT_BRIEF.template.md)、[审查反馈](templates/REVIEW_FEEDBACK.template.md)。
