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
def write(file: Path | str, content: str) -> None:
    """创建或覆盖 UTF-8 文件；父目录必须存在，空内容会清空文件。

    拒绝向目录、管道等非普通文件写入。
    """
    file = Path(file).expanduser().resolve()
    if not file.parent.exists():
        raise FileNotFoundError(f"父目录不存在：{file.parent}")
    if not file.parent.is_dir():
        raise NotADirectoryError(f"父路径不是目录：{file.parent}")
    if file.exists():
        _check_file(file)

    file.write_text(content, encoding="utf-8")


