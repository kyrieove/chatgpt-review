"""Generate the standalone ChatGPT entry point from the maintained references."""

from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    refs = root / "skills" / "chatgpt-review" / "references"
    content = (
        "# chatgpt-review 首次启动\n\n"
        "将本文件上传给 ChatGPT，描述你的目标，请它按协调与审查端流程生成任务书。"
        "本文由 references/chatgpt.md、github.md 和 contract.md 自动生成。"
        "后续上传的审查 ZIP 自带这两份说明。\n\n"
        + (refs / "chatgpt.md").read_text(encoding="utf-8")
        + "\n\n"
        + (refs / "github.md").read_text(encoding="utf-8")
        + "\n\n"
        + (refs / "contract.md").read_text(encoding="utf-8")
    )
    target = root / "CHATGPT_START.md"
    with target.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
    print(f"CHATGPT_START={target}")


if __name__ == "__main__":
    main()
