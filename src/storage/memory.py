"""Markdown memory storage operations for vigil.

Handles reading, atomic overwriting, and FIFO character-capped appending
for structured Markdown logs (e.g., data/USER.md, data/MEMORY.md).
"""

from pathlib import Path
import uuid


DEFAULT_USER_MD = Path("data/USER.md")
DEFAULT_MEMORY_MD = Path("data/MEMORY.md")


def read_markdown(file_path: Path | str) -> str:
    """Safely read markdown file content.

    If the file does not exist, creates an empty file and returns an empty string.
    """
    path = Path(file_path)
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        return ""
    return path.read_text(encoding="utf-8")


def overwrite_markdown(file_path: Path | str, content: str) -> None:
    """Atomically overwrite markdown file content."""
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_file = path.with_suffix(f"{path.suffix}.tmp_{uuid.uuid4().hex[:6]}")
    try:
        temp_file.write_text(content, encoding="utf-8")
        temp_file.replace(path)
    except Exception:
        if temp_file.exists():
            temp_file.unlink(missing_ok=True)
        # Direct write fallback if atomic rename encounters file lock
        path.write_text(content, encoding="utf-8")


def append_markdown(
    file_path: Path | str, entry: str, max_chars: int = 2000
) -> None:
    """Append a new text entry to target markdown file with FIFO character capping.

    If the resulting content exceeds `max_chars`, earliest body lines are trimmed
    (FIFO) while preserving top-level header structure (# ...) to keep token size bounded.
    """
    path = Path(file_path)
    existing = read_markdown(path)

    # Format incoming entry
    clean_entry = entry.rstrip("\r\n") + "\n"
    if existing and not existing.endswith("\n"):
        existing += "\n"

    full_content = existing + clean_entry

    if len(full_content) <= max_chars:
        overwrite_markdown(path, full_content)
        return

    # Parse headers vs body lines
    lines = full_content.splitlines(keepends=True)
    header_lines: list[str] = []
    body_lines: list[str] = []
    in_header = True

    for line in lines:
        stripped = line.strip()
        if in_header:
            if stripped.startswith("#"):
                header_lines.append(line)
            elif not stripped and header_lines:
                # Blank line following a header is kept as part of header block
                header_lines.append(line)
            else:
                in_header = False
                body_lines.append(line)
        else:
            body_lines.append(line)

    header_len = sum(len(h) for h in header_lines)
    if header_len >= max_chars:
        # Header itself exceeds budget; retain truncated header
        overwrite_markdown(path, "".join(header_lines)[:max_chars])
        return

    available_for_body = max_chars - header_len

    # Drop oldest body lines (FIFO) until within budget
    while body_lines and sum(len(b) for b in body_lines) > available_for_body:
        if len(body_lines) == 1:
            # Single remaining line exceeds budget; retain its most recent characters
            body_lines = [body_lines[0][-available_for_body:]]
            break
        body_lines.pop(0)

    final_content = "".join(header_lines) + "".join(body_lines)
    overwrite_markdown(path, final_content)


def remove_markdown_bullet(file_path: Path | str, text_snippet: str) -> bool:
    """Remove line(s) from a markdown file that contain the given text snippet (case-insensitive).

    Returns True if at least one line was removed, False otherwise.
    """
    path = Path(file_path)
    if not path.is_file():
        return False
    content = read_markdown(path)
    if not content:
        return False
    lines = content.splitlines()
    snippet_lower = text_snippet.strip().lower()
    new_lines = [line for line in lines if snippet_lower not in line.lower()]
    if len(new_lines) != len(lines):
        overwrite_markdown(path, "\n".join(new_lines) + ("\n" if new_lines else ""))
        return True
    return False
