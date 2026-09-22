import yaml
import difflib
import html


def safe_parse(text):
    """Parse YAML safely; fall back to raw text."""
    try:
        return yaml.safe_load(text) or {}
    except Exception:
        return None


def highlight_inline_changes(a_line: str, b_line: str):
    """Return HTML with inline character-level diff."""
    sm = difflib.SequenceMatcher(None, a_line, b_line)
    a_out = []
    b_out = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            a_out.append(html.escape(a_line[i1:i2]))
            b_out.append(html.escape(b_line[j1:j2]))

        elif tag == "replace":
            a_out.append(f'<span class="char-diff">{html.escape(a_line[i1:i2])}</span>')
            b_out.append(f'<span class="char-diff">{html.escape(b_line[j1:j2])}</span>')

        elif tag == "delete":
            a_out.append(f'<span class="char-diff">{html.escape(a_line[i1:i2])}</span>')

        elif tag == "insert":
            b_out.append(f'<span class="char-diff">{html.escape(b_line[j1:j2])}</span>')

    return "".join(a_out), "".join(b_out)


def generate_combined_diff(a_text: str, b_text: str):
    """
    Create unified paired diff with inline character highlighting.
    """

    # -------------------------
    # STRUCTURED YAML CHANGES
    # -------------------------
    parsed_a = safe_parse(a_text)
    parsed_b = safe_parse(b_text)

    structured_changes = []
    if isinstance(parsed_a, dict) and isinstance(parsed_b, dict):
        keys = sorted(set(parsed_a.keys()) | set(parsed_b.keys()))
        for k in keys:
            if parsed_a.get(k) != parsed_b.get(k):
                structured_changes.append(k)

    # -------------------------
    # UNIFIED DIFF
    # -------------------------
    udiff = list(difflib.unified_diff(
        a_text.splitlines(),
        b_text.splitlines(),
        lineterm=""
    ))

    skip_headers = ("---", "+++", "@@")

    diff_rows = []
    additions = deletions = modifications = 0

    i = 0
    while i < len(udiff):
        line = udiff[i]

        # Skip unified diff header lines
        if any(line.startswith(h) for h in skip_headers):
            i += 1
            continue

        # -----------------------------------
        # MODIFICATION: -old followed by +new
        # -----------------------------------
        if (
            line.startswith("-")
            and i + 1 < len(udiff)
            and udiff[i + 1].startswith("+")
        ):
            a_val = line[1:]
            b_val = udiff[i + 1][1:]

            a_html, b_html = highlight_inline_changes(a_val, b_val)

            diff_rows.append({
                "css": "diff-change",
                "a": a_html,
                "b": b_html,
            })

            modifications += 1
            i += 2
            continue

        # -------------------------
        # PURE DELETION
        # -------------------------
        if line.startswith("-"):
            diff_rows.append({
                "css": "diff-remove",
                "a": f'<span class="char-diff">{html.escape(line[1:])}</span>',
                "b": "",
            })
            deletions += 1
            i += 1
            continue

        # -------------------------
        # PURE ADDITION
        # -------------------------
        if line.startswith("+"):
            diff_rows.append({
                "css": "diff-add",
                "a": "",
                "b": f'<span class="char-diff">{html.escape(line[1:])}</span>',
            })
            additions += 1
            i += 1
            continue

        # -------------------------
        # UNCHANGED LINE
        # -------------------------
        if line.startswith(" "):
            val = line[1:]

            # Ignore blank unchanged rows
            if val.strip() == "":
                i += 1
                continue

            escaped = html.escape(val)

            diff_rows.append({
                "css": "",
                "a": escaped,
                "b": escaped,
            })

            i += 1
            continue

        i += 1

    # -------------------------
    # SUMMARY
    # -------------------------
    summary = f"{additions} additions, {deletions} deletions, {modifications} modifications"
    if structured_changes:
        summary = f"Changed YAML keys: {', '.join(structured_changes)} — {summary}"

    return diff_rows, summary
