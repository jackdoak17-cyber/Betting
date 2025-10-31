#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Render a text or Markdown-table post to a social PNG (1080x1350).

Usage:
  python scripts/render/text_to_social_image.py <input.md> <output.png> [--mode table|text] [--title "Title"]
Env (optional):
  BG="#0B0B0B"  FG="#FFFFFF"  ACCENT="#A0A0A0"
  FONT_REG="/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
  FONT_BOLD="/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
  FONT_MONO="/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
  FOOTER="your footer text"
"""
import os, sys, re, textwrap, argparse, datetime as dt
from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1350
MARGIN_X, MARGIN_Y = 64, 64

BG     = os.getenv("BG", "#0B0B0B")
FG     = os.getenv("FG", "#FFFFFF")
ACCENT = os.getenv("ACCENT", "#A0A0A0")

FONT_REG  = os.getenv("FONT_REG",  "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
FONT_BOLD = os.getenv("FONT_BOLD", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
FONT_MONO = os.getenv("FONT_MONO", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")

def load_font(path, size):
    try:
        return ImageFont.truetype(path, size=size)
    except Exception:
        return ImageFont.load_default()

def strip_md_inline(s: str) -> str:
    # light cleanup for bold/italics/code
    for ch in ("**","__","*","_","`"):
        s = s.replace(ch, "")
    return s

def read_lines(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().splitlines()

def first_nonempty(lines: list[str]) -> str:
    for ln in lines:
        if ln.strip():
            return ln.strip()
    return ""

# ---------- Markdown table parsing ----------
def parse_md_table(lines: list[str]) -> list[list[str]] | None:
    table_lines = [ln for ln in lines if ln.strip().startswith("|") and ln.strip().endswith("|")]
    if not table_lines:
        return None
    rows = []
    # ignore alignment rule row (---)
    for ln in table_lines:
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if all(set(c) <= set(":- ") for c in cells):  # alignment row like :---:
            continue
        rows.append(cells)
    # sanity: need header + at least one data row
    if len(rows) < 2:
        return None
    # ensure consistent columns
    n = max(len(r) for r in rows)
    rows = [r + [""]*(n-len(r)) for r in rows]
    return rows

def wrap_to_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    words = text.split()
    if not words:
        return [""]
    lines = []
    cur = words[0]
    for w in words[1:]:
        test = cur + " " + w
        if draw.textlength(test, font=font) <= max_w:
            cur = test
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    return lines

def render_table(img: Image.Image, rows: list[list[str]], title: str | None):
    d = ImageDraw.Draw(img)
    # fonts
    title_f  = load_font(FONT_BOLD, 64)
    head_f   = load_font(FONT_BOLD, 42)
    body_f   = load_font(FONT_REG,  40)
    mono_f   = load_font(FONT_MONO, 40)

    x, y = MARGIN_X, MARGIN_Y

    # title
    if title:
        d.text((x, y), title, font=title_f, fill=FG)
        y += title_f.size + 32

    # layout
    avail_w = W - 2*MARGIN_X
    # fixed column width ratios tuned for Rank | Player | Goals | Odds | Opponent
    # (works even if columns differ; we cap to number of cols)
    ratios = [0.10, 0.48, 0.12, 0.14, 0.16]
    cols = len(rows[0])
    ratios = ratios[:cols]
    # normalize ratios to sum=1
    s = sum(ratios)
    ratios = [r/s for r in ratios]
    col_w = [int(avail_w * r) for r in ratios]

    # header background & zebra rows
    zebra = (20, 20, 20)  # #141414-ish
    grid  = (60, 60, 60)

    # compute wrapped cells & row heights
    wrapped: list[list[list[str]]] = []
    heights: list[int] = []

    for r_idx, r in enumerate(rows):
        cells_wrapped = []
        max_lines = 1
        for c_idx, cell in enumerate(r):
            txt = strip_md_inline(cell)
            fnt = body_f if r_idx else head_f
            # Monospace for numeric-ish columns (rank, goals, odds)
            if r_idx and c_idx in (0, 2, 3):
                fnt = mono_f
            lines = wrap_to_width(d, txt, fnt, col_w[c_idx] - 24)
            max_lines = max(max_lines, len(lines))
            cells_wrapped.append(lines)
        wrapped.append(cells_wrapped)
        line_h = int((body_f.size if r_idx else head_f.size) * 1.2)
        heights.append(max(48, line_h * max_lines + 16))  # padding

    # draw rows
    for r_idx, (cells_wrapped, rh) in enumerate(zip(wrapped, heights)):
        # background
        if r_idx and r_idx % 2 == 1:
            d.rectangle([x, y, x + avail_w, y + rh], fill=zebra)
        # grid lines (top)
        d.line([x, y, x + avail_w, y], fill=grid, width=1)

        cx = x
        for c_idx, cell_lines in enumerate(cells_wrapped):
            cw = col_w[c_idx]
            # draw text
            fnt = body_f if r_idx else head_f
            if r_idx and c_idx in (0, 2, 3):
                fnt = mono_f
            ty = y + 8
            for line in cell_lines:
                d.text((cx + 12, ty), line, font=fnt, fill=FG)
                ty += int(fnt.size * 1.2)
            # vertical grid
            d.line([cx, y, cx, y + rh], fill=grid, width=1)
            cx += cw
        # right-most grid
        d.line([x + avail_w, y, x + avail_w, y + rh], fill=grid, width=1)
        y += rh
        if y > H - MARGIN_Y - 120:
            break

    # bottom border
    d.line([x, y, x + avail_w, y], fill=grid, width=1)

def render_text(img: Image.Image, text: str, title: str | None):
    d = ImageDraw.Draw(img)
    title_f = load_font(FONT_BOLD, 64)
    body_f  = load_font(FONT_REG, 44)

    x, y = MARGIN_X, MARGIN_Y
    if title:
        d.text((x, y), title, font=title_f, fill=FG)
        y += title_f.size + 28

    # wrap to a comfy width
    width_chars = 46
    for raw in text.splitlines():
        ln = strip_md_inline(raw.rstrip())
        if not ln:
            y += int(body_f.size * 0.6)
            continue
        for w in textwrap.wrap(ln, width_chars, break_long_words=False, replace_whitespace=False):
            d.text((x, y), w, font=body_f, fill=FG)
            y += int(body_f.size * 1.3)
        if y > H - MARGIN_Y - 120:
            break

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inp")
    ap.add_argument("out")
    ap.add_argument("--mode", choices=["table","text"], default="table")
    ap.add_argument("--title", default=os.getenv("TITLE","").strip())
    args = ap.parse_args()

    raw_lines = read_lines(args.inp)

    # If a title is provided and the first non-empty body line equals it, drop it to avoid duplication
    if args.title:
        first = first_nonempty(raw_lines)
        if first and first.strip() == args.title.strip():
            # remove that first occurrence
            idx = raw_lines.index(first)
            raw_lines.pop(idx)

    img = Image.new("RGB", (W, H), BG)

    if args.mode == "table":
        table = parse_md_table(raw_lines)
        if table:
            render_table(img, table, args.title or None)
        else:
            # fallback to text if no table detected
            render_text(img, "\n".join(raw_lines), args.title or None)
    else:
        render_text(img, "\n".join(raw_lines), args.title or None)

    # footer
    footer = os.getenv("FOOTER", "")
    if footer:
        d = ImageDraw.Draw(img)
        foot_f = load_font(FONT_REG, 30)
        w_foot = d.textlength(footer, font=foot_f)
        d.text((W - MARGIN_X - w_foot, H - MARGIN_Y - foot_f.size),
               footer, font=foot_f, fill=ACCENT)

    img.save(args.out, "PNG")
    print(f"wrote {args.out}")

if __name__ == "__main__":
    main()