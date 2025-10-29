#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Render a text post to a social-friendly PNG (1080x1350).
Usage:
  python scripts/render/text_to_social_image.py posts/top10_scorers_anytime.md posts/top10_scorers_anytime.png
Optional env:
  TITLE="Top 10 Scorers — Bet365 Anytime"  BG="#0B0B0B"  FG="#FFFFFF"
"""
import os, sys, textwrap, datetime as dt
from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1350
MARGIN_X, MARGIN_Y = 64, 64
TITLE = os.getenv("TITLE", "")
BG = os.getenv("BG", "#0B0B0B")
FG = os.getenv("FG", "#FFFFFF")
ACCENT = os.getenv("ACCENT", "#A0A0A0")
FONT_REG = os.getenv("FONT_REG", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
FONT_BOLD = os.getenv("FONT_BOLD", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")

def load_font(path, size):
    try:
        return ImageFont.truetype(path, size=size)
    except Exception:
        return ImageFont.load_default()

def strip_md(s: str) -> str:
    # very light markdown cleanup for bold/italics
    for ch in ["**", "__", "*", "_", "`"]:
        s = s.replace(ch, "")
    return s

def wrap_lines(raw: str, width_chars: int) -> list[str]:
    out = []
    for ln in raw.splitlines():
        ln = strip_md(ln).rstrip()
        if not ln:
            out.append("")
            continue
        for w in textwrap.wrap(ln, width_chars, break_long_words=False, replace_whitespace=False):
            out.append(w)
    return out

def main(inp: str, outp: str):
    txt = open(inp, "r", encoding="utf-8").read()
    # choose a comfortable char width (narrow for scan-ability)
    lines = wrap_lines(txt, width_chars=46)

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    title_f = load_font(FONT_BOLD, 54)
    body_f  = load_font(FONT_REG, 42)
    foot_f  = load_font(FONT_REG, 30)

    x, y = MARGIN_X, MARGIN_Y

    # optional title
    if TITLE:
        d.text((x, y), TITLE, font=title_f, fill=FG)
        y += title_f.size + 24

    # body
    for ln in lines:
        if y > H - MARGIN_Y - 120:
            # stop before footer if overflowing
            break
        d.text((x, y), ln, font=body_f, fill=FG)
        y += int(body_f.size * 1.3)

    # footer (date + handle)
    footer = dt.datetime.utcnow().strftime("%d %b %Y UTC")
    handle = os.getenv("FOOTER", footer + "  •  @yourhandle")
    w_foot, h_foot = d.textlength(handle, font=foot_f), foot_f.size
    d.text((W - MARGIN_X - w_foot, H - MARGIN_Y - h_foot), handle, font=foot_f, fill=ACCENT)

    img.save(outp, "PNG")
    print(f"wrote {outp}")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit("usage: text_to_social_image.py <input.txt/md> <output.png>")
    main(sys.argv[1], sys.argv[2])
