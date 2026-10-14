# 任务书：chatgpt-review 自审与插件化（第一轮实施与验证）

- task_id: 20261015-chatgpt-review-self-review
- 目标仓库: kyrieove/chatgpt-review
- 输入基线: c6ce0fa3f4e65d5dadbb61bf792e7d9f8b996b22
- 本地分支: feature/chatgpt-review-self-review
- 状态: AWAITING_REVIEW（本地实施与验证已完成，未推送、尚无 PR）

## 用户目标

在不破坏 chatgpt-review 核心协作协议的前提下：
1. 修复 CLI 边界问题（显式文件打包失败、任务 ID 指定与防覆盖）。
2. 在仓库根目录添加最小 plugin.json，复用唯一 Skill 目录，使 Antigravity 可直接安装/识别插件。
3. 补全跨平台测试，隔离 Windows 符号链接权限限制，增加入口文档一致性检查。
4. 明确推送前历史凭据检查与 ZIP 暂存检查边界。
5. 验证 Chrome 与 Antigravity 插件集成能力，遵守借用确认，不改动全局激活配置。
6. 生成审查包并交由 ChatGPT 进行独立复审。

## 验收项与完成状态

| ID | 检查项 | 目标 | 当前状态 | 证据 |
|---|---|---|---|---|
| A-001 | F-001 任务 ID 指定与防冲突 | `relay.py start --task-id <id>` 支持指定合法 ID，已有同名任务报错且不覆盖现有文件 | PASS | `test_relay.py` 4 项测试覆盖正常、冲突、非法 ID、指针更新 |
| A-002 | F-002 显式文件与目录收集边界 | 显式单个文件超限、不可读、敏感或被过滤时立即报错退出；显式目录允许过滤子项并记录于 `skipped_paths` | PASS | `test_relay.py` 7 项测试覆盖超限、敏感、非普通文件、不可读、目录过滤 |
| A-003 | F-003 最小 plugin.json | 根目录添加包含 name、description 的最小 manifest，复用唯一 `skills/chatgpt-review` | PASS | `agy plugin validate .` 输出 `✔ skills: 1 processed`，`test_plugin.py` 通过 |
| A-004 | F-004 符号链接测试隔离 | 在无 Windows 符号链接权限环境下提供 mock 隔离测试，保留真实链接测试 | PASS | 31 项测试中 29 项 PASS，2 项符号链接权限受限正常 SKIP |
| A-005 | F-005 凭据检查边界说明 | 文档明确区分工作树/拟推送 commit 历史检查与本次 ZIP 暂存内容扫描 | PASS | README.md、executor.md、contract.md、CHATGPT_START.md 已同步 |
| A-006 | 文档与源协议一致性 | `CHATGPT_START.md` 由 `build_bootstrap.py` 生成并自动化测试比对 | PASS | `test_committed_bootstrap_matches_generation_and_tracks_reference_changes` PASS |
| A-007 | Chrome 操作与会话验证 | 通过 bsk 验证 daemon、observe、eval，借用确认机制及标签页归还 | PASS | bsk session `ljln` 成功 observe、evaluate、borrow 确认并 return 归还 |
| A-008 | 安全审查包生成 | 生成符合协议的便携 ZIP，包含五份持久记录与代码变更，无凭据泄露 | PASS | 由 `relay.py pack` 输出 review-artifacts ZIP |

## 授权边界

- 本轮仅限本地实施、验证与材料打包。
- 未获得用户明确授权前，禁止 git push、创建/更新 Draft PR、合并或修改远端设置。
- ChatGPT 审查结论不构成发布授权。
