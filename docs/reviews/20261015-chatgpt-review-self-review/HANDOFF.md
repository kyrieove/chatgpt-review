# 任务状态与交接：自审加固、TOCTOU 防护与打包多重防覆盖（第四轮实施与验证）

- task_id: 20261015-chatgpt-review-self-review
- round: 4
- 状态: AWAITING_USER_REVIEW
- 仓库: kyrieove/chatgpt-review
- 分支: feature/chatgpt-review-self-review
- 远端 PR: [Draft PR #1](https://github.com/kyrieove/chatgpt-review/pull/1)（状态：DRAFT）
- Base Commit SHA: `c6ce0fa3f4e65d5dadbb61bf792e7d9f8b996b22`
- last_reviewed_sha: `a928ee26776fb82ed80c43df131234c7af2f67c8`（ChatGPT 审查基线）
- last_published_sha / current_pr_head_sha: `c28330334f11f8a72b3020f8d97b5b516fe1f8c6`（远端 PR #1 当前 head）
- 本地 HEAD: `c28330334f11f8a72b3020f8d97b5b516fe1f8c6`
- 本地未提交改动:
  - `skills/chatgpt-review/scripts/browser_relay.py`（F-007 多块提取、F-008 fail-closed 与哈希核验、F-010 会话精确匹配与 TOCTOU 防护、资源清理诊断）
  - `skills/chatgpt-review/scripts/relay.py`（F-011 双文件冲突防覆盖动态名称分配）
  - `tests/test_browser_relay.py`（31 项单元测试，含 7 项 r4 针对性回归测试）
  - `tests/test_relay.py`（30 项单元测试，含 F-011 双文件冲突测试）
  - `tests/dom_fixture.js`（Node.js 原生 DOM 模拟器夹具）
  - 五份任务记录与根目录 HANDOFF.md（F-009 状态校准与第 4 轮同步）
- 授权限制: **禁止向远端进行任何写入（git push）、禁止创建新 PR、禁止转 Ready、禁止合并、未经用户明确许可禁止向真实标签页发送测试消息**。

## 本轮事实与代码证据

### 1. F-008 发送前 fail-closed 与发送后 user turn 哈希核验
- **Fail-closed 阻断**: `send_prompt()` 中，若发送前 DOM 检查返回异常状态或非 `("OK", "USER_TURN_NOT_FOUND")`，抛出 `DeliveryError("Pre-send DOM check failed closed: ...")`，杜绝在状态未知时贸然注入。
- **发送后哈希比对**: 验证 formal user turn 时，核验 DOM 中提取出的文本 SHA-256 与预期消息 SHA-256 完全一致；若哈希不符抛出 `DeliveryError`，杜绝仅验证 `exists=True`。

### 2. F-010 目标会话精确匹配与 TOCTOU 防护
- **精确会话比对**: `match_conversation_url()` 严格比对 URL path 是否精确匹配 `/c/<id>`（支持可选 query 参数与 trailing slash），阻止通过 `/c/id-extra-malicious` 伪造前缀匹配。
- **Tab ID 借用后核验**: 即使仅传入 `target_tab_id`，借用成功后也必须比对当前实际 URL 与初始目标 URL，一旦发生跳转立即抛出 `ConversationMismatchError` 并触发资源归还清理。
- **操作前防 TOCTOU 校验**: 在执行输入框内容注入前与点击发送按钮前，分别重新调用 `get_current_url()` 核验当前会话，防止在页面借用期间被异步跳转劫持。

### 3. F-007 回复完整多正文块提取
- **废弃单块截断**: `build_extract_js()` 重构为查询并提取容器内的所有正文块（`.prose, .markdown`），按文档顺序收集并使用换行拼接，彻底修复复杂回复中仅提取首个 `.prose` 丢失后续段落的问题。

### 4. F-011 双文件冲突动态防覆盖
- **动态分配非冲突文件名**: 当用户项目根目录同时存在 `CHATGPT_START.md` 与 `CHATGPT_COORDINATION.md` 时，打包工具循环尝试分配 `CHATGPT_COORDINATION_1.md`、`CHATGPT_COORDINATION_2.md` 等，绝不覆盖任一源文件。

### 5. 资源清理失败可诊断性
- **诊断保留与警示**: `return_tab()` 或 `stop_session()` 返回 `False` 或异常时，在 journal 中记录 `cleanup_status: CLEANUP_FAILED` 与详细错误日志；当业务流程成功但清理受阻时，将最终任务状态标记为 `WARNING_CLEANUP_FAILED`，不再静默掩盖。

### 6. 测试套件全量结果（63 项）
- **命令**: `python -B -m unittest discover -s tests -v`
- **结果**: **61 PASS, 2 SKIP, 0 FAIL**（耗时约 19.4s，严禁表述为 63 PASS）。
- 2 项跳过由于 Windows 目录符号链接权限限制，已由 Mock 测试替代覆盖。

## browser-relay 的能力与边界

- **能保证**: 精确 `/c/<id>` 会话匹配、Tab ID 防绕过、借用前后及输入/点击操作前 TOCTOU 跳转检测、发送前 fail-closed 阻断、发送后正文 SHA-256 哈希比对、多正文块完整提取、无可靠结构严格 BLOCKED、双文件打包多重防覆盖、资源清理状态全链路诊断追踪。
- **不能保证**: 无法绕过 ChatGPT 突发前端 Captcha 人机验证、无法在未经用户点击浏览器授权弹窗时强制借用、无法跨越 OpenAI 前端彻底抛弃 DOM 树语义结构的破坏性改动。

## 下个会话的第一条操作

`git status` 审查第四轮新增修改与测试，等待用户进一步决策。
