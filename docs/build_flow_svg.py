#!/usr/bin/env python3
"""Build assets/review-flow.svg: the landing page's animated pipeline
diagram, as a standalone image the README can embed.

Run from docs/:  python3 build_flow_svg.py

Why this is generated rather than copied once. GitHub strips inline <svg>
from a README, so the only way to show the animation there is an <img> of
a separate .svg file. That file has to be self-contained: no page CSS, no
CSS variables from :root, no web fonts (an image cannot fetch any of them).
The source of truth stays the diagram in index.html and the marks in
assets/site.css; this script lifts both, resolves what the page would have
resolved, and writes the result -- so editing the diagram once and
re-running this keeps the README from quietly showing an old one.

It draws on a fixed dark panel rather than following light/dark: whether a
browser applies prefers-color-scheme inside an <img> varies, and a diagram
that is only legible on one theme is worse than one that is a card on both.
"""

from __future__ import annotations

import re
from pathlib import Path

HERE = Path(__file__).parent

# The dark theme's values from assets/site.css, resolved. The page uses
# var(--x); a standalone image has no :root to resolve them against.
COLORS = {
    "--surface": "#121a1b",
    "--tint": "#16292a",
    "--text": "#e8efed",
    "--muted": "#91a3a4",
    "--line": "#2b3a3b",
    "--accent": "#5bd3c6",
    "--flag": "#e39a63",
}
PANEL_FILL = "#0e1516"
PANEL_STROKE = "#223031"

# An image cannot load Archivo or IBM Plex, so fall back to what is on the
# viewer's machine. Metrics differ slightly; the sizes below leave room.
MONO = 'ui-monospace, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace'
DISPLAY = '-apple-system, "Segoe UI", Helvetica, Arial, sans-serif'

# Labels at the page's sizes render near 5px once this is scaled to a README
# column, and a README image cannot scroll sideways the way the page's
# diagram does. Sizes are raised only where the text sits in open space;
# the text inside the stage boxes is bounded by the box and stays close to
# the original.
FONT_SIZES = {
    "s-num": 12,
    "s-name": 17,
    "s-tech": 11.5,
    "hand": 12.5,
    # smaller than its neighbours on purpose: "← is it real?" and "verified"
    # share a row with a 164-unit gap, and at 12.5 they read as one phrase
    "call-label": 11.5,
    "tag-text": 12,
    "drop-text": 12.5,
    "pill-text": 16,
    "aside-label": 12.5,
}

# Bigger text needs more line spacing: (class, old y) -> new y.
LINE_FIXES = {("hand", 246): 251, ("call-label", 112): 115}

ENTITIES = {"&larr;": "&#8592;", "&rarr;": "&#8594;", "&mdash;": "&#8212;", "&middot;": "&#183;"}


def extract_svg(index_html: str) -> str:
    match = re.search(r'<svg viewBox="0 0 1468 348".*?</svg>', index_html, re.DOTALL)
    if not match:
        raise SystemExit("could not find the pipeline <svg> in index.html")
    return match.group(0)


def extract_marks(site_css: str) -> str:
    start = site_css.index("/* ---- diagram marks ---- */")
    end = site_css.index("/* ---- worked example ---- */")
    return site_css[start:end]


def resolve_vars(text: str) -> str:
    for name, value in COLORS.items():
        text = text.replace(f"var({name})", value)
    return text


def style_block(marks: str) -> str:
    css = resolve_vars(marks)
    css = css.replace("var(--mono)", MONO).replace("var(--display)", DISPLAY)
    for cls, size in FONT_SIZES.items():
        css = re.sub(
            rf"(\.{re.escape(cls)} \{{[^}}]*?font-size: )[\d.]+px", rf"\g<1>{size}px", css
        )
    # the page's reduced-motion block also sets scroll-behavior on *, which
    # means nothing inside an image
    css = css.replace("    * { scroll-behavior: auto; }\n", "")
    return css


def build() -> str:
    svg = extract_svg((HERE / "index.html").read_text())
    marks = extract_marks((HERE / "assets" / "site.css").read_text())

    for entity, numeric in ENTITIES.items():
        svg = svg.replace(entity, numeric)
    leftover = re.findall(r"&[a-zA-Z]+;", svg)
    if leftover:
        raise SystemExit(f"entities this script does not convert: {sorted(set(leftover))}")

    svg = resolve_vars(svg)
    if "var(--" in svg.replace("var(--i)", ""):
        raise SystemExit("an unresolved CSS variable is left in the SVG")

    for (cls, old), new in LINE_FIXES.items():
        svg = re.sub(rf'(class="{cls}"[^>]*? y=")({old})(")', rf"\g<1>{new}\g<3>", svg)

    # a panel behind the diagram, with margin, so it reads as a card
    pad_x, pad_y = 24, 18
    width, height = 1468 + 2 * pad_x, 348 + 2 * pad_y
    svg = svg.replace(
        '<svg viewBox="0 0 1468 348"',
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{-pad_x} {-pad_y} {width} {height}"',
        1,
    )
    panel = (
        f'<rect x="{-pad_x}" y="{-pad_y}" width="{width}" height="{height}" rx="14" '
        f'fill="{PANEL_FILL}" stroke="{PANEL_STROKE}" stroke-width="1.5"/>'
    )
    style = f"<style>\n{style_block(marks)}\n</style>"
    svg = re.sub(r"(<svg [^>]*>)", rf"\1\n{style}\n{panel}", svg, count=1)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + svg + "\n"


def main() -> None:
    out = HERE / "assets" / "review-flow.svg"
    out.write_text(build(), encoding="utf-8")
    print(f"wrote {out.relative_to(HERE)} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
