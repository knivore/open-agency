from __future__ import annotations

from pathlib import Path


def read_text_file(path: str, encoding: str = "utf-8") -> dict[str, str]:
    content = Path(path).read_text(encoding=encoding)
    return {"content": content}
