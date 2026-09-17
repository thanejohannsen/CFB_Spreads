#!/usr/bin/env python3
"""Render docs/how-it-works.html to docs/reading-a-spread-ladder.pdf.

The page is the master copy; this is how the downloadable PDF is kept in step
with it. Two things make it more than a print-to-file:

  * Fonts are inlined. The page links Google Fonts, which a headless render may
    not reach and a PDF cannot carry by reference, so the woff2 faces are
    fetched and embedded as data: URIs. Without this the document silently
    falls back to Georgia and the careful typography is lost.
  * The sticky navigation bar is dropped by the page's own `@media print`
    rules, which Chromium applies to page.pdf() by default.

Needs Playwright with the preinstalled Chromium; run it from the repo root.

    python tools/build_pdf.py [--out path.pdf]
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "docs", "how-it-works.html")
OUT = os.path.join(ROOT, "docs", "reading-a-spread-ladder.pdf")
CHROME = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
CA_BUNDLE = "/root/.ccr/ca-bundle.crt"

# Only the subsets the document actually uses; pulling every subset triples the
# embedded weight for glyphs that never appear.
SUBSETS = ("latin", "latin-ext")
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")

FOOTER = ('<div style="width:100%;font:8pt \'IBM Plex Mono\',monospace;'
          'color:#5f6d6a;padding:0 14mm;display:flex;'
          'justify-content:space-between;">'
          '<span>Reading a Spread Ladder &middot; CFB Spreads</span>'
          '<span class="pageNumber"></span></div>')


def fetch(url: str) -> bytes:
    """GET through the agent proxy, trusting its CA rather than disabling TLS."""
    cmd = ["curl", "-sS", "--fail", "-A", UA]
    if os.path.exists(CA_BUNDLE):
        cmd += ["--cacert", CA_BUNDLE]
    return subprocess.run(cmd + [url], check=True, capture_output=True).stdout


def inline_fonts(css_url: str) -> str:
    """Turn a Google Fonts stylesheet into @font-face rules with embedded files."""
    css = fetch(css_url).decode("utf-8")
    faces, seen = [], {}
    for _, subset, body in re.findall(
            r"(/\* ([a-z0-9-]+) \*/\s*)?@font-face \{(.*?)\}", css, re.S):
        if subset not in SUBSETS:
            continue
        found = re.search(r"url\((https://[^)]+\.woff2)\)", body)
        if not found:
            continue
        url = found.group(1)
        if url not in seen:
            seen[url] = ("data:font/woff2;base64,"
                         + base64.b64encode(fetch(url)).decode("ascii"))
        faces.append("@font-face {" + body.replace(url, seen[url]) + "}")
    if not faces:
        sys.exit("no font faces resolved; refusing to render with fallbacks")
    return "\n".join(faces)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()

    html = open(PAGE, encoding="utf-8").read()
    link = re.search(r'<link rel="stylesheet" href="(https://fonts\.googleapis\.com[^"]+)">', html)
    if not link:
        sys.exit("no Google Fonts link in the page; update this script")
    html = html.replace(link.group(0), "<style>\n" + inline_fonts(link.group(1)) + "\n</style>")

    # Render from a temp copy beside the page so its relative links still resolve.
    fd, tmp = tempfile.mkstemp(suffix=".html", dir=os.path.dirname(PAGE))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(html)

        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=CHROME)
            page = browser.new_page(viewport={"width": 1100, "height": 1400})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto("file://" + tmp, wait_until="load")
            page.wait_for_timeout(1500)
            # document.fonts.check() is not a guard on its own: it answers true
            # for a family the page never defined, because the fallback can
            # render it. Count the faces that actually loaded instead.
            loaded = page.evaluate(
                "[...document.fonts].filter(f => f.status === 'loaded')"
                ".map(f => f.family)")
            for family in ("Spectral", "IBM Plex Mono"):
                if family not in loaded:
                    sys.exit(f"{family} did not load ({len(loaded)} faces ready); "
                             "the PDF would silently use a fallback")
            page.pdf(path=args.out, format="Letter", print_background=True,
                     display_header_footer=True, header_template="<span></span>",
                     footer_template=FOOTER,
                     margin={"top": "14mm", "bottom": "16mm",
                             "left": "13mm", "right": "13mm"})
            browser.close()
        if errors:
            sys.exit(f"page errors during render: {errors[:3]}")
    finally:
        os.unlink(tmp)

    data = open(args.out, "rb").read()
    pages = data.count(b"/Type /Page") - data.count(b"/Type /Pages")
    print(f"{os.path.relpath(args.out, ROOT)}: {pages} pages, {len(data) // 1024} KB")


if __name__ == "__main__":
    main()
