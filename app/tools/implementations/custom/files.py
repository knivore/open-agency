from __future__ import annotations

from pathlib import Path
from pydantic import BaseModel, Field
from typing import Any


class FileWriteInput(BaseModel):
    content: str = Field(..., description="The content to write")
    mode: str = Field(..., description="Either 'write' or 'append'")
    filename: str | None = Field(default=None, description="Optional filename override")


def write_text_file(
        content: str,
        mode: str,
        base_folder: str,
        filename: str | None = None,
        default_filename: str | None = None,
) -> dict[str, Any]:
    base_path = Path(base_folder).expanduser().resolve()
    base_path.mkdir(parents=True, exist_ok=True)
    chosen_file = filename or default_filename
    if not chosen_file:
        raise ValueError("No filename specified and no default file set.")

    full_path = (base_path / chosen_file).resolve()
    try:
        full_path.relative_to(base_path)
    except ValueError:
        raise ValueError("Access outside the base directory is not allowed.")

    full_path.parent.mkdir(parents=True, exist_ok=True)
    with full_path.open("a" if mode == "append" else "w", encoding="utf-8") as handle:
        handle.write(content)
    return {
        "status": "success",
        "message": f"Content successfully {'appended to' if mode == 'append' else 'written to'} {full_path}",
        "path": str(full_path),
    }
