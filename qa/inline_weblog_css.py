"""Inline the CASA weblog's stylesheet so it renders when served from CANFAR. Stdlib only.

The ARC files service (cavern) serves .css files as text/plain, and browsers refuse to apply a
<link rel="stylesheet"> with that type in standards mode, so the weblog shows unstyled. This
replaces each <link rel="stylesheet" href="...css"> in the extracted weblog with a <style> block
holding that file's text. Relative url(...) targets in the CSS (fonts) are rewritten to resolve from
the page's own directory instead of the CSS file's.

Only the extracted copy products/weblog/ is changed; weblog.tgz (the CASA output) is untouched, and
so is the pipeline. Pages without a matching <link> are skipped, so reruns are safe.

    python inline_weblog_css.py <products_dir>
"""
import posixpath
import re
import sys
from pathlib import Path

LINK_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
REL_RE = re.compile(r"""\brel\s*=\s*["']?stylesheet\b""", re.IGNORECASE)
HREF_RE = re.compile(r"""\bhref\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)
URL_RE = re.compile(r"""url\(\s*(['"]?)([^'")]*)\1\s*\)""", re.IGNORECASE)


def _is_local_relative(ref: str) -> bool:
    return bool(ref) and not re.match(r"^(?:[a-z][a-z0-9+.-]*:|//|/|#)", ref, re.IGNORECASE)


def rebase_urls(css: str, css_dir: str) -> str:
    """Rewrite relative url() refs from being relative to css_dir to being relative to the page.

    css_dir is the CSS file's directory relative to the page (posix, '' if the same directory).
    """
    def repl(m):
        quote, ref = m.group(1), m.group(2)
        if not _is_local_relative(ref):
            return m.group(0)
        new = posixpath.normpath(posixpath.join(css_dir, ref)) if css_dir else ref
        return f"url({quote}{new}{quote})"

    return URL_RE.sub(repl, css)


def fix(page: Path) -> int:
    """Inline stylesheets in one page. Returns the number of <link> tags replaced."""
    text = page.read_text(errors="surrogateescape")
    count = 0

    def repl(m):
        nonlocal count
        tag = m.group(0)
        if not REL_RE.search(tag):
            return tag
        h = HREF_RE.search(tag)
        href = (h.group(1) or h.group(2)) if h else ""
        if not href.split("?")[0].lower().endswith(".css") or not _is_local_relative(href):
            return tag
        css_path = (page.parent / href.split("?")[0]).resolve()
        if not css_path.is_file():
            print(f"inline_weblog_css: {page}: missing {href}, left as is", file=sys.stderr)
            return tag
        css = css_path.read_text(errors="surrogateescape")
        css = rebase_urls(css, posixpath.dirname(href.split("?")[0]))
        css = css.replace("</style", "<\\/style")
        count += 1
        return f"<style>\n{css}\n</style>"

    new = LINK_RE.sub(repl, text)
    if count:
        page.write_text(new, errors="surrogateescape")
    return count


def main(products_dir):
    html_dir = Path(products_dir) / "weblog" / "html"
    if not html_dir.is_dir():
        sys.exit(f"inline_weblog_css: no weblog/html under {products_dir}")
    pages = changed = 0
    for page in sorted(html_dir.rglob("*.html")):
        pages += 1
        if fix(page):
            changed += 1
    print(f"inline_weblog_css: inlined the stylesheet in {changed} of {pages} weblog pages under {html_dir}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
