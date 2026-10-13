from pathlib import Path

from langchain.tools import tool


def _check_file(file: Path) -> None:
    """确认路径指向已存在的普通文件。"""
    if not file.exists():
        raise FileNotFoundError(f"路径不存在：{file}")
    if file.is_dir():
        raise IsADirectoryError(f"路径指向目录：{file}")
    if not file.is_file():
        raise ValueError(f"路径不是普通文件：{file}")


@tool
def edit(
    file: Path | str, old_string: str, new_string: str, replace_all: bool
) -> None:
    """替换文件内的文本；空匹配、多处匹配未授权时拒绝修改。

    old_string 不能为空；new_string 可以为空，表示删除匹配文本。
    """
    if not isinstance(replace_all, bool):
        raise TypeError("replace_all 必须是布尔值")
    if old_string == "":
        raise ValueError("替换文本不能为空")

    file = Path(file).expanduser().resolve()
    _check_file(file)
    content = file.read_text(encoding="utf-8")
    match_count = content.count(old_string)
    if match_count == 0:
        raise ValueError("未找到匹配文本")
    if match_count > 1 and not replace_all:
        raise ValueError(f"匹配到 {match_count} 处，请提供更完整的文本")

    new_content = content.replace(old_string, new_string)
    file.write_text(new_content, encoding="utf-8")


