from itertools import islice
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


# def list_files(directory: Path | str) -> list[Path]:
#     """返回目录下的直属普通文件，结果为绝对路径，不递归子目录。"""
#     directory = Path(directory).expanduser().resolve()
#     if not directory.exists():
#         raise FileNotFoundError(f"路径不存在：{directory}")
#     if not directory.is_dir():
#         raise NotADirectoryError(f"路径不是目录路径：{directory}")

#     return [item for item in directory.iterdir() if item.is_file()]

@tool
def read(file: Path | str, offset: int = 1, limit: bool = False) -> str:
    """读取 UTF-8 文件；offset 从 1 开始，limit=True 时最多读取 50 行。

    空文件或 offset 超出文件行数时返回空字符串。
    """
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise TypeError("offset 必须是整数")
    if offset < 1:
        raise ValueError("offset 必须大于等于1")
    if not isinstance(limit, bool):
        raise TypeError("limit 必须是布尔值")

    file = Path(file).expanduser().resolve()
    _check_file(file)

    with file.open("r", encoding="utf-8") as stream:
        lines = (
            line for number, line in enumerate(stream, start=1) if number >= offset
        )
        return "".join(islice(lines, 50) if limit else lines)



