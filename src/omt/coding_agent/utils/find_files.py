# find a fold's all files
from pathlib import Path

def find_files(root: str | Path, pattern: str) -> list[Path]:
    root = Path(root).expanduser().resolve()

    if not root.exists():
        raise FileNotFoundError(f"目录不存在 {root}")

    if not root.is_dir():
        raise FileExistsError(f"不是目录 {root}")

    ignored = {".git", ".venv", "__pycache__"}
    result: list[Path] = []

    for file in root.glob(pattern=pattern):
        if not file.is_file():
            continue

        parent = file.relative_to(root).parent.parts

        if ignored.intersection(parent):
            continue

        result.append(file)

    return sorted(result)