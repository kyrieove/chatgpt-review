# chatgpt-review v2 协作指令

替换尖括号内容。GitHub Review 默认优先，实际读不到对应版本时用备用 ZIP。

## ChatGPT 首轮任务书

先读取或上传本仓库 CHATGPT_START.md。

```text
按 chatgpt-review v2 担任协调与审查端。
目标：<需求>。项目仓库：<owner/repo>。执行 Agent：<名称>。
约束：<允许修改范围与需保留行为>。
给可以直接转交的任务书，包含本地功能分支、验收、测试与凭据检查、持久记录和发布准备。
对应远端写入需要我的明确许可，审查通过不能代替许可。项目事实未知时先派调查任务，不猜路径。
```

## 执行 Agent 本地准备

```text
读取 <skill 实际路径>/SKILL.md，按执行端角色完成任务书。
项目：<实际路径>。
<粘贴 ChatGPT 任务书>
默认 GitHub Review：固定 task_id，在确认 base 的独立功能分支修改，保护现有工作。
实际执行测试、扫描拟推送的全部新增 commit 范围，维护 HANDOFF、REVIEW_REPORT、TEST_RESULTS、TASK_BRIEF、CHATGPT_BRIEF。
已有对应明确许可才推送或创建 PR；否则先给我最终 diff、commits、测试与扫描结果和 Draft PR 文案，等待许可。
```

## 给 ChatGPT 审查同一 PR

```text
按 chatgpt-review v2 审查：
仓库：<owner/repo>。任务：<task_id>。PR：<URL>。
期待 head SHA：<完整 SHA>。任务记录路径：<相对路径>。轮次：<n>。
实际读取 PR head/base、五份记录、该 SHA 的完整相关源码与 diff。
核对 TEST_RESULTS 被测版本与当前 head，区分直接检查、执行端报告、推断和未验证。
读不到指定版本时请求固定版本文件或备用 ZIP，不用默认分支替代。
输出有证据的发现和绑定 reviewed_head_sha 的执行指令，继续同一 PR，不自动写评论或合并。
```

## 执行 Agent 处理反馈

```text
继续同 task_id、功能分支和 Draft PR。
<粘贴包含 reviewed_head_sha 的完整反馈>
核对反馈版本与本地现状，保存当前新改动，处理仍适用且已授权的发现。
保存反馈原文、处置与证据，更新五份记录并复测相关行为。
后续推送和 PR 更新须有对应明确许可；没有则完成本地准备并等待。别创建重复 PR。
```

## 双方换会话

给新 ChatGPT：

```text
继续 chatgpt-review v2。仓库：<owner/repo>。task_id：<id>。
同一 PR：<URL>。上次审查 SHA：<SHA>。期待当前 head：<SHA>。
记录路径：<路径>。未入仓库的新反馈：<内容或附件>。
重新读取并核对远端 head 和记录，继续下一步。不要依赖旧聊天记忆或另开 PR。
```

给新执行 Agent：

```text
读取 <skill 实际路径>/SKILL.md，续接项目 <实际路径> 的 task_id <id>。
先读五份记录、反馈和许可范围，再核对本地与同一 PR 的远端版本、未推送改动和待批准内容。
继续明确的下一步，不重做有证据的已完成工作。远端写入仍遵守已有明确许可范围。
```

## 备用 ZIP 审查

上传独立最新包，以及尚未入包的新反馈。

```text
本轮 GitHub 内容不可读，按上传 ZIP 中 CHATGPT_START、GITHUB_REVIEW 和 REVIEW_PROTOCOL 备用续接。
读取 PACKAGE_INFO 和五份任务记录，确认 task_id、round、package_id。
审查完整相关源码和本轮实际证据，缺材料给收集任务。
输出绑定此包的反馈。旧历史摘要是执行端报告，不是你直接检查过的旧日志。
```
