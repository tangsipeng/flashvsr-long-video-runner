from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def write_text_atomic(path: str | Path, text: str, *, encoding: str = "utf-8") -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.tmp")
    temporary.write_text(text, encoding=encoding)
    temporary.replace(target)
    return target


def write_json_atomic(path: str | Path, payload: Any, *, ensure_ascii: bool = False, indent: int = 2) -> Path:
    text = json.dumps(payload, ensure_ascii=ensure_ascii, indent=indent)
    return write_text_atomic(path, text)
