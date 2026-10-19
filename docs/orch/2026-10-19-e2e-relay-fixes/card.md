# e2e-relay-fixes — 修复真实 E2E 中暴露的三个发送缺陷

## Goal
1. 提示词已写入输入框时，不再误报 `INJECT_EMPTY`；确实没写进去时，报错里带诊断信息，且错误类型表明「未发送」。
2. 在点击发送按钮之前失败，事务记为 `NOT_SENT`；下一次 `exchange` 用同一个 request_id 正常重发，而不是只查不发、永久卡死。
3. 用户传入的聊天地址必须是 `/c/<uuid>` 形式，占位符（如 `/c/<问候交流的ID>`）在 `start` / `exchange`（含 `--dry-run`）/ `resume` 都被拒绝。恢复路径不再静默忽略新的 `--chat-url`：`NOT_SENT` 时切换，其他状态明确报错。

真实复现证据（只读）：`D:/2/dev/review-relay/e2e-runs/c6c049d/handoffs/20261019-live-e2e-c6c049d-b/workflow.json`
（`delivery_status: DELIVERY_UNKNOWN`，journal 报 `INJECT_EMPTY`，但用户截图显示请求全文已在 ChatGPT 输入框里，未发送）；
`.../20261019-live-e2e-c6c049d/workflow.json`（占位地址被接受，之后带正确 `--chat-url` 重跑仍按占位地址找标签页）。

## Risk
medium

## Parameters
- 会话地址正则（路径部分）：`^(/g/[A-Za-z0-9_-]+)?/c/[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}/?$`
- 合法样例：`https://chatgpt.com/g/g-p-68d1848f372081918ae13ad310a7fff8-ove-phd/c/6ad5e25e-5628-83ed-8cac-6e8e6b447f3c`、`https://chatgpt.com/c/6ad5e25e-5628-83ed-8cac-6e8e6b447f3c`
- 新的投递状态值：`NOT_SENT`
- 写入后补查等待：0.5 秒，补查 1 次
- journal 新字段：`send_clicked`（bool）

## Files
may change: `skills/chatgpt-review/scripts/browser_relay.py`, `skills/chatgpt-review/scripts/workflow.py`, `tests/test_browser_relay.py`, `tests/test_workflow.py`, `tests/dom_fixture.js` / read-only: `D:/2/dev/review-relay/e2e-runs/`

## Steps (N = 7)
1. `browser_relay.py`：在 `validate_chatgpt_origin` 后新增 `validate_conversation_url(url) -> bool`：先 `validate_chatgpt_origin(url)`，再用 Parameters 里的正则匹配 `urlparse(url).path`。新增异常 `class NotSentError(DeliveryError)`，放在 `DeliveryError` 定义之后。
2. `browser_relay.py` → `send_prompt` 的 `inject_js`（约 758–790 行）：
   - 候选元素：按原来的选择器顺序，用 `querySelectorAll` 收集，去重；
   - 可见判断：`n.getClientRects ? n.getClientRects().length > 0 : true`；
   - 选择：优先「可见且 `contenteditable="true"`」，其次「可见」，最后取第一个；
   - 写入逻辑不变（textarea 设 value；contenteditable 用 focus + selectAll + insertText）；
   - 写入后的判断改为：`el` 的文本**或** `document.activeElement` 的文本包含 request_id（用 `json.dumps(request_id)` 嵌入 JS）就返回 `'OK'`；否则返回 `'INJECT_EMPTY:' + JSON.stringify({candidates, chosen:{tag,id,ce,visible}, active:{tag,id}})`；没有任何候选元素则返回 `'NO_INPUT'`。
3. `browser_relay.py` → `send_prompt`：`res` 以 `INJECT_EMPTY` 开头时，`time.sleep(0.5)` 后执行一段只读 JS：按第 2 步的规则重新选出可见输入框，返回其文本是否包含 request_id（`'OK'` / `'MISSING'`）。结果为 `'OK'` 就继续往下走；否则先尽力清空输入框（focus + `execCommand('selectAll')` + `execCommand('delete')`，失败忽略），再 `raise NotSentError(f"Prompt injection not confirmed: {res}")`。`NO_INPUT` 同样抛 `NotSentError`。不要动后面的点击和投递核验逻辑。
4. `browser_relay.py` → 点击记录：在 `send_prompt` 中，`send_res = "NO_SEND_ACTION"` 这一行之前加 `self._send_clicked = True`。在 `run_e2e_review` 的 `try` 之前加 `self._send_clicked = False`。在它的 `finally` 开头加 `journal_entry["send_clicked"] = bool(self._send_clicked)`。`__init__` 里初始化 `self._send_clicked = False`。
5. `workflow.py`：
   - `VALID_DELIVERY_STATUSES` 加入 `"NOT_SENT"`；
   - 约 1622–1625 行改为：恢复时只有 `delivery_status != "NOT_SENT"` 才 `skip_send = True`；为 `NOT_SENT` 时，把事务的 `delivery_status` 设为 `"SEND_ATTEMPTED"` 并 `save_state`，再正常发送；
   - 约 1680 行：未确认时，若 `res.get("send_clicked") is False and not skip_send`，记为 `"NOT_SENT"`，否则仍记 `"DELIVERY_UNKNOWN"`（键缺失时按原样 fail-closed）。
6. `workflow.py`，用户传入地址的校验：从 `browser_relay` 导入 `validate_conversation_url`（两处 import 都要加）。
   - `start`（约 835 行的函数）：`chat_url` 非空且不合法时，抛 `WorkflowError`；
   - `execute_exchange`：在约 1534 行的恢复判断**之前**，`chat_url` 非空且不合法就抛 `WorkflowError`（dry-run 也一样）；
   - 恢复路径（约 1543 行）：传入的 `chat_url` 与事务的 `chat_url` 不同时：
     - 事务 `delivery_status == "NOT_SENT"`：调用 `update_chat_url(task_dir, chat_url)`，事务的 `chat_url` 改成新地址，`save_state`，并用新地址作为 `target_chat_url`；
     - 其他状态：抛 `WorkflowError`，信息里写明 request_id、事务地址和新地址，说明「消息可能已送达旧聊天，拒绝切换」；
   - 两种情况都不能静默忽略；
   - `resume`（约 1770 行）：调用 `update_chat_url` 前用 `validate_conversation_url` 校验，不合法抛 `WorkflowError`。
   - `update_chat_url` 本身不改：它也接收发送后页面的真实地址。
7. 测试，每条都要在旧代码上失败。workflow 测试沿用 `test_delivery_unknown_fails_closed` 的 mock 方式。
   - `test_browser_relay.py`：
     - `test_validate_conversation_url`：两个合法样例返回 True；占位地址、`https://chatgpt.com/`、http 协议返回 False。
     - `test_inject_empty_then_verify_ok_continues_send`：mock `evaluate`，注入返回 `INJECT_EMPTY:{}`，补查返回 `OK`，之后按原流程成功，断言点击被执行。
     - `test_inject_not_confirmed_raises_not_sent_and_journal_send_clicked_false`：注入和补查都失败，`send_prompt` 抛 `NotSentError`；经 `run_e2e_review` 时，journal 的 `send_clicked` 为 False。
     - node DOM 测试 `test_dom_inject_prefers_visible_contenteditable`：在 `dom_fixture.js` 的 DOMNode 加 `getClientRects()`，带 `data-hidden` 属性时返回 `[]`，否则返回 `[{}]`；构造一个隐藏的 `#prompt-textarea` 和一个可见的 contenteditable，断言写入选中的是可见那个。如果 fixture 不支持 `execCommand`，就在 fixture 里给 document 加一个最小 `execCommand`：`insertText` 写入 activeElement 的文本。
   - `test_workflow.py`：
     - `test_pre_click_failure_records_not_sent_and_resends`：第一次 relay 返回 `{"status":"BLOCKED_DELIVERY_UNCONFIRMED","send_clicked":False,...}`，事务记为 `NOT_SENT`；第二次 `exchange` 以 `skip_send=False`、同一个 request_id 调用 relay，SUCCESS 后正常推进。
     - `test_recovery_chat_url_switch_rules`：`NOT_SENT` 的事务换地址成功，`previous_chat_urls` 含旧地址；`DELIVERY_UNKNOWN` 的事务换地址抛 `WorkflowError`，state 文件不变。
     - `test_placeholder_chat_url_rejected`：`start`、`exchange`（含 dry-run）、`resume` 收到占位地址都抛 `WorkflowError`。

## Check
`python -B -m unittest discover -s tests -q`

## Don't
- No commits, no other files, no refactors beyond the steps.
- Run scripts in the foreground and wait; before replying, stop any background task still running.
- Reuse existing functions; don't copy existing logic.
- No rm, no git push, no git checkout . / restore . / reset --hard.
- 不要改 `wait_for_response`、模型核对、点击按钮的选择器和投递后的哈希核验。
- 不要新增 skip；已有 2 个 symlink skip 保持原样。
- 不要连接浏览器或 bsk，不要运行 `workflow.py exchange`；全部用 mock 和 node fixture。

## Fix 1
Check 还剩 10 个 ERROR，全是 `tests/test_workflow.py` 里旧测试用的非 uuid 假地址（如 `https://chatgpt.com/c/dedup-chat`）被新校验拒绝。只做下面一步，不改任何其他代码：

1. 在仓库根目录用 Python 运行下面这段（可以先写成临时文件，跑完删掉；或者用 `python -` 从标准输入读入）。它把 `tests/test_workflow.py` 里每个非 uuid 的 `/c/<slug>` 换成由 slug 确定的 uuid5，同一个 slug 总是映射到同一个 uuid，所以前后断言保持一致。占位符测试（含 `<`）不受影响：

```python
import re, uuid, pathlib
p = pathlib.Path("tests/test_workflow.py")
s = p.read_text(encoding="utf-8")
U = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
def rep(m):
    slug = m.group(1)
    if U.fullmatch(slug):
        return m.group(0)
    return "https://chatgpt.com/c/" + str(uuid.uuid5(uuid.NAMESPACE_URL, slug))
s2 = re.sub(r"https://chatgpt\.com/c/([A-Za-z0-9-]+)", rep, s)
assert s2 != s
p.write_text(s2, encoding="utf-8", newline="\n")
```

2. 运行 Check，确认 113 项中 0 FAIL / 0 ERROR，skip 仍为 2。若仍有 ERROR，只报告测试名和异常行，不要再改。

## Fix 2
Fix 1 的替换脚本漏了一处断言里的裸 slug。只改这一行，不改其他任何东西：

1. `tests/test_workflow.py` 第 602 行（`test_cross_chat_continuation` 中）：
   `self.assertIn("old-chat-1", brief)` → `self.assertIn("af80494a-c5a5-5883-aa24-d4ae27872dff", brief)`
   （这是 `uuid5(NAMESPACE_URL, "old-chat-1")`，与第 600 行 `previous_chat_urls` 里的地址一致。）
2. 运行 Check，确认 113 项 0 FAIL / 0 ERROR，skip 为 2。仍失败只报告，不再改。

## Acceptance (draft)
- verdict: accepted via v2 (`docs/orch/2026-10-19-e2e-relay-fixes-v2/`): full sol review found 5 P1, all fixed there; check after Fix 2: 113 tests, 0 fail, 2 skip
- files: skills/chatgpt-review/scripts/browser_relay.py, skills/chatgpt-review/scripts/workflow.py, tests/dom_fixture.js, tests/test_browser_relay.py, tests/test_workflow.py
- check: `python -B -m unittest discover -s tests -q` → exit 0 (run by run-card)
- tokens:
  - round 1: exec custom-local:antigravity/gemini-3.8-flash: fresh 2209k, cached 121917k, out 478k  (agy-e2e-relay-fixes.jsonl)
  - Fix 1: exec custom-local:antigravity/gemini-3.8-flash: fresh 40k, cached 373k, out 11k  (agy-e2e-relay-fixes.jsonl)
  - Fix 2: exec custom-local:antigravity/gemini-3.8-flash: fresh 26k, cached 259k, out 3k  (agy-e2e-relay-fixes.jsonl)
- review findings: 5 real / 0 false positive / 0 missed (medium, gpt-6.1-sol full review run by hand after check_failed; #3 only partly real: early-return paths never use the URL). Fix 1 missed a bare slug assertion (card error, fixed in Fix 2).
