"""Shared utility functions for vigil."""


def safe_truncate(text: str, max_chars: int = 1500) -> str:
    """Return text fully intact if within budget; truncate at word boundary if anomalously long.

    Applied per individual data row to preserve message integrity.
    Only activates on extreme outliers (e.g., pasted logs or scripts)
    while leaving 99% of normal conversational messages completely untouched.

    Args:
        text: The text content to evaluate.
        max_chars: Maximum character threshold per row. Defaults to 1500.

    Returns:
        Original text if len <= max_chars, otherwise truncated at the
        nearest word boundary with an explicit truncation indicator.
    """
    if not text or len(text) <= max_chars:
        return text

    snippet = text[:max_chars]
    last_space = snippet.rfind(" ")

    # Only cut back to word boundary if the space is reasonably close to the limit
    # (within the last 200 chars) to avoid discarding huge chunks of unbroken text.
    min_keep = max(0, max_chars - 200)
    if last_space >= min_keep:
        truncated = snippet[:last_space].rstrip()
    else:
        truncated = snippet.rstrip()

    return f"{truncated}... [terpotong, total: {len(text)} karakter]"


def markdown_to_telegram_html(text: str) -> str:
    """Convert common LLM Markdown formatting into Telegram-compatible HTML.

    Features:
    - Code blocks (```lang\ncode\n``` or unclosed ``` at EOF) -> <pre><code class="language-lang">...</code></pre>
    - Inline code (`code`) -> <code>...</code>
    - Headers (#, ##, ###) -> <b>...</b>
    - Bold+Italic (***...***, ___...___) -> <b><i>...</i></b>
    - Bold (**...**, __...__) -> <b>...</b>
    - Italic (*...*, _..._) with word-boundary awareness -> <i>...</i>
    - Strikethrough (~~...~~) -> <s>...</s>
    - Links ([text](url)) -> <a href="url">text</a>
    - Bullet list markers (- or *) at start of line -> •
    - Complete protection of code blocks from markdown translation.
    - Sanitizes raw HTML entities (<, >, &) to prevent Telegram entity errors.
    """
    if not text:
        return text if text is not None else ""

    import html
    import re

    placeholders: dict[str, str] = {}
    counter = 0

    # 1. Extract fenced code blocks: ```lang\ncode\n``` or unclosed at EOF
    def replace_code_block(match: re.Match) -> str:
        nonlocal counter
        key = f"\x00CODEBLOCK_{counter}\x00"
        counter += 1
        lang = (match.group(1) or "").strip()
        code = match.group(2)
        if code.endswith("\n"):
            code = code[:-1]
        escaped_code = html.escape(code)
        if lang:
            placeholders[key] = f'<pre><code class="language-{html.escape(lang)}">{escaped_code}</code></pre>'
        else:
            placeholders[key] = f'<pre><code>{escaped_code}</code></pre>'
        return key

    # Matches ```lang\ncode``` or truncated at EOF
    code_block_pattern = re.compile(r"```([a-zA-Z0-9_\-\+]*)\n?(.*?)(?:```|$)", re.DOTALL)
    processed = code_block_pattern.sub(replace_code_block, text)

    # 2. Extract inline code: `code`
    def replace_inline_code(match: re.Match) -> str:
        nonlocal counter
        key = f"\x00INLINECODE_{counter}\x00"
        counter += 1
        code = match.group(1)
        placeholders[key] = f"<code>{html.escape(code)}</code>"
        return key

    inline_code_pattern = re.compile(r"`([^`\n]+)`")
    processed = inline_code_pattern.sub(replace_inline_code, processed)

    # 3. Escape HTML special characters in the remaining normal text
    processed = html.escape(processed)

    # 4. Convert Markdown headers to <b>...</b>
    processed = re.sub(r"(?m)^#{1,6}\s+(.+?)$", r"<b>\1</b>", processed)

    # 5. Convert bullet points at start of line: - item or * item -> • item
    processed = re.sub(r"(?m)^[\*\-]\s+(.+?)$", r"• \1", processed)

    # 6. Convert Bold+Italic (Triple asterisks / underscores): ***text*** or ___text___
    processed = re.sub(r"\*\*\*(.+?)\*\*\*", r"<b><i>\1</i></b>", processed)
    processed = re.sub(r"___(.+?)___", r"<b><i>\1</i></b>", processed)

    # 7. Convert Bold: **text** or __text__
    processed = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", processed)
    processed = re.sub(r"__(.+?)__", r"<b>\1</b>", processed)

    # 8. Convert Strikethrough: ~~text~~
    processed = re.sub(r"~~(.+?)~~", r"<s>\1</s>", processed)

    # 9. Convert Italic: *text* or _text_
    # Must not match snake_case variables (e.g. user_id or my_long_var)
    processed = re.sub(r"(?<!\w)\*([^\*\n]+?)\*(?!\w)", r"<i>\1</i>", processed)
    processed = re.sub(r"(?<!\w)_([^_\n]+?)_(?!\w)", r"<i>\1</i>", processed)

    # 10. Convert Links: [label](url)
    processed = re.sub(r"\[(.+?)\]\((https?://[^\s\)]+)\)", r'<a href="\2">\1</a>', processed)

    # 11. Restore placeholders
    for key, val in placeholders.items():
        processed = processed.replace(key, val)

    return processed
