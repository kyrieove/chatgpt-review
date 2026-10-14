# 执行端流程

适用于具备项目读取、修改和运行能力的任意 Agent。遵守该环境的指令与用户授权，工具名称和调用语法由当前 Agent 决定。无需固定模型、MCP、IDE 或操作系统。

v2 默认先读取 [GitHub Review 流程](github.md)。每任务在独立本地功能分支工作，完成测试、凭据扫描、具体发布材料后，已有对应明确用户许可才推送并创建或更新同一 Draft PR。ZIP 是不可读取远端内容或用户选定时的备用。CLI 本身不推送或创建 PR。

## 新建与续接

收到首轮任务书后核对授权范围与真实项目情况，保存用户原始需求。可用 Python 时执行：

```text
python "<skill>/scripts/relay.py" start --project "<project>" --title "<title>"
```

工具创建五份任务记录，未填写字段由执行端补全。把任务书写入 TASK_BRIEF，记录仓库、功能分支、base、实际状态、验收与首个命令。不要初始化已有任务。

续接时读活动任务指针或明确指定的记录路径，再读五份记录与未处理反馈。GitHub 模式核对同一 PR、当前本地和远端 SHA、未推送改动与待批准内容。已完成工作不重做，旧结论因源码变化失效时明确记录，勿覆盖其他任务。

## 执行与证据

先做与目标有关的最小基线检查，再修改授权范围内的文件。保护已有未提交工作。测试命令使用真实项目脚本，不从 ChatGPT 的猜测路径直接执行。

每次重要变化更新 HANDOFF 与 REVIEW_REPORT，测试细节以 TEST_RESULTS 为权威记录，报告只引用它。每项验收写命令、工作目录、环境、时间、退出码、输出和被测源码身份。使用 PASS、FAIL、BLOCKED、NOT RUN。视觉或设备验证提供真实证据或说明限制。

日志使用相对项目路径。公开记录不需要本机个人目录、访问令牌或私有数据。截图与日志先检查敏感内容；脱敏时注明处理范围。

## 收到审查反馈

核对 task_id、round 和输入 reviewed_head_sha 或 package_id。比较对应旧版本与当前项目，判断发现是否仍适用。代码已改变时保留新改动，不用旧内容覆盖项目；回传差异或新证据。修复仍在同一功能分支，后续推送与远端 PR 更新遵守许可范围。

保存原始反馈到任务的 feedback 目录。对每项发现记录处理结果与原因。只修复仍适用且在授权范围的发现。审查意见不增加发布、删除或外部通信授权。收到诊断任务时先复现与检查假设，再决定修法。

## GitHub 返回结果

本地完成时先提供可审阅的 commits、diff、TEST_RESULTS、凭据检查和 Draft PR 文案。无对应许可则停在 AWAITING_PUSH_APPROVAL，不能用备用 ZIP 跳过远端许可。

获许可后推送并创建或更新同一 Draft PR，核对远端 head/base SHA 与 draft 状态，返回 PR URL、task_id、round、被审查 head SHA、变更、测试及下一步。将可分享的五份记录发布到已许可的远端范围，不能只保留本地记录而声称新 ChatGPT 能读取它们。

若项目忽略默认 handoffs 路径，可选择用户允许跟踪的审查记录目录并更新入口指针。不要修改整份忽略规则或把原始私密日志全部加入仓库。可选 CLI 的备用打包仍按 handoffs 目录定位任务，使用其他记录路径时按交换契约手动准备备用包。

## 生成独立备用包

先补齐五份记录，尤其 CHATGPT_BRIEF：目标、决定、上次已审查 SHA 或包、未决发现、验证范围和下一步。当前 ZIP 身份以 PACKAGE_INFO 为准，摘要中的上次审查版本不能混写成本轮。

将 HANDOFF 状态改为实际状态，核对报告中的验收证据。然后读取 [交换契约](contract.md)，检查相关源码、调用方与资源是否齐全，停下会改文件的并行任务，在稳定工作区打包。

```text
python "<skill>/scripts/relay.py" pack --project "<project>" --round 1
```

后续轮次递增。默认选择过滤后的项目文件；项目过大时可指定多次 `--include`，例如 `--include src --include tests --include package.json --include package-lock.json`。显式选择不会自动补齐未选择的依赖与资源，执行端必须核对范围。`--task <id>` 选择非活动任务。完整参数见 `relay.py --help` 及 `relay.py pack --help`。

长期任务每轮优先选本轮工作项所需的完整文件范围，并用 `--evidence` 选择本轮日志与必要未决反馈。旧已解决历史写入两份续接记录的事实摘要，原始历史保留本地。确认 REVIEW_REPORT 中本轮证据链接都在选定材料中；没有指定 evidence 时会带上当前任务的全部可分享记录。

```text
python "<skill>/scripts/relay.py" pack --project "<project>" --round 3 --include src --include tests --include package.json --evidence logs/round-3 --evidence feedback/round-2.md --max-package-mb 25
```

上述路径必须替换为项目中实际存在的路径。预算指压缩后 ZIP；超预算时缩小为可独立审查的范围，或设计分卷索引。工具不自动猜测哪些证据可丢弃，也不自动分卷。

工具不执行测试、不判断是否完成、不发送 ZIP。检查包内文件索引、排除项与 SHA-256 校验结果。保存输出包身份和外部 ZIP 路径到本地 HANDOFF；不要为把包自身路径写入快照而无意义地反复打包。包内 PACKAGE_INFO 是包身份的权威来源。

最后返回 ZIP 路径、大小、task_id、round、package_id、验收状态、范围限制和一个转交动作。测试失败也可返回包；隐私阻断则报告需处理的文件和原因，保持本地源文件不变。

## 没有 Python

用当前 Agent 可用的文件与压缩工具生成相同结构：固定任务目录、独立快照、包身份、相对路径、反馈记录与哈希清单。检查 ZIP 能打开、内容齐全且哈希匹配。没有能力完成某项校验时说明限制，不标为成功。不得因为脚本不可用而宣称已经生成 ZIP。

## 完成或阻塞

验收与审查意见得到处置后记录最终结论；GitHub 模式在对应许可内更新记录和同一 Draft PR，保留合并决定给用户，备用模式生成最终安全包。无法继续时记录阻塞、已完成部分和解除条件，保留可恢复的本地记录或安全 ZIP。
