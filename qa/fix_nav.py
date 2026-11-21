"""Point QAPlotter's navbar at the CANFAR QA layout (plan §5). Stdlib only.

QAPlotter writes "Webserver Home" and "Project Home" links for the old webserver layout, as bare
folder URLs (the ARC files service answers a folder URL with 400), and prefixes an absolute
flagging-sheet URL with "../" on subpages. For pages under <products_dir>/<EB>/iter_<n>/:
  Webserver Home -> <products_dir>/index.html          renamed "QA home (all tracks)"
  Project Home   -> <products_dir>/<EB>/index.html     renamed "All iterations"
  Flagging Sheet -> the absolute URL without the "../" prefix (or left out when empty)
The weblog is not touched. To retire once QAPlotter takes these links as options (plan §4).

    python fix_nav.py <iteration QA folder>
"""
import re
import sys
from pathlib import Path


def fix(page: Path, root: Path) -> bool:
    depth = len(page.relative_to(root).parts) - 1
    up = "../" * depth
    text = page.read_text(errors="surrogateescape")
    new = re.sub(r'<a href="(?:\.\./)+">Webserver Home', f'<a href="{up}../../index.html">QA home (all tracks)', text)
    new = re.sub(r'<a href="(?:\.\./)+">Project Home', f'<a href="{up}../index.html">All iterations', new)
    new = re.sub(r'<a href="(?:\.\./)*(https?://[^"]*)">Flagging Sheet', r'<a href="\1">Flagging Sheet', new)
    new = re.sub(r'[ \t]*<a href="(?:\.\./)*">Flagging Sheet</a>\n?', "", new)
    if new != text:
        page.write_text(new, errors="surrogateescape")
        return True
    return False


def main(folder):
    root = Path(folder)
    n = sum(fix(p, root) for p in root.rglob("*.html") if "weblog" not in p.relative_to(root).parts)
    print(f"fix_nav: rewrote links in {n} pages under {root}")


if __name__ == "__main__":
    main(sys.argv[1])
