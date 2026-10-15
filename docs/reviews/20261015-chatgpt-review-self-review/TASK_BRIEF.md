# 任务书：chatgpt-review 自审、可靠性加固与版本一致性（第四轮实施与验证）

- task_id: 20261015-chatgpt-review-self-review
- round: 4
- 目标仓库: kyrieove/chatgpt-review
- 输入基线: c6ce0fa3f4e65d5dadbb61bf792e7d9f8b996b22
- 本地分支: feature/chatgpt-review-self-review
- 远端 PR: [Draft PR #1](https://github.com/kyrieove/chatgpt-review/pull/1)
- last_reviewed_sha: `a928ee26776fb82ed80c43df131234c7af2f67c8`（ChatGPT 审查基准）
- last_published_sha / current_pr_head_sha: `c28330334f11f8a72b3020f8d97b5b516fe1f8c6`（远端 PR #1 当前 head）
- 本地 HEAD: `c28330334f11f8a72b3020f8d97b5b516fe1f8c6`
- 本地工作区状态: 本地工作区包含第四轮核心加固与回归测试（`browser_relay.py`、`relay.py`、`test_browser_relay.py`、`test_relay.py`、`dom_fixture.js` 及五份任务记录），未获用户许可严禁推送或合并。
- 状态: AWAITING_USER_REVIEW

## 第四轮目标与范围（针对 r3 CHANGES REQUESTED 反馈）

1. **F-008（P1）**:
   - `send_prompt` 发送前 DOM 检查异常或非正常状态必须 fail-closed，严禁继续注入发送。
   - 发送后必须核验正式 user turn 内容的 SHA-256 哈希匹配，不能仅判断 `exists`。
2. **F-010（P1）**:
   - 目标 conversation ID 必须精确匹配（`/c/<id>`），禁止前缀匹配导致跨会话串扰。
   - 即使调用仅指定 `target_tab_id`，借用后的页面 URL 也必须与初始目标一致。
   - 在输入注入与点击发送关键操作前实时重新校验当前 URL，彻底防范 TOCTOU 竞争漏洞。
3. **F-007（P1）**:
   - 完整提取 assistant 回复中的所有正文块（`.prose, .markdown`），避免因只取首个元素导致的多块内容截断。
4. **F-011（P2）**:
   - 当用户项目源码中同时已存在 `CHATGPT_START.md` 与 `CHATGPT_COORDINATION.md` 时，打包机制动态分配唯一后缀名称（`CHATGPT_COORDINATION_<suffix>.md`），绝不覆盖任一源码文件。
5. **资源清理可诊断性（P2）**:
   - `return_tab` 或 `stop_session` 返回 False 或抛出异常时，不得静默宣称完全清理成功；记录 `cleanup_status: CLEANUP_FAILED` 与详细诊断信息。

## 验收项与状态

| ID | 检查项 | 目标 | 状态 | 证据 |
|---|---|---|---|---|
| A-001 | F-008 发送前 fail-closed 与发送后哈希校验 | 检查异常直接阻断；发送后校验 user turn 内容 SHA-256 | PASS | `test_send_prompt_fails_closed_on_pre_send_parse_error`、`test_send_prompt_verifies_user_turn_hash_after_send` |
| A-002 | F-010 会话精确匹配与 TOCTOU 防护 | `/c/<id>` 精确比对；借用前后与输入/点击前核验 URL | PASS | `test_target_conversation_id_must_match_exactly`、`test_borrowed_page_must_match_initial_url_even_with_target_tab_id_only`、`test_send_prompt_verifies_url_before_inject_and_send_preventing_toctou` |
| A-003 | F-007 多正文块完整提取 | 提取所有 `.prose, .markdown` 块，多段无缝拼接 | PASS | `test_dom_execution_multi_block_reply_extracts_all_blocks` |
| A-004 | F-011 双文件冲突防覆盖 | 当 START 与 COORDINATION 均存在时分配非冲突名称 | PASS | `test_pack_preserves_both_chatgpt_start_and_coordination_when_both_exist` |
| A-005 | 资源清理诊断保留 | 清理失败记录错误状态并标记警告 | PASS | `test_run_e2e_review_records_diagnostic_when_cleanup_fails` |
| A-006 | 全量回归测试 | 63 项测试运行 | PASS | 61 PASS, 2 SKIP, 0 FAIL（禁止表述为 63 PASS） |
| A-007 | 第四轮安全审查 ZIP | 生成 r4 安全审查 ZIP 与 SHA-256 | PASS | `review-artifacts/20261015-chatgpt-review-self-review-r4-557fbc76afbf4601b5de65f690842329.zip` (SHA-256: `05d235fe88abcc60eb498b7696e6c919e4c001f6672a8416dd3f3b8f9cc35e1e`) |

## 授权边界

- 远端写入：Draft PR #1 当前 head 保持 `c28330334f11f8a72b3020f8d97b5b516fe1f8c6`。当前工作区改动**未获用户明确许可，严禁执行 git push、转 Ready、创建新 PR 或合并**。
- 浏览器权限：未经用户明确许可，不向真实 ChatGPT 标签页发送测试消息。
