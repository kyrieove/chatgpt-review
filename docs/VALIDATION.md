# 验证记录

日期：2026-10-15。最终方案为 chatgpt-review v2，GitHub Review 默认，独立 ZIP 备用。

## 实际运行

在 Windows、Python 3.12 上运行：

```text
python -B -m unittest discover -s tests -v
```

17 项测试，15 项通过，2 项因主机不能创建目录符号链接而跳过，退出码 0。

覆盖任务初始化与指针保护、五份必需记录、两轮独立包与字节哈希、非 Git 项目、未跟踪源码、已暂存及未暂存 diff、子项目边界、环境变量文件与数据库排除、显式源码及证据范围、凭据阻断、无效路径与缺文件、压缩后超预算阻断并保留旧包。

skill-creator 的 quick_validate 对 skills/chatgpt-review 返回 `Skill is valid!`。生成的 CHATGPT_START 与 chatgpt、github、contract 三份权威参考一致。

待发布的 21 份文件完成凭据模式扫描，0 项警报；Git diff --check 通过。公开源码不含原蓝本目录、任务记录、审查 ZIP、缓存或本机凭据。

## 源码发布核对

用户此前明确要求将 skill 上传公开 Git 仓库，本次仅执行这一源码发布。仓库为 kyrieove/chatgpt-review，已查询确认 visibility=PUBLIC、默认分支 main、远端 SHA 与本地相同，主 skill 文件的远端 blob 哈希与本地相同。未创建、合并或更新任何用户开发任务 PR。

## 独立审查

独立审查者核对了真实文件及场景输出，最终没有未解决的实质发现。场景包括目标 SHA 不可读、批准后内容变化、PR head 在审查中前进、测试版本与证据记录 commit 不同、CI artifact 不可读、双方换会话、审查争议及连续两轮修复无效。后者会生成诊断任务，不重复同一修法。

工具审查发现并修复了两类隐私问题：Git 采集越出子项目边界，以及被排除文件通过 binary diff 再次进入包。对应真实 Git 夹具已加入测试。Git status 的仓库相对路径也已转换为项目相对路径。

## 验证范围

未实测 Python 3.9、原生 macOS 或 Linux。符号链接拒绝路径在本机无法运行完整夹具。GitHub 流程是协议场景审查，未替用户在真实开发项目创建、更新或合并 PR，未在用户的 ChatGPT 连接器上测试读取。连接器读取能力与实际 head 内容仍须每任务核对。

本工具不推送、创建 PR、执行项目测试或发送聊天消息。凭据模式扫描不能替代对机密与个人信息的人工检查。
