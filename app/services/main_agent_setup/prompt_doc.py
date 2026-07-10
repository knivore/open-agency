from __future__ import annotations

from pathlib import Path

PROMPT_DOC = Path(__file__).resolve().parents[3] / "docs" / "main-agent.md"


def extract_prompt_from_doc(path: Path = PROMPT_DOC) -> str:
    content = path.read_text(encoding="utf-8")
    marker = "## Prompt"
    start = content.index(marker)
    fence_start = content.index("```markdown", start) + len("```markdown")
    fence_end = content.index("```", fence_start)
    return content[fence_start:fence_end].strip()
