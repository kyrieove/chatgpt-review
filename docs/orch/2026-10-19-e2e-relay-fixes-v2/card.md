# e2e-relay-fixes-v2 — 补齐 v1 审查发现的 5 个 P1

## Goal
在 v1 未提交改动之上（同一工作区，不要回退任何 v1 改动）：
1. `--dry-run` 在任何路径下都不写 state 文件。
2. `NOT_SENT` 事务切换聊天地址时，不再沿用旧的 `tab_id`。
3. `execute_exchange` 在任何阶段分支之前就校验传入的 `chat_url`。
4. daemon 或浏览器不可用（`BLOCKED_NO_BROWSER`）时，journal 也带 `send_clicked: False`，事务记为 `NOT_SENT`。
5. 「可见编辑框优先」测试改为执行生产代码里的注入 JS。

v1 审查原文：`docs/orch/2026-10-19-e2e-relay-fixes/review.md`

## Risk
medium

## Parameters
- 占位地址样例：`https://chatgpt.com/c/<问候交流的ID>`
- 合法地址 A：`https://chatgpt.com/c/11111111-1111-1111-1111-111111111111`；合法地址 B：`https://chatgpt.com/c/22222222-2222-2222-2222-222222222222`

## Files
may change: `skills/chatgpt-review/scripts/browser_relay.py`, `skills/chatgpt-review/scripts/workflow.py`, `tests/test_browser_relay.py`, `tests/test_workflow.py` / read-only: `tests/dom_fixture.js`

## Steps (N = 6)
1. `workflow.py` → `execute_exchange`：把 `if chat_url and not validate_conversation_url(chat_url): raise WorkflowError(...)` 移到 `with TaskLock(...)` 里 `state = load_state(...)` 的紧后面，排在所有 `if stage == ...` 提前 return 之前；删掉原来位置（约 1547 行）的那份，不要留两份。
2. `workflow.py` → `execute_exchange` 恢复分支（约 1561–1575 行）：遇到 `NOT_SENT` 且地址不同的情况，这里**只做判断不写盘**：设局部变量 `pending_switch = True`，`target_chat_url = chat_url`，`target_tab_id = <调用方传入的 tab_id 参数>`（可以是 None），**不要**再用 `active_tx.get("tab_id")`。非 `NOT_SENT` 时照旧抛 `WorkflowError`。不切换时保持原来的 `target_tab_id = active_tx.get("tab_id") or target_tab_id`。在函数开头把 `pending_switch` 初始化为 False。
3. `workflow.py` → 同一函数，在 `if dry_run: return {...}` 之后，紧挨原有的 `if not is_recovery:` 分支，加一个 `elif pending_switch:` 分支，执行第 2 步里原先的写盘动作：
   - `update_chat_url(task_dir, chat_url)`，然后 `state = load_state(task_dir)`；
   - `active_tx = state["active_transaction"]`，设 `active_tx["chat_url"] = chat_url`、`active_tx["tab_id"] = target_tab_id`；
   - `save_state(task_dir, state)`。
   原有的 `elif chat_url and not active_tx.get("chat_url"):` 分支保持不变，放在新分支之后。
4. `browser_relay.py` → `run_e2e_review`：在 `journal_entry` 字典创建处加 `"send_clicked": False`，位置在 `check_status()` 之前，这样 `BLOCKED_NO_BROWSER` 的提前 return 也带上这个字段。`finally` 里原有的赋值保持不变。
5. `tests/test_browser_relay.py` → `test_dom_inject_prefers_visible_contenteditable`：删掉复制来的 `test_js`，改为捕获生产代码的注入 JS：
   - 构造 `BrowserRelay()`；`patch.object` 掉 `check_user_turn_exists`，让它返回 `{"status": "USER_TURN_NOT_FOUND", "exists": False}`；
   - `evaluate` 用 side_effect：把第一次调用的 js 参数存起来，然后抛自定义异常 `_Captured`；
   - 调用 `send_prompt(session, msg, req_id)`，不传 expected_url / model / thinking，这样第一次 `evaluate` 就是注入；用 `assertRaises(_Captured)` 接住；
   - 把捕获到的 js 放进原来的 DOM fixture（隐藏的 `#prompt-textarea` + 可见的 contenteditable），用 `self.run_in_dom` 执行，断言返回 `'OK'` 且文字写进了可见元素。
   用 `git stash` 之外的方式自测它在旧代码上会失败：在内存里把注入 JS 中的可见性判断换掉看结果即可，不必改文件；做不到就跳过这项自测，在回复里说明。
6. 新测试（都要在当前代码上先失败）：
   - `tests/test_workflow.py`：
     - `test_not_sent_switch_dry_run_read_only_and_tab_cleared`：事务为 `NOT_SENT`，`chat_url` 为 A，`tab_id` 为 `"tab-old"`。用地址 B 执行 `exchange(dry_run=True)`，前后 state 文件字节相同。再用 B 正式 `exchange`，mock 的 relay 收到 `target_url_prefix=B`、`target_tab_id=None`；之后 state 中事务的 `chat_url` 为 B、`tab_id` 为 None，`previous_chat_urls` 含 A。
     - `test_placeholder_rejected_before_stage_returns`：任务处于 `WAITING_EXECUTION`，用占位地址 `exchange`，抛 `WorkflowError`。
     - `test_no_browser_records_not_sent`：mock 的 relay 返回 `{"status": "BLOCKED_NO_BROWSER", "send_clicked": False, ...}`，事务 `delivery_status` 为 `NOT_SENT`。
   - `tests/test_browser_relay.py`：
     - `test_no_browser_journal_has_send_clicked_false`：mock `check_status` 返回 daemon 未运行，`run_e2e_review` 的 journal 里 `send_clicked is False`。
   mock 方式沿用文件里已有的 `test_pre_click_failure_records_not_sent_and_resends` 和 `test_inject_not_confirmed_raises_not_sent_and_journal_send_clicked_false`。

## Check
`python -B -m unittest discover -s tests -q`

## Don't
- No commits, no other files, no refactors beyond the steps.
- Run scripts in the foreground and wait; before replying, stop any background task still running.
- Reuse existing functions; don't copy existing logic.
- No rm, no git push, no git checkout . / restore . / reset --hard.
- 不要回退 v1 的任何改动；不要用 git stash。
- 跑测试时先只跑本卡新增和改动的测试（`python -B -m unittest tests.test_workflow.WorkflowTests.<name> ...`），全部通过后再跑一次完整 Check；不要每改一处就跑完整套件。
- 不要新增 skip；不要连接浏览器或 bsk。

## Acceptance
- verdict: accepted
- files: skills/chatgpt-review/scripts/browser_relay.py, skills/chatgpt-review/scripts/workflow.py, tests/test_browser_relay.py, tests/test_workflow.py
- check: `python -B -m unittest discover -s tests -q` → exit 0 (run by run-card)
- tokens:
  - round 1: exec custom-local:antigravity/gemini-3.8-flash: fresh 291k, cached 5822k, out 18k  (agy-e2e-relay-fixes-v2.jsonl) · review gpt-6.1-sol: fresh 80k, cached 346k, out 1k  (codex-review-e2e-relay-fixes-v2.jsonl)
- review findings: 0 real / 0 false positive / 0 missed (medium, gpt-6.1-sol)
