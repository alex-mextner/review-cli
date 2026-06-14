"""Markdown -> HTML, rendered SERVER-SIDE, for the spec-web reviewer.

Why a bundled renderer (not pandoc / the `markdown` lib) is the primary path:
the whole point of in-doc navigation is that the spec's OWN internal links
(``[§9.4](#94-…)``, ``[Part 15](#part-15-…)``) must resolve against the heading ids we
emit. Those links use the GitHub slug scheme. pandoc and python-markdown each use a
DIFFERENT slug scheme, so their ids would silently not match the spec's hand-written
anchors. This renderer reproduces the GitHub slug scheme exactly (verified against the
styles master spec's own anchors) so every in-doc link lands.

Assets (``./assets/fig-*.svg|png``) are NOT inlined — they are rewritten to
``/asset/<name>`` and served as real HTTP resources by the server. (Inlining was the bug
the earlier static file had; figures must load over HTTP.)

The renderer is a pragmatic subset (headings, lists, tables, fenced code, blockquotes,
hr, paragraphs, inline code/bold/italic/links/images). It is deliberately the SAME
subset already proven against the master spec — not a full CommonMark implementation.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote as urlquote
from urllib.parse import unquote


@dataclass
class RenderResult:
    """The outcome of rendering a spec: the HTML body + the heading table of contents."""

    html: str
    # (level, text, id) for every heading, in document order — drives the TOC and lets
    # the store re-anchor a comment to a section by id.
    headings: list[tuple[int, str, str]]
    # asset filename -> absolute path on disk, for every figure referenced in the doc that
    # exists next to the spec. The server serves these via /asset/<name>.
    assets: dict[str, Path]


# --------------------------------------------------------------------------- #
# Slug (GitHub-style). Verified to match the styles master spec's own anchors.
# --------------------------------------------------------------------------- #
def slug(text: str) -> str:
    s = text.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = s.replace(" ", "-")
    return s


# Canonical image extension -> MIME map. SINGLE source of truth: render_image only accepts
# (registers for serving) these extensions, and the server serves them with the matching
# MIME. Keeping one map prevents the two from drifting (an accepted ext with no MIME would
# be served as octet-stream and not display).
IMAGE_MIME_TYPES = {
    "svg": "image/svg+xml",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "avif": "image/avif",
    "bmp": "image/bmp",
    "ico": "image/x-icon",
}


# Schemes that may become an active <a href>. A spec from an UNTRUSTED repo must not be
# able to emit a clickable `javascript:`/`data:`/`vbscript:` link that runs script in the
# spec-web origin (and could hit the write APIs). Relative paths and fragments are safe.
_SAFE_SCHEMES = ("http://", "https://", "mailto:", "tel:")


def _is_safe_href(href: str) -> bool:
    h = href.strip()
    if not h:
        return False
    # A code-span sentinel inside the URL means a backtick was in the link destination.
    # Code spans are stashed BEFORE link parsing and restored AFTER the <a> is built, so a
    # restored span could inject raw markup into the href attribute (XSS via
    # `[x](https://e.test/`" onclick="…"`)`). A backtick in a URL is non-standard anyway —
    # refuse to make it an active link (the label still renders as plain text).
    if "\x00CODE" in h:
        return False
    if h.startswith("#") or h.startswith("/") or h.startswith("./") or h.startswith("../"):
        return True
    low = h.lower()
    if low.startswith(_SAFE_SCHEMES):
        return True
    # A bare relative path with no scheme (no leading "scheme:") is safe; a "javascript:"
    # / "data:" / unknown scheme is not. Detect a scheme by an early ":" before any "/".
    colon = h.find(":")
    slash = h.find("/")
    if colon == -1 or (slash != -1 and slash < colon):
        return True  # no scheme component -> relative
    return False


# Inline emphasis (bold/italic) applied to a fragment of ALREADY-escaped text. Shared by
# the paragraph emphasis pass and link-label rendering so a link label gets emphasis
# without exposing the generated <a> tag's href to the greedy emphasis regexes.
def _emphasis(text: str) -> str:
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\w)__([^_]+)__(?!\w)", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\*\w])\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(r"(?<![_\w])_([^_\n]+)_(?!\w)", r"<em>\1</em>", text)
    return text


# The VISIBLE text of an inline markdown fragment (markup reduced to what a reader sees):
# images -> alt, links -> label, code/emphasis -> inner text. Used to compute a heading's
# GitHub slug from its rendered text, not its raw markup.
def _visible_text(text: str) -> str:
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)  # image -> alt
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)  # link -> label
    text = text.replace("`", "")  # code span fences
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"(?<!\w)__([^_]+)__(?!\w)", r"\1", text)
    text = re.sub(r"(?<![\*\w])\*([^*\n]+)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<![_\w])_([^_\n]+)_(?!\w)", r"\1", text)
    return text


class _Renderer:
    """Stateful per-document renderer. One instance per render() call.

    Holds the assets-dir + the collected asset references so figure ``src`` rewriting and
    asset discovery happen in one pass.
    """

    def __init__(self, assets_dir: Path) -> None:
        self.assets_dir = assets_dir
        self.assets: dict[str, Path] = {}
        self._used_ids: dict[str, int] = {}
        self._emitted_ids: set[str] = set()

    # ---- inline ----------------------------------------------------------- #
    def render_inline(self, text: str) -> str:
        # Protect inline code spans first.
        code_spans: list[str] = []

        def stash_code(m: re.Match[str]) -> str:
            code_spans.append(m.group(1))
            return f"\x00CODE{len(code_spans) - 1}\x00"

        text = re.sub(r"`([^`]+)`", stash_code, text)

        # Escape raw HTML in the remaining text (markup tokens are re-injected below).
        text = html.escape(text, quote=False)

        # Images and links produce FINISHED HTML tags (with attributes). The emphasis pass
        # below rewrites `_x_` / `*x*` greedily — if it ran over a generated `<a href=...>`
        # it would corrupt URLs containing `_` or `*` (e.g. `?q=_hi_`). So stash the whole
        # generated tag and restore it AFTER emphasis, leaving only label/alt text exposed
        # to emphasis (the label was rendered with emphasis already inside render_link).
        tags: list[str] = []

        def stash_tag(snippet: str) -> str:
            tags.append(snippet)
            return f"\x00TAG{len(tags) - 1}\x00"

        # Images first: ![alt](src)
        text = re.sub(
            r"!\[([^\]]*)\]\(([^)]+)\)",
            lambda m: stash_tag(self.render_image(m.group(1), m.group(2))),
            text,
        )
        # Links: [text](href)
        text = re.sub(
            r"\[([^\]]+)\]\(([^)]+)\)",
            lambda m: stash_tag(self.render_link(m.group(1), m.group(2))),
            text,
        )

        # Bold then italic (shared helper; link/image tags are stashed so they're safe).
        text = _emphasis(text)

        # Restore link/image tags, then code spans.
        text = re.sub(r"\x00TAG(\d+)\x00", lambda m: tags[int(m.group(1))], text)

        def restore_code(m: re.Match[str]) -> str:
            idx = int(m.group(1))
            return f"<code>{html.escape(code_spans[idx], quote=False)}</code>"

        return re.sub(r"\x00CODE(\d+)\x00", restore_code, text)

    # ---- links ------------------------------------------------------------ #
    def render_link(self, label: str, href: str) -> str:
        """Render a markdown link, sanitizing the href scheme.

        The renderer may be pointed at a spec from an UNTRUSTED repo, so a `javascript:`
        (or `data:`) href must NEVER become an active same-origin link a reviewer could
        click to run script against the write APIs. Only safe schemes (relative, fragment,
        http/https/mailto) produce an `<a>`; anything else renders as plain text.

        `href` and `label` arrive already html.escape(quote=False)'d (the whole text was
        escaped upstream), so `&`/`<`/`>` are entity-encoded — only the attribute
        delimiters `"`/`'` still need escaping (re-escaping would double-encode query
        strings). Emphasis on the label is applied here because the finished tag is stashed
        before the caller's emphasis pass runs (so the pass can't reach the label).
        """
        label_html = _emphasis(label)
        if not _is_safe_href(href):
            return label_html  # drop the unsafe link, keep the visible text
        href_attr = href.replace('"', "&quot;").replace("'", "&#39;")
        target = "" if href.startswith("#") else ' target="_blank" rel="noopener"'
        return f'<a href="{href_attr}"{target}>{label_html}</a>'

    # ---- images / assets -------------------------------------------------- #
    def render_image(self, alt: str, src: str) -> str:
        """Rewrite an image to an HTTP ``/asset/<name>`` reference (never inline).

        The figure's basename is registered so the server can serve it; relative paths
        like ``./assets/fig-1.svg`` collapse to ``fig-1.svg`` under ``/asset/``. A missing
        file renders a visible placeholder instead of a broken image (never crash).
        """
        # `src` arrives html.escape(quote=False)'d (e.g. `a&b.svg` -> `a&amp;b.svg`) and may
        # be URL-encoded in the markdown (`my%20diagram.svg`). Reverse BOTH up front, THEN
        # take the basename — decoding AFTER .name would let an encoded slash
        # (`%2FUsers%2Falice%2Fsecret.svg`) survive into the name and probe an absolute path
        # outside the assets dir (a filesystem-existence oracle). Reject anything that isn't
        # a bare basename after full decode.
        src = unquote(html.unescape(src.strip()))
        # .name AFTER decoding always yields a bare final component (an encoded slash can no
        # longer survive into the name and turn `assets_dir / fname` into an absolute path).
        fname = Path(src.split("?", 1)[0].split("#", 1)[0]).name
        if fname in (".", ".."):
            fname = ""
        # `alt` is ALREADY html.escape(quote=False)'d (this runs from within render_inline
        # after the upstream escape) and may still hold \x00CODE\x00 sentinels — the figure
        # snippet is itself stashed as a TAG and the outer pass restores both TAG and CODE,
        # so DON'T call render_inline again (a fresh empty code_spans -> IndexError on a
        # sentinel, i.e. 500 for `![`x`](...)`). For the alt ATTRIBUTE strip sentinels +
        # escape the delimiter; for the figcaption keep markup via _emphasis (sentinels
        # survive for the outer restore).
        alt_attr = re.sub(r"\x00CODE\d+\x00", "", alt).replace('"', "&quot;")
        disk = self.assets_dir / fname
        # Figures-only: a markdown image must point at an IMAGE file. Otherwise
        # `![x](./assets/notes.txt)` would register an arbitrary non-image file for serving
        # over /asset/. Anything else renders as a missing-figure placeholder, not served.
        ext = fname.lower().rsplit(".", 1)[-1] if "." in fname else ""
        is_image = ext in IMAGE_MIME_TYPES
        if not fname or not is_image or not disk.is_file():
            # Strip any code-span sentinel from the displayed name (a backtick in the src)
            # so the placeholder reads cleanly instead of restoring to a <code> tag.
            shown = re.sub(r"\x00CODE\d+\x00", "", fname or src)
            return (
                f'<figure class="missing-fig">[figure missing: {html.escape(shown)}]'
                f"<figcaption>{_emphasis(alt)}</figcaption></figure>"
            )
        self.assets[fname] = disk
        # Percent-encode the basename for the URL (a figure named "my diagram.svg" must
        # emit /asset/my%20diagram.svg so the browser's encoded request matches; the server
        # unquotes before the disk lookup). `safe=""` so dots/spaces/etc. all encode.
        url = "/asset/" + html.escape(urlquote(fname, safe=""), quote=True)
        return (
            f'<figure class="fig"><img loading="lazy" alt="{alt_attr}" src="{url}">'
            f"<figcaption>{_emphasis(alt)}</figcaption></figure>"
        )

    # ---- headings --------------------------------------------------------- #
    def new_id(self, text: str) -> str:
        # A per-base counter alone collides when a suffixed id equals another heading's
        # natural slug (e.g. `# Foo`, `# Foo`, `# Foo 1` -> foo, foo-1, foo-1). Track EVERY
        # emitted id and advance the counter until the candidate is genuinely unused, so
        # GitHub-style internal links + section re-anchoring never target a dup id.
        base = slug(text)
        candidate = base
        n = self._used_ids.get(base, 0)
        while candidate in self._emitted_ids:
            n += 1
            candidate = f"{base}-{n}"
        self._used_ids[base] = n
        self._emitted_ids.add(candidate)
        return candidate

    # ---- tables ----------------------------------------------------------- #
    def render_table(self, rows: list[str]) -> str:
        def cells(line: str) -> list[str]:
            line = line.strip()
            if line.startswith("|"):
                line = line[1:]
            if line.endswith("|"):
                line = line[:-1]
            return [c.strip() for c in line.split("|")]

        header = cells(rows[0])
        body = [cells(r) for r in rows[2:]]
        out = ['<table class="md-table">', "<thead><tr>"]
        for h in header:
            out.append(f"<th>{self.render_inline(h)}</th>")
        out.append("</tr></thead><tbody>")
        for r in body:
            out.append("<tr>")
            for c in r:
                out.append(f"<td>{self.render_inline(c)}</td>")
            out.append("</tr>")
        out.append("</tbody></table>")
        return "".join(out)

    # ---- lists ------------------------------------------------------------ #
    @staticmethod
    def _list_end(lines: list[str], start: int) -> int:
        i = start
        n = len(lines)
        while i < n:
            line = lines[i]
            if re.match(r"^\s*([-*+]|\d+\.)\s+", line):
                i += 1
                continue
            if not line.strip():
                j = i + 1
                while j < n and not lines[j].strip():
                    j += 1
                if j < n and (re.match(r"^\s+", lines[j]) or re.match(r"^\s*([-*+]|\d+\.)\s+", lines[j])):
                    i = j
                    continue
                return i
            if re.match(r"^\s+\S", line):  # indented continuation
                i += 1
                continue
            return i
        return i

    def render_list(self, lines: list[str], start: int, end: int) -> str:
        items: list[list] = []  # [indent, "ul"|"ol", [content lines]]
        i = start
        while i < end:
            line = lines[i]
            m = re.match(r"^(\s*)([-*+]|\d+\.)\s+(.*)$", line)
            if m:
                indent = len(m.group(1))
                mtype = "ol" if m.group(2)[0].isdigit() else "ul"
                items.append([indent, mtype, [m.group(3)]])
            elif items and line.strip():
                items[-1][2].append(line.strip())
            i += 1

        if not items:
            return ""

        def build(idx: int, base_indent: int) -> tuple[str, int]:
            html_out: list[str] = []
            list_type = items[idx][1] if idx < len(items) else "ul"
            html_out.append(f"<{list_type}>")
            k = idx
            while k < len(items):
                indent, _mtype, content = items[k]
                if indent < base_indent:
                    break
                if indent > base_indent:
                    k += 1
                    continue
                item_html = self.render_inline("\n".join(content))
                child_html = ""
                if k + 1 < len(items) and items[k + 1][0] > indent:
                    child_html, k = build(k + 1, items[k + 1][0])
                html_out.append(f"<li>{item_html}{child_html}</li>")
                k += 1
            html_out.append(f"</{list_type}>")
            return "".join(html_out), k - 1

        result, _ = build(0, items[0][0])
        return result

    # ---- block ------------------------------------------------------------ #
    def render(self, md: str) -> RenderResult:
        lines = md.split("\n")
        out: list[str] = []
        headings: list[tuple[int, str, str]] = []
        i = 0
        n = len(lines)
        para: list[str] = []

        def flush_para() -> None:
            if para:
                joined = "\n".join(para).strip()
                if joined:
                    out.append(f"<p>{self.render_inline(joined)}</p>")
                para.clear()

        while i < n:
            line = lines[i]

            # HTML comments (possibly multi-line) -> drop.
            if line.lstrip().startswith("<!--"):
                flush_para()
                block = line
                while "-->" not in block and i + 1 < n:
                    i += 1
                    block += "\n" + lines[i]
                i += 1
                continue

            # Fenced code block.
            m = re.match(r"^(\s*)(`{3,}|~{3,})(.*)$", line)
            if m:
                flush_para()
                fence = m.group(2)[0]
                lang = m.group(3).strip()
                i += 1
                code_lines: list[str] = []
                while i < n and not re.match(rf"^\s*{re.escape(fence)}{{3,}}\s*$", lines[i]):
                    code_lines.append(lines[i])
                    i += 1
                i += 1  # consume closing fence
                code = html.escape("\n".join(code_lines), quote=False)
                cls = f' class="language-{html.escape(lang, quote=True)}"' if lang else ""
                out.append(f"<pre><code{cls}>{code}</code></pre>")
                continue

            # Heading.
            m = re.match(r"^(#{1,6})\s+(.*)$", line)
            if m:
                flush_para()
                level = len(m.group(1))
                # Strip only an ATX *closing* hash sequence (a run of # preceded by space at
                # end of line), NOT content hashes — so `## C#` / `## F#` keep their `#`.
                text = re.sub(r"\s+#+\s*$", "", m.group(2).strip()).strip()
                # The GitHub slug is computed from the heading's RENDERED (visible) text, so
                # link/emphasis/code markup must be reduced to its visible label first;
                # otherwise `## See [API](api.md)` would slug "see-apiapimd" not "see-api".
                hid = self.new_id(_visible_text(text))
                headings.append((level, text, hid))
                out.append(
                    f'<h{level} id="{hid}" class="md-h md-h{level}">'
                    f'{self.render_inline(text)}'
                    f'<a class="anchor-link" href="#{hid}" aria-label="permalink">#</a>'
                    f"</h{level}>"
                )
                i += 1
                continue

            # Horizontal rule.
            if re.match(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$", line):
                flush_para()
                out.append("<hr>")
                i += 1
                continue

            # Table (header row + a |---|---| separator row).
            if "|" in line and i + 1 < n and re.match(r"^\s*\|?\s*:?-{2,}.*\|", lines[i + 1]):
                flush_para()
                tbl = [line, lines[i + 1]]
                i += 2
                while i < n and "|" in lines[i] and lines[i].strip():
                    tbl.append(lines[i])
                    i += 1
                out.append(self.render_table(tbl))
                continue

            # Blockquote.
            if re.match(r"^\s*>", line):
                flush_para()
                bq: list[str] = []
                while i < n and re.match(r"^\s*>", lines[i]):
                    bq.append(re.sub(r"^\s*>\s?", "", lines[i]))
                    i += 1
                out.append(f"<blockquote>{self.render_inline(chr(10).join(bq))}</blockquote>")
                continue

            # Lists.
            if re.match(r"^\s*([-*+]|\d+\.)\s+", line):
                flush_para()
                end = self._list_end(lines, i)
                out.append(self.render_list(lines, i, end))
                i = end
                continue

            # Standalone image line.
            if re.match(r"^\s*!\[", line):
                flush_para()
                out.append(self.render_inline(line.strip()))
                i += 1
                continue

            # Blank line ends a paragraph.
            if not line.strip():
                flush_para()
                i += 1
                continue

            para.append(line)
            i += 1

        flush_para()
        return RenderResult(html="\n".join(out), headings=headings, assets=dict(self.assets))


def render_spec(spec_path: Path, *, assets_dir: Path | None = None) -> RenderResult:
    """Render a spec markdown file to HTML.

    ``assets_dir`` defaults to ``<spec_dir>/assets`` (the common spec convention); pass it
    explicitly to point figures elsewhere. Figures are referenced as ``/asset/<name>`` and
    the returned ``assets`` map tells the server which files to serve.
    """
    spec_path = Path(spec_path).expanduser().resolve()
    if assets_dir is None:
        assets_dir = spec_path.parent / "assets"
    md = spec_path.read_text(encoding="utf-8", errors="replace")
    return _Renderer(Path(assets_dir)).render(md)
