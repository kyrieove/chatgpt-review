# 测试与验证结果报告（第四轮加固全量报告）

- task_id: 20261015-chatgpt-review-self-review
- round: 4
- 远端 PR: [Draft PR #1](https://github.com/kyrieove/chatgpt-review/pull/1)
- 远端 Head SHA: `c28330334f11f8a72b3020f8d97b5b516fe1f8c6`
- 本地 HEAD: `c28330334f11f8a72b3020f8d97b5b516fe1f8c6`
- 基线 Commit SHA: `c6ce0fa3f4e65d5dadbb61bf792e7d9f8b996b22`
- 测试汇总: **63 项测试运行完毕，61 项 PASS，2 项 SKIP，0 项 FAIL**。

## 详细测试套件分布

| 测试模块 | 运行数 | PASS | SKIP | FAIL | 说明 |
|---|---|---|---|---|---|
| `tests/test_browser_relay.py` | 31 | 31 | 0 | 0 | 覆盖 F-007 多块提取、F-008 fail-closed 及发送后哈希、F-010 精确会话及 TOCTOU 防护、资源清理诊断 |
| `tests/test_relay.py` | 30 | 28 | 2 | 0 | 新增 F-011 双文件冲突防覆盖测试；2 项跳过因 Windows 目录软链接受系统权限限制 |
| `tests/test_plugin.py` | 2 | 2 | 0 | 0 | 根目录 plugin.json 结构与 Skill 引用校验 |
| **总计** | **63** | **61** | **2** | **0** | **禁止表述为 63 PASS** |

## 第四轮新增针对性回归测试表 (8 项先行失败并在修复后通过)

| 场景 | 对应测试方法 | 状态 | 验证逻辑 |
|---|---|---|---|
| 1. F-008 发送前检查异常 fail-closed | `test_send_prompt_fails_closed_on_pre_send_parse_error` | PASS | 发送前 DOM 检查返回解析失败或未知状态时，直接抛出 `DeliveryError` 终止，严禁注入 |
| 2. F-008 发送后验证正文哈希 | `test_send_prompt_verifies_user_turn_hash_after_send` | PASS | 发送后不仅验证 user turn 存在，更核验 DOM 中提取的正文 SHA-256 与发送消息一致 |
| 3. F-010 目标会话精确匹配 | `test_target_conversation_id_must_match_exactly` | PASS | 精确比对 `/c/<id>`，阻止后缀伪造前缀匹配（如 `/c/conv-123-extra-malicious`） |
| 4. F-010 Tab ID 借用后 URL 核验 | `test_borrowed_page_must_match_initial_url_even_with_target_tab_id_only` | PASS | 即使只通过 `target_tab_id` 借用，借用后也核验当前 URL 与初次发现的目标一致 |
| 5. F-010 输入与发送前防 TOCTOU | `test_send_prompt_verifies_url_before_inject_and_send_preventing_toctou` | PASS | 在注入提示词前与点击发送前实时核验 URL，防止异步跳转劫持 |
| 6. F-007 回复多正文块完整提取 | `test_dom_execution_multi_block_reply_extracts_all_blocks` | PASS | Node.js DOM 夹具实测：提取回复中所有 `.prose, .markdown` 块，多段无缝拼接无截断 |
| 7. F-011 双文件冲突动态防覆盖 | `test_pack_preserves_both_chatgpt_start_and_coordination_when_both_exist` | PASS | 当项目已有 `CHATGPT_START.md` 与 `CHATGPT_COORDINATION.md` 时自动分配新名字，源码完全保真 |
| 8. 资源清理失败保留诊断 | `test_run_e2e_review_records_diagnostic_when_cleanup_fails` | PASS | `return_tab` 或 `stop_session` 失败时记录 `CLEANUP_FAILED` 与错误列表，标记状态警告 |

## 限制与未验证项声明

1. **真实浏览器实机发送**: 本轮在 Node.js DOM 夹具和 Mock 隔离环境中执行了真实 DOM JavaScript 与单元测试，**未向真实在线 ChatGPT 会话发送 live prompt**。依据授权边界，真实会话交互须待用户明确指定目标会话并批准测试消息后执行。
2. **符号链接测试跳过**: `test_evidence_path_rejects_symlink_ancestor_when_available` 等 2 项测试因 Windows 平台无目录符号链接权限而跳过，已在代码中通过 Mock 隔离覆盖。
3. **远端写入限制**: 本地新增与修改代码未向 GitHub PR #1 推送。
