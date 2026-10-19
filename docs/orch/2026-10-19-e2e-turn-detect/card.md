# e2e-turn-detect — 用 ChatGPT 新版页面的语义标记识别消息

## Goal
ChatGPT 新版页面（2026-10-19 实测）没有 `article`，也没有 `data-message-author-role`。脚本只能退回按布局类名（`gap-3`、`block-`）去猜消息块，在 bsk 借用的 Agent 窗口里找不到用户消息，报 `USER_TURN_NOT_FOUND`。实际上消息在页面上，文字与原请求去空白后完全一致。
做完后：
1. 识别消息时优先使用语义标记，不依赖布局类名；
2. 找不到时，返回值里带诊断信息，报错文字里也带上。

## Risk
medium

## Parameters
实测 DOM（source：真实 Chrome 中的 `https://chatgpt.com/g/g-p-68d1848f372081918ae13ad310a7fff8-ove-phd/c/6ad60491-0ff0-83eb-a9ed-3719eb81d591`，只读读取）：
- 每条消息外层有 `[data-chatgpt-search-unit-key]`，值形如 `fallback-turn-1:0:user` 和 `fallback-turn-1:2:assistant`，按 `:` 分割后最后一段是角色。
- 用户气泡：`[data-user-message-bubble="true"]`；用户文字：`[data-markdown-text-tone="user-message"]`，其 textContent 去空白后与原请求一致。
- 消息块类名 `block-BQZwFn`，父容器类名 `flex flex-col gap-3 browser:gap-1`。这些是布局类名，不可依赖。

## Files
may change: `skills/chatgpt-review/scripts/browser_relay.py`, `tests/test_browser_relay.py`, `tests/dom_fixture.js` / read-only: `skills/chatgpt-review/scripts/workflow.py`

## Steps (N = 5)
1. `browser_relay.py`：`build_check_user_turn_js`（约 166–223 行）和 `build_extract_js`（约 278–335 行）里各有一份相同的 `getTurnRole` + `findTurns`。把它们提取成模块级普通字符串常量 `_TURN_DETECTION_JS`（单花括号，不是 f-string），两个 builder 都通过字符串拼接引用它，删掉两份副本。原有行为必须完全不变；第 2 步才改逻辑。
2. `_TURN_DETECTION_JS` 里：
   - `findTurns` 最前面加一个优先分支：
     - 收集 `main.querySelectorAll('[data-chatgpt-search-unit-key]')`；
     - 角色取 `key.split(':').pop()`，只保留 `user` / `assistant`；
     - 有祖先也带 `[data-chatgpt-search-unit-key]` 的元素丢掉，只保留最外层；
     - 有结果就按文档顺序返回 `{node, role}` 列表；否则走原来的回退链，原逻辑一行不改。
   - `getTurnRole` 在 `data-message-author-role` 两项检查之后，加 `if (el.matches && el.matches('[data-user-message-bubble]') || el.querySelector('[data-user-message-bubble]')) return 'user';`。
3. `build_check_user_turn_js` 的「没找到」分支：返回 `{exists:false, status:'USER_TURN_NOT_FOUND', diag:{turns:turns.length, roles:turns.map(t=>t.role).join(','), units: main.querySelectorAll('[data-chatgpt-search-unit-key]').length, bubbles: main.querySelectorAll('[data-user-message-bubble]').length, mainHasReq: main.textContent.includes(<req>)}}`。`BLOCKED_DOM_NOT_SUPPORTED` 分支也加上同样的 `diag`。
4. `send_prompt` 末尾的「failed verification as a formal user turn」报错，以及 `run_e2e_review` 中 skip_send 恢复路径的「Recovery check failed: … not found in DOM」报错：在消息末尾追加 `diag=<json.dumps(最后一次 check 结果的 diag)>`，取不到就写 `diag=none`。
5. `tests/test_browser_relay.py`：新增 node DOM 测试，用 `tests/dom_fixture.js` 构造新版结构。`main` 下放一个**没有** `gap-3` / `browser:gap-1` / `block-` 类名的普通 div，里面依次是：
   - ① `div[data-chatgpt-search-unit-key="fallback-turn-0:0:user"]`，内含 `div[data-user-message-bubble="true"] > div[data-markdown-text-tone="user-message"]`，文字为「你好」；
   - ② `div[data-chatgpt-search-unit-key="fallback-turn-0:2:assistant"]`，含一段回复文字；
   - ③ 与 ① 结构相同的用户块，文字以 `# Plan [req-X]` 开头；
   - ④ 一个 assistant 块，文字为 `answer for X`。
   断言：
   - `test_dom_new_layout_user_turn_found_by_unit_key`：`build_check_user_turn_js("req-X")` 返回 `exists: true`，`userText` 包含 `req-X`。这个测试在旧代码上必须失败（旧代码在这种结构下返回 `BLOCKED_DOM_NOT_SUPPORTED`）。
   - `test_dom_new_layout_extract_assistant_after_user`：`build_extract_js("req-X")` 提取到 `answer for X`，不是第一条回复。
   - `test_dom_not_found_returns_diag`：同样结构，查一个不存在的 `req-Y`，返回 `status == 'USER_TURN_NOT_FOUND'`，且 `diag.units == 4`、`diag.bubbles == 2`、`diag.mainHasReq is False`。
   fixture 不支持 `matches` / `closest` 时，在 `dom_fixture.js` 的 DOMNode 上补最小实现，复用已有的 `matchSimpleSelector`。

## Check
`python -B -m unittest discover -s tests -q`

## Don't
- No commits, no other files, no refactors beyond the steps.
- Run scripts in the foreground and wait; before replying, stop any background task still running.
- Reuse existing functions; don't copy existing logic.
- No rm, no git push, no git checkout . / restore . / reset --hard.
- 先只跑本卡新增的测试和 `tests.test_browser_relay`（几秒钟），全部通过后再跑一次完整 Check；不要每改一处就跑完整套件。
- 不要改投递后的哈希核验、点击、模型核对和 `workflow.py`；不要新增 skip；不要连接浏览器或 bsk。

## Acceptance
- verdict: review pending
- files: skills/chatgpt-review/scripts/browser_relay.py, tests/dom_fixture.js, tests/test_browser_relay.py
- check: `python -B -m unittest discover -s tests -q` → exit 0 (run by run-card)
- tokens:
  - round 1: exec custom-local:antigravity/gemini-3.8-flash: fresh 263k, cached 5172k, out 35k  (agy-e2e-turn-detect.jsonl) · review gpt-6.1-sol: fresh 0k, cached 0k, out 0k  (codex-review-e2e-turn-detect.jsonl)
- review findings: 0 real / 0 false positive / 0 missed (medium, gpt-6.1-sol)
- claude checks (review pending, sol quota used up): the 3 new tests fail on HEAD b103ef0 and pass after; the new detection JS, run read-only on the real ChatGPT page, found user,assistant,user,assistant and extracted the plan reply with its JSON block.
