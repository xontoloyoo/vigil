import pytest
from src.core.utils import safe_truncate


def test_safe_truncate_none_or_empty():
    assert safe_truncate("") == ""
    assert safe_truncate(None) is None


def test_safe_truncate_under_limit():
    short_text = "Pesan obrolan pendek yang normal."
    assert safe_truncate(short_text, max_chars=1500) == short_text


def test_safe_truncate_exact_limit():
    exact_text = "a" * 1500
    assert safe_truncate(exact_text, max_chars=1500) == exact_text


def test_safe_truncate_exceeds_limit_cuts_at_word_boundary():
    # Construct a sentence exceeding 1500 characters
    prefix = "kata " * 310  # 310 * 5 = 1550 chars
    total_len = len(prefix)
    truncated = safe_truncate(prefix, max_chars=1500)

    assert truncated.endswith(f"... [terpotong, total: {total_len} karakter]")
    # Verify word boundary: should not end with a partial word before the indicator
    content_part = truncated.split("... [terpotong")[0]
    assert content_part.endswith("kata")
    assert len(content_part) <= 1500


def test_safe_truncate_continuous_string_without_spaces():
    long_continuous = "X" * 2000
    truncated = safe_truncate(long_continuous, max_chars=1500)
    assert truncated.startswith("X" * 1500)
    assert truncated.endswith("... [terpotong, total: 2000 karakter]")


def test_safe_truncate_space_far_back_falls_back_to_max_chars():
    # Space only at index 10, then unbroken characters up to 1800
    text = "Error log: " + ("Z" * 1800)
    total_len = len(text)
    truncated = safe_truncate(text, max_chars=1500)
    content_part = truncated.split("... [terpotong")[0]
    # It should not roll back to index 10 (which would be len ~10),
    # but instead preserve up to 1500 chars
    assert len(content_part) == 1500
    assert truncated.endswith(f"... [terpotong, total: {total_len} karakter]")


def test_per_row_truncation_isolation():
    # Simulate 3 database rows
    row1 = "Baris 1 pendek dan utuh."
    row2 = "Baris 2 panjang " + ("detail " * 300)  # > 1500 chars
    row3 = "Baris 3 catatan penutup."

    rows = [row1, row2, row3]
    processed_rows = [safe_truncate(r, max_chars=1500) for r in rows]

    # Row 1 and Row 3 must be completely unchanged
    assert processed_rows[0] == row1
    assert processed_rows[2] == row3

    # Row 2 must have truncation indicator per row
    assert "... [terpotong, total:" in processed_rows[1]
    assert processed_rows[1].startswith("Baris 2 panjang")


# ============================================================================
# TESTS FOR markdown_to_telegram_html
# ============================================================================

from src.core.utils import markdown_to_telegram_html


def test_markdown_to_telegram_html_standard_formatting():
    """Verify standard markdown features are accurately translated to Telegram HTML."""
    # 1. Headers
    assert markdown_to_telegram_html("# Judul 1") == "<b>Judul 1</b>"
    assert markdown_to_telegram_html("## Subjudul 2") == "<b>Subjudul 2</b>"
    assert markdown_to_telegram_html("### Bagian 3") == "<b>Bagian 3</b>"

    # 2. Bold and Strikethrough
    assert markdown_to_telegram_html("Teks **tebal** dan __tebal juga__") == "Teks <b>tebal</b> dan <b>tebal juga</b>"
    assert markdown_to_telegram_html("Teks ~~dicoret~~") == "Teks <s>dicoret</s>"

    # 3. Italic and Nested Bold+Italic
    assert markdown_to_telegram_html("Teks *miring* dan _miring juga_") == "Teks <i>miring</i> dan <i>miring juga</i>"
    assert markdown_to_telegram_html("***tebal dan miring***") == "<b><i>tebal dan miring</i></b>"
    assert markdown_to_telegram_html("**_gabungan format_**") == "<b><i>gabungan format</i></b>"

    # 4. Inline code
    assert markdown_to_telegram_html("Gunakan `var_name = 1` disini") == "Gunakan <code>var_name = 1</code> disini"

    # 5. Fenced code block with language
    code_input = "```python\ndef halo():\n    return 'dunia'\n```"
    expected_code = '<pre><code class="language-python">def halo():\n    return &#x27;dunia&#x27;</code></pre>'
    assert markdown_to_telegram_html(code_input) == expected_code

    # 6. Links and Bullets
    link_input = "Kunjungi [Situs Kami](https://example.com/info)"
    assert markdown_to_telegram_html(link_input) == 'Kunjungi <a href="https://example.com/info">Situs Kami</a>'

    bullet_input = "- Poin pertama\n* Poin kedua"
    assert markdown_to_telegram_html(bullet_input) == "• Poin pertama\n• Poin kedua"


def test_markdown_to_telegram_html_adversarial_and_extreme_cases():
    """Verify robustness under extreme, adversarial, and malformed inputs."""
    # 1. None, empty, whitespace
    assert markdown_to_telegram_html("") == ""
    assert markdown_to_telegram_html(None) == ""
    assert markdown_to_telegram_html("   ") == "   "

    # 2. Unclosed markdown delimiters (must not crash, must remain intact)
    assert markdown_to_telegram_html("Teks **bold tanpa tutup") == "Teks **bold tanpa tutup"
    assert markdown_to_telegram_html("Teks *italic tanpa tutup") == "Teks *italic tanpa tutup"
    assert markdown_to_telegram_html("Teks `inline code tanpa tutup") == "Teks `inline code tanpa tutup"

    # 3. Truncated code block at EOF (e.g. LLM hitting max tokens mid-generation)
    truncated_code = "```python\ndef hitung():\n    x = 10 < 20"
    res_code = markdown_to_telegram_html(truncated_code)
    assert '<pre><code class="language-python">def hitung():\n    x = 10 &lt; 20</code></pre>' in res_code

    # 4. HTML/XSS injection attempts (must be escaped into entities, not raw HTML tags)
    xss_input = "<script>alert('xss')</script> dan <img src=x onerror=alert(1)> serta <custom_tag>hai</custom_tag>"
    xss_output = markdown_to_telegram_html(xss_input)
    assert "<script>" not in xss_output
    assert "&lt;script&gt;" in xss_output
    assert "<img" not in xss_output
    assert "&lt;img" in xss_output
    assert "&lt;custom_tag&gt;" in xss_output

    # 5. Math inequalities and boolean operators (&, <, >)
    math_input = "Kondisi: jika a < 10 dan b > 20 serta c & d == True"
    math_output = markdown_to_telegram_html(math_input)
    assert "a &lt; 10" in math_output
    assert "b &gt; 20" in math_output
    assert "c &amp; d" in math_output

    # 6. Snake_case identifiers (must NOT be corrupted into italics)
    snake_input = "Variabel user_account_id, update_user_profile, dan file_path_v2"
    snake_output = markdown_to_telegram_html(snake_input)
    assert "<i>" not in snake_output
    assert "user_account_id" in snake_output
    assert "update_user_profile" in snake_output

    # 7. Code block with markdown characters inside (markdown must NOT be parsed inside code)
    block_with_md = (
        "```python\n"
        "# **ini bukan tebal**\n"
        "# `ini bukan inline code`\n"
        "if x < 10 and y > 20:\n"
        "    return a & b\n"
        "```"
    )
    block_output = markdown_to_telegram_html(block_with_md)
    assert "<b>" not in block_output  # **ini bukan tebal** must stay literal
    assert "&lt; 10" in block_output  # < must be escaped
    assert "&gt; 20" in block_output  # > must be escaped
    assert "&amp; b" in block_output  # & must be escaped

    # 8. Complex link with URL query parameters containing '&' and anchors
    complex_link = "[Pencarian](https://example.com/api?query=halo%20dunia&page=2&sort=desc#top)"
    link_output = markdown_to_telegram_html(complex_link)
    assert '<a href="https://example.com/api?query=halo%20dunia&amp;page=2&amp;sort=desc#top">Pencarian</a>' == link_output
