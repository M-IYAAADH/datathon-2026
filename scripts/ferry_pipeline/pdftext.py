"""Character-level PDF text extraction with coordinates.

Plain text extraction loses the column relationships of these timetables and
re-orders Dhivehi glyphs, so every parser works from individual characters
(PyMuPDF ``rawdict``) grouped into visual lines and gap-separated tokens.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import fitz  # PyMuPDF


_DV_PUNCT = {"،", "؛", "؟", "ﷲ"}  # Arabic comma etc., used in Dhivehi text


def is_thaana(ch: str) -> bool:
    return "ހ" <= ch <= "޿" or ch in _DV_PUNCT


@dataclass(frozen=True)
class Char:
    x0: float
    y0: float
    x1: float
    y1: float
    c: str
    size: float
    color: int

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass
class Token:
    """A run of same-script characters with small gaps between them."""
    chars: list[Char]
    thaana: bool

    @property
    def x0(self) -> float:
        return min(c.x0 for c in self.chars)

    @property
    def x1(self) -> float:
        return max(c.x1 for c in self.chars)

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def text(self) -> str:
        return chars_to_text(self.chars)

    @property
    def compact(self) -> str:
        return "".join(c.c for c in self.chars)


@dataclass
class Line:
    y: float
    chars: list[Char]
    dv_spans: list[tuple[float, str]] = field(default_factory=list)  # (x0, raw span text)

    @property
    def latin_chars(self) -> list[Char]:
        return [c for c in self.chars if not is_thaana(c.c)]

    @property
    def text(self) -> str:
        """Latin/digit text in left-to-right order (Thaana excluded)."""
        return chars_to_text(sorted(self.latin_chars, key=lambda c: c.x0))

    @property
    def has_thaana(self) -> bool:
        return any(is_thaana(c.c) for c in self.chars)

    @property
    def raw_dv(self) -> str:
        """Raw Dhivehi span text as stored in the PDF, right-to-left by position.

        This is NOT reconstructed: the PDFs store many Thaana words with the
        final vowel sign moved to the front and some glyphs dropped.
        """
        return " | ".join(t.strip() for _, t in sorted(self.dv_spans, key=lambda s: -s[0]) if t.strip())

    def tokens(self, join_gap: float | None = None, x_min: float = -1e9, x_max: float = 1e9) -> list[Token]:
        cs = sorted((c for c in self.chars if x_min <= c.xc < x_max), key=lambda c: c.x0)
        out: list[Token] = []
        for c in cs:
            th = is_thaana(c.c)
            gap_limit = join_gap if join_gap is not None else 0.6 * c.size
            if out and out[-1].thaana == th and c.x0 - out[-1].x1 <= gap_limit:
                out[-1].chars.append(c)
            else:
                out.append(Token([c], th))
        return out


def chars_to_text(cs: list[Char]) -> str:
    """Join characters, inserting a space where the visual gap looks like one."""
    out = []
    prev = None
    for c in cs:
        if prev is not None and c.x0 - prev.x1 > 0.18 * c.size:
            out.append(" ")
        out.append(c.c)
        prev = c
    return "".join(out).strip()


def _background_rects(page: fitz.Page) -> list[fitz.Rect]:
    rects = []
    for d in page.get_drawings():
        fill = d.get("fill")
        if fill is None or min(fill) >= 0.94:
            continue
        rects.append(fitz.Rect(d["rect"]))
    for img in page.get_image_info():
        rects.append(fitz.Rect(img["bbox"]))
    return rects


def page_chars(page: fitz.Page) -> tuple[list[Char], list[Char], list[tuple[fitz.Rect, str]]]:
    """Return (visible chars, hidden chars, raw spans).

    Whitespace is dropped (gaps are used instead). Characters outside the page
    and white characters not lying on a non-white fill are treated as hidden:
    the files contain stale white-on-white text that is not visible on the page.
    """
    bgs = _background_rects(page)
    W, H = page.rect.width, page.rect.height
    visible: list[Char] = []
    hidden: list[Char] = []
    spans: list[tuple[fitz.Rect, str]] = []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                col = span["color"]
                whiteish = min((col >> 16) & 255, (col >> 8) & 255, col & 255) >= 240
                span_vis = []
                for ch in span["chars"]:
                    if not ch["c"].strip():
                        continue
                    x0, y0, x1, y1 = ch["bbox"]
                    c = Char(x0, y0, x1, y1, ch["c"], span["size"], col)
                    off_page = x1 < 0 or x0 > W or y1 < 0 or y0 > H
                    on_bg = any(r.contains(fitz.Point(c.xc, c.yc)) for r in bgs)
                    if off_page or (whiteish and not on_bg):
                        hidden.append(c)
                    else:
                        visible.append(c)
                        span_vis.append(c)
                if span_vis:
                    text = "".join(ch["c"] for ch in span["chars"])
                    spans.append((fitz.Rect(span["bbox"]), text))
    return visible, hidden, spans


def group_lines(chars: list[Char], spans: list[tuple[fitz.Rect, str]], tol: float = 4.0) -> list[Line]:
    """Cluster characters into visual lines by vertical centre."""
    lines: list[Line] = []
    for c in sorted(chars, key=lambda c: (c.yc, c.x0)):
        if lines and abs(c.yc - lines[-1].y) <= tol:
            ln = lines[-1]
            ln.chars.append(c)
            ln.y = sum(x.yc for x in ln.chars) / len(ln.chars)
        else:
            lines.append(Line(c.yc, [c]))
    for rect, text in spans:
        if not any(is_thaana(ch) for ch in text):
            continue
        yc = (rect.y0 + rect.y1) / 2
        best = min(lines, key=lambda ln: abs(ln.y - yc), default=None)
        if best is not None and abs(best.y - yc) <= tol + 2:
            best.dv_spans.append((rect.x0, text))
    return lines


def page_lines(page: fitz.Page) -> tuple[list[Line], list[Char]]:
    vis, hid, spans = page_chars(page)
    return group_lines(vis, spans), hid
