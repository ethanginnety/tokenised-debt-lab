"""Guards against bugs that only show up on some operating systems."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEXT_IO = {"read_text", "write_text", "open"}


def _calls_without_encoding(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if name not in TEXT_IO:
            continue
        if name == "open" and any(
            isinstance(a, ast.Constant) and isinstance(a.value, str) and "b" in a.value
            for a in node.args[1:2]
        ):
            continue  # binary mode needs no encoding
        if not any(k.arg == "encoding" for k in node.keywords):
            found.append(f"{path.relative_to(ROOT)}:{node.lineno} {name}()")
    return found


def test_text_files_always_use_explicit_utf8():
    # Without encoding=..., Python uses the OS default: UTF-8 on Linux/macOS but
    # cp1252 on Windows, which garbles characters like "·" and "−".
    offenders = []
    for folder in ("debtlab", "scripts", "tests"):
        for py in (ROOT / folder).rglob("*.py"):
            offenders += _calls_without_encoding(py)
    assert offenders == [], "add encoding='utf-8' to: " + ", ".join(offenders)
