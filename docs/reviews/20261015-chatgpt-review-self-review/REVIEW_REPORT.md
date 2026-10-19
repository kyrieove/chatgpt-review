# 自审与可靠性加固报告（F-001 ~ F-011 全量闭环与第四轮加固）

- task_id: 20261015-chatgpt-review-self-review
- round: 4
- base: `c6ce0fa3f4e65d5dadbb61bf792e7d9f8b996b22`
- 远端 PR: [Draft PR #1](https://github.com/kyrieove/chatgpt-review/pull/1)（状态：DRAFT）
- last_reviewed_sha: `a928ee26776fb82ed80c43df131234c7af2f67c8`
- last_published_sha / current_pr_head_sha: `c28330334f11f8a72b3020f8d97b5b516fe1f8c6`
- 本地 HEAD: `c28330334f11f8a72b3020f8d97b5b516fe1f8c6`
- 本地状态: 包含第四轮未提交加固与回归测试（F-007、F-008、F-010、F-011、清理诊断），本地 63 项测试全量通过（61 PASS, 2 SKIP, 0 FAIL），禁止未经许可的远端写入。

## 审查发现项闭环状态 (F-001 ~ F-011 与 r4 加固)

| 编号 | 优先级 | 审查发现 | 处理结论 | 代码定位 / 证据 |
|---|---|---|---|---|
| **F-001** | P2 | start 无法指定确定 task_id | **ACCEPTED & FIXED** | 已合并至 PR #1 (`a928ee2...`)；`relay.py` 支持 `--task-id` |
| **F-002** | P2 | 显式 `--include` 单文件超限/过滤静默跳过 | **ACCEPTED & FIXED** | 已合并至 PR #1 (`a928ee2...`)；`relay.py` 单文件失败立即阻断 |
| **F-003** | P2 | 缺少 plugin.json 且需避免重复 skill | **ACCEPTED & FIXED** | 已合并至 PR #1 (`a928ee2...`)；根目录 `plugin.json`，验证通过 |
| **F-004** | P2 | Windows 符号链接权限跳过未证防护有效 | **ACCEPTED & FIXED** | 已合并至 PR #1 (`a928ee2...`)；Mock 隔离测试通过 |
| **F-005** | P2 | 暂存扫描与 Git 提交历史扫描职责混淆 | **ACCEPTED & FIXED** | 已合并至 PR #1 (`a928ee2...`)；文档规范与测试闭环 |
| **F-006** | P2 | 五份记录与远端 PR 真实状态不一致且散列偏差 | **ACCEPTED & FIXED** | 已合并至 PR #1 (`c283303...`)；全量校准为远端 PR #1 与真实 SHA |
| **F-007** | P1 | 回复提取需提取全部正文块并剥离操作控件 | **ACCEPTED & FIXED** | `browser_relay.py` 中 `build_extract_js`：提取全部 `.prose, .markdown` 块无截断，剥离按钮；`test_dom_execution_multi_block_reply_extracts_all_blocks` PASS |
| **F-008** | P1 | 发送前 fail-closed 且发送后校验 user turn 哈希 | **ACCEPTED & FIXED** | `browser_relay.py` 中 `send_prompt`：发送前 DOM 检查异常直接阻断；发送后核验正式 user turn 内容 SHA-256；回归测试 PASS |
| **F-009** | P2 | Handoff 状态版本混淆与自引用提交死循环 | **ACCEPTED & FIXED** | 五份记录全量同步，清晰区分 `last_reviewed_sha` (`a928ee2...`)、`current_pr_head_sha` (`c283303...`) 与本地未推送修改 |
| **F-010** | P1 | 目标会话 `/c/<id>` 精确比对与防 TOCTOU 校验 | **ACCEPTED & FIXED** | `browser_relay.py` 中 `match_conversation_url` 精确校验会话 ID；借用后核验 URL；在注入和点击发送前实时校验当前 URL 防 TOCTOU |
| **F-011** | P2 | 源码双冲突文件防覆盖与打包保真度 | **ACCEPTED & FIXED** | `relay.py` 中 `pack_review`：当用户项目同时存在 `CHATGPT_START.md` 与 `CHATGPT_COORDINATION.md` 时动态分配新文件名，绝不覆盖源码 |
| **CLEANUP**| P2 | 资源清理失败不能静默掩盖成功 | **ACCEPTED & FIXED** | `browser_relay.py` 中 `run_e2e_review`：记录 `cleanup_status` 与异常列表，清理失败标记 `WARNING_CLEANUP_FAILED` 并保留诊断日志 |

## 代码变更清单 (第四轮加固)

1. `skills/chatgpt-review/scripts/browser_relay.py`:
   - `match_conversation_url()`: 实现精确会话比对，支持 `/c/<id>` 与可选 query 参数，拦截任何带恶意后缀的前缀伪造。
   - `send_prompt()`:
     - 发送前 DOM 检查状态异常时 fail-closed 抛出 `DeliveryError`。
     - 操作前 TOCTOU 防护：在输入注入与点击发送前分别核验当前 URL 与目标会话是否一致。
     - 发送后核验 formal user turn 文本的 SHA-256，不匹配抛出 `DeliveryError`。
   - `build_extract_js()`: 提取全部正文块（`.prose, .markdown`），避免多段落回复被单元素选择器截断。
   - `run_e2e_review()`: 即使仅通过 `target_tab_id` 借用，借用后也强制校验目标 URL；增加 `cleanup_info` 结构追踪 `return_tab` 与 `stop_session` 的执行结果，失败保留诊断。
2. `skills/chatgpt-review/scripts/relay.py`:
   - 动态处理文件名冲突：当 `CHATGPT_START.md` 与 `CHATGPT_COORDINATION.md` 同时存在于项目中时，自动循环生成 `CHATGPT_COORDINATION_<suffix>.md`，保护源码完整性。
3. `tests/test_browser_relay.py`:
   - 增加 7 项针对 r4 的专项回归测试（发送前 fail-closed、发送后 SHA 校验、精确 `/c/<id>` 会话匹配、借用后 URL 核验、TOCTOU URL 防护、多块提取、清理失败诊断），全部实测通过。
4. `tests/test_relay.py`:
   - 增加 `test_pack_preserves_both_chatgpt_start_and_coordination_when_both_exist` 校验双文件共存时的动态命名与内容保真度。

## 验证结论

- 单元测试: **63 项全部运行，61 PASS，2 SKIP，0 FAIL**。
- 敏感信息: 未引入任何未授权私钥、Token 或敏感内容。
- 远端状态: 未执行推送，PR #1 保持 DRAFT。
