STATUS: done

Files changed:
- `skills/chatgpt-review/scripts/workflow.py`
- `skills/chatgpt-review/scripts/browser_relay.py`
- `tests/test_browser_relay.py`
- `tests/test_workflow.py`

Anything could not do:
- None. (Step 5 自测已在内存中通过替换注入 JS 可见性判断执行验证，确认旧逻辑在隐藏 `#prompt-textarea` 存在时会选中隐藏元素导致断言失败，新逻辑通过。)