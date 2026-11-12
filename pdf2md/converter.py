#!/usr/bin/env python3
"""
pdf2md.py - exam result PDFs -> Practice Hub .md   (single file or whole folders, auto-detects the type)

Supported PDF types (auto-detected):
  map     MAP daily / monthly analysis reports ("Question N", green tick, Bijoy/SutonnyMJ or Unicode Bangla)
  retina  Retina LMS offline-exam results ("Q1.", "Your answer: ... Correct answer: D")
  retina2 Retina LMS "Exam Result" (mobile web: "Question 1", "Marks: 0", letter on its own line)

Usage:
    python pdf2md.py exam.pdf                         # -> exam.md next to the PDF
    python pdf2md.py exam.pdf -o questions/GK4.md
    python pdf2md.py pdfs/ -o questions/              # batch: every PDF in the folder -> questions/<name>.md
    python pdf2md.py a.pdf b.pdf c.pdf -o questions/
    options: --status (keep correct/incorrect/skipped)  --images img (crop vector formulas)  --type map|retina|retina2

Install:
    PC     : pip install pymupdf bijoy2unicode flask
    Termux : pkg install python poppler ; pip install bijoy2unicode pdfminer.six flask
             (pymupdf is optional; without it MAP PDFs use pdfminer and Retina PDFs use poppler)
"""
import argparse
import re
import sys
from pathlib import Path

try:
    import pymupdf
except ImportError:  # e.g. Termux: use pure-python pdfminer.six instead
    pymupdf = None
from bijoy2unicode import converter

from pdf2md.mathglyph import repair as repair_text, repair_markdown

# ---------------------------------------------------------------- Bijoy -> Unicode
converter.conversionMap["ÿ"] = "ক্ষ"  # missing in the library
converter.conversionMap["ø"] = "্ল"    # library maps to স্ন (wrong for SutonnyMJ)
converter.conversionMap["æ"] = "ু"     # library maps to ম্ন (wrong for SutonnyMJ)
_U = converter.Unicode()

_CONS = "ক-হড়ঢ়য়ৎ"
_CLUSTER = rf"[{_CONS}](?:্[{_CONS}])*"
_SIGNS = "ািীুূৃেৈোৌ"
_RE = "\u24c7"  # placeholder for reph (©), fixed after conversion
_DASH = "\u24b9"  # placeholder for a literal " – "
_REPH_RE = re.compile(rf"({_CLUSTER})([{_SIGNS}]*){_RE}([{_SIGNS}]*)")


# PDF-specific typos / odd glyph orders that can't be fixed by rules. Add your own here.
WORD_FIXES = {"জামার্নি": "জার্মানি"}


def bijoy_to_unicode(text: str) -> str:
    core = text.strip()
    if not core:
        return text
    core = core.replace(" – ", f" {_DASH} ").replace("©", _RE)
    try:
        out = _U.convertBijoyToUnicode(core + "  ")  # padding avoids a library IndexError
    except IndexError:
        out = _U.convertBijoyToUnicode(core + "    ")
    out = out.replace("ন্ত্ম", "ন্ত").replace("স্ত্ম", "স্ত")   # šÍ / ¯Í
    out = out.replace("স্্ল", "স্ল")
    out = _REPH_RE.sub(lambda m: "র্" + m.group(1) + m.group(2) + m.group(3), out)  # reph before cluster
    out = out.replace(_RE, "র্").replace(_DASH, "–")
    for bad, good in WORD_FIXES.items():
        out = out.replace(bad, good)
    out = out.replace("ে" + "া", "ো").replace("ে" + "ৗ", "ৌ")
    out = out.replace("\u200c", "").replace("\u00ad", "")
    out = re.sub(r"[ \t]+", " ", out).strip()
    out = repair_text(out)          # math glyphs decoded as MacRoman look-alikes
    return (" " if text[:1].isspace() else "") + out + (" " if text[-1:].isspace() else "")


# ---------------------------------------------------------------- PDF reading
# Both readers return (items, ticks, vecs):
#   items : [(y, x, text)]   y is global (page * 10000 + y on page), Bijoy already converted
#   ticks : [(y, x)]         position of every "correct answer" tick (green drawing or tick glyph)
#   vecs  : [(y, x)]         dark vector shapes = formulas the PDF drew as paths (no text to extract)
# Font families that carry Bijoy-encoded Bangla.  The name test alone is not
# enough: PDFs also ship them as "MJ-Normal", "Sutonny MJ", "Jomuna", plain
# "MJ", or with a subset prefix "ABCDF+SutonnyMJ".
_BIJOY_FONTS = ("sutonny", "jomuna", "akaash", "boishakhi", "prothoma", "shonar",
                "shonarbangla", "adv", "mj", "bijoy")


def _is_bijoy(font: str) -> bool:
    name = (font or "").lower().replace(" ", "").replace("-", "").replace(",", "")
    if any(token in name for token in _BIJOY_FONTS):
        return True
    return name.endswith("mj") or "mj" == name


# A Bijoy span is ASCII that uses these diacritic-ish glyphs heavily and has no
# Unicode Bangla at all.  Used as a safety net when the font name is unhelpful
# (missing, subset-named, or a plain "F1").
# Only glyphs that are Bijoy-only: daggers, "not", ্ল, ক্ষ, ৃ, ৌ and the
# currency slot.  Bullets (•), guillemets («), pilcrows (¶) and § are
# deliberately excluded - they are legitimate in Unicode text.
_BIJOY_MARKS = "\u2021\u2020\u00ac\u00f8\u00ff\u201e\u00f4\u00a4"


def _looks_bijoy(text: str) -> bool:
    """True when a span is plainly Bijoy text, whatever its font is called."""
    if not text:
        return False
    if any("\u0980" <= ch <= "\u09ff" for ch in text):
        return False                      # already Unicode Bangla
    marks = sum(text.count(ch) for ch in _BIJOY_MARKS)
    return marks >= 2 and marks / max(1, len(text)) >= 0.03


def _maybe_bijoy(text: str, font: str) -> str:
    """Convert a span to Unicode when it is Bijoy, however that was detected."""
    return bijoy_to_unicode(text) if (_is_bijoy(font) or _looks_bijoy(text)) else text


def _dark(c) -> bool:
    return bool(c) and isinstance(c, (tuple, list)) and len(c) == 3 and max(c) < 0.4 and not (c[1] > c[0] + .3)


def _fix_text(t: str) -> str:
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r" +([।,;:?!)])", r"\1", t)  # tidy gaps at font boundaries
    return re.sub(r"([(]) +", r"\1", t)


def read_pymupdf(path):
    doc = pymupdf.open(path)
    items, ticks, vecs = [], [], []
    for pi, page in enumerate(doc):
        base = pi * 10000
        for b in page.get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                parts = []
                for sp in ln["spans"]:
                    t, f = sp["text"], sp["font"]
                    if "awesome" in f.lower():                      # icon font: tick glyph
                        if "\uf00c" in t:
                            r = sp["bbox"]
                            ticks.append((base + (r[1] + r[3]) / 2, r[0]))
                        continue
                    if "cmsy" in f.lower():                         # TeX symbol font: "!" is an arrow
                        t = t.replace("!", "→")
                    parts.append(_maybe_bijoy(t, f))
                text = _fix_text("".join(parts))
                if text:
                    r = ln["bbox"]
                    items.append((base + (r[1] + r[3]) / 2, r[0], text))
        for dr in page.get_drawings():                              # green vector tick
            c, r = dr.get("color"), dr["rect"]
            if c and abs(c[0]) < .05 and abs(c[1] - .5) < .05 and abs(c[2]) < .05 and r.width < 20 and r.height < 20:
                ticks.append((base + (r.y0 + r.y1) / 2, r.x0))
            elif _dark(dr.get("fill")) and r.width < 80 and r.height < 60:   # math glyph drawn as vector
                vecs.append((base + (r.y0 + r.y1) / 2, (r.x0 + r.x1) / 2))
    return items, ticks, vecs


def _pdfminer_shim():
    """Termux: pip's `cryptography` wheel often fails to load (dlopen ... PyLong_Type) and pdfminer.six
    imports it only for encrypted PDFs. If it is broken, install tiny stand-ins so pdfminer still works."""
    try:
        import cryptography.hazmat.primitives.ciphers  # noqa: F401
        import cryptography.hazmat.backends  # noqa: F401
        return False
    except Exception:
        import types
        for name in [m for m in list(sys.modules) if m == "cryptography" or m.startswith("cryptography.")]:
            del sys.modules[name]
        names = ["cryptography", "cryptography.hazmat", "cryptography.hazmat.backends",
                 "cryptography.hazmat.primitives", "cryptography.hazmat.primitives.ciphers"]
        mods = {n: types.ModuleType(n) for n in names}

        def _unsupported(*a, **k):
            raise RuntimeError("encrypted PDFs are not supported without a working 'cryptography' package")

        mods["cryptography.hazmat.backends"].default_backend = _unsupported
        c = mods["cryptography.hazmat.primitives.ciphers"]
        c.Cipher = _unsupported
        c.algorithms = types.SimpleNamespace(AES=_unsupported, ARC4=_unsupported)
        c.modes = types.SimpleNamespace(CBC=_unsupported, ECB=_unsupported)
        sys.modules.update(mods)
        return True



def read_miner(path):
    """Pure-python backend (pdfminer.six) - works in Termux where pymupdf won't install."""
    _pdfminer_shim()
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTChar, LTCurve

    def walk(o):
        if isinstance(o, (LTChar, LTCurve)):
            yield o
        elif hasattr(o, "__iter__"):
            for x in o:
                yield from walk(x)

    items, ticks, vecs = [], [], []
    for pi, page in enumerate(extract_pages(path)):
        base, H = pi * 10000, page.height
        chars = [o for o in walk(page) if isinstance(o, LTChar)]
        for c in [c for c in chars if "awesome" in c.fontname.lower()]:
            if "\uf00c" in c.get_text() or "cid" in c.get_text():
                ticks.append((base + H - (c.y0 + c.y1) / 2, c.x0))
        chars = [c for c in chars if "awesome" not in c.fontname.lower()]
        chars.sort(key=lambda c: (-(c.y0 + c.y1) / 2, c.x0))
        rows, top = [], None
        for c in chars:                                   # group by vertical centre
            yc = (c.y0 + c.y1) / 2
            if top is None or abs(yc - top) > 5:
                rows.append([])
                top = yc
            rows[-1].append(c)
        for row in rows:
            row.sort(key=lambda c: c.x0)
            groups, cur, prev = [], [], None
            for c in row:                                 # split: big gap / font-size change
                if prev is not None and (c.x0 - prev.x1 > 1.5 * prev.size or abs(c.size - prev.size) > 0.5):
                    groups.append(cur)
                    cur = []
                cur.append(c)
                prev = c
            groups.append(cur)
            for g in groups:
                parts, span, font, pc = [], "", None, None

                def flush():
                    if span:
                        t = span
                        if "cmsy" in font.lower():
                            t = t.replace("!", "→")
                        parts.append(_maybe_bijoy(t, font))

                for c in g:
                    if pc is not None and c.x0 - pc.x1 > 0.2 * pc.size and not span.endswith(" ") and c.get_text() != " ":
                        span += " "
                    if font is not None and c.fontname != font:
                        flush()
                        span = ""
                    font = c.fontname
                    span += c.get_text()
                    pc = c
                flush()
                text = _fix_text("".join(parts))
                if text:
                    yc = sum((c.y0 + c.y1) / 2 for c in g) / len(g)
                    items.append((base + H - yc, g[0].x0, text))
        for o in walk(page):
            if isinstance(o, LTCurve) and o.stroking_color and o.width < 20 and o.height < 20:
                c = o.stroking_color
                if len(c) == 3 and abs(c[0]) < .05 and abs(c[1] - .5) < .05 and abs(c[2]) < .05:
                    ticks.append((base + H - (o.y0 + o.y1) / 2, o.x0))
                elif _dark(o.non_stroking_color) and o.width < 80 and o.height < 60:
                    vecs.append((base + H - (o.y0 + o.y1) / 2, (o.x0 + o.x1) / 2))
    return items, ticks, vecs


def read_pdf(path, backend="auto"):
    if backend == "miner" or (backend == "auto" and pymupdf is None):
        return read_miner(path)
    return read_pymupdf(path)


# ---------------------------------------------------------------- parsing (position based)
# Works for both layouts: options in 2 columns (MAP daily) or stacked, stats row / solution box
# anywhere in the stream. Everything is assigned by coordinates, not by text order.
STATUSES = ("Correct", "Incorrect", "Skipped")
JUNK = {"Past Exam", "Correct Incorrect Skipped"}


def _rows_join(lines):
    """join lines into text; lines on the same visual row are joined left->right"""
    rows = []
    for l in sorted(lines, key=lambda l: l[0]):
        if rows and l[0] - rows[-1][0] < 6:
            rows[-1][1].append(l)
        else:
            rows.append([l[0], [l]])
    out = ""
    for _, row in rows:
        for l in sorted(row, key=lambda l: l[1]):
            t = l[2]
            if out and re.match(r"^(\(?[ivx]+[\).]|[১-৯][\).])\s", t):
                out += "  \n" + t
            else:
                out += (" " if out else "") + t
    return out.strip()


def parse(items, ticks, vecs=()):
    items = sorted(items, key=lambda l: (l[0], l[1]))
    heads = []
    for it in items:
        m = re.fullmatch(r"Question\s*(\d+)\s*(Correct|Incorrect|Skipped)?", it[2])
        if m:
            heads.append((it, int(m.group(1)), m.group(2) or ""))
    qs = []
    for k, (h, num, status) in enumerate(heads):
        y0 = h[0]
        y1 = heads[k + 1][0][0] if k + 1 < len(heads) else float("inf")
        blk = [l for l in items if y0 - 0.1 <= l[0] < y1 - 0.1 and l is not h]
        for l in blk[:]:                                  # status label on the header row
            if l[2] in STATUSES and abs(l[0] - y0) < 5:
                status = status or l[2]
                blk.remove(l)
        blk = [l for l in blk if l[2] not in JUNK]
        # stats row (A 87% B 1% ... Skipped 8%)
        stat_ys = [l[0] for l in blk if re.fullmatch(r"\d+%", l[2])]
        is_stat = lambda l: any(abs(l[0] - sy) < 5 for sy in stat_ys) and (
            re.fullmatch(r"\d+%|[ABCD]|Skipped", l[2]) or re.fullmatch(r"[ABCD] \d+%", l[2]))
        stats = [l for l in blk if is_stat(l)]
        blk = [l for l in blk if l not in stats]
        # solution
        sol_l = [l for l in blk if l[2].startswith("Solution")]
        sol, body = [], blk
        if sol_l:
            sy = min(l[0] for l in sol_l) - 6
            sol = [l for l in blk if l[0] >= sy]
            body = [l for l in blk if l[0] < sy]
            sol = [(l[0], l[1], re.sub(r"^Solution\s*:\s*", "", l[2])) for l in sol]
            sol = [l for l in sol if l[2]]
        letters = sorted([l for l in body if re.fullmatch(r"[ABCD]", l[2])], key=lambda l: (l[0], l[1]))
        letters = letters[:4] if len(letters) >= 4 else letters
        q = {"n": num, "status": status.lower(), "ans": "", "q": "", "opts": {}, "sol": "",
             "regions": {}}
        bottom = min([l[0] for l in sol_l + stats] + [y1]) - 4 if (sol_l or stats) else y1

        def owner(y, x):
            cand = [c for c in letters if c[0] <= y + 8]
            if not cand:
                return None
            top = max(c[0] for c in cand)
            row = [c for c in cand if top - c[0] < 8]
            left = [c for c in row if c[1] <= x + 3]
            return (max(left, key=lambda c: c[1]) if left else min(row, key=lambda c: c[1]))[2]

        qlines, opts = [], {L: [] for L in "ABCD"}
        for l in body:
            if l in letters:
                continue
            o = owner(l[0], l[1])
            (opts[o] if o else qlines).append(l)
        q["q"] = _rows_join(qlines)
        q["opts"] = {L: _rows_join(v) for L, v in opts.items()}
        # regions that may hold vector formulas: question text and every option
        if letters:
            q["regions"]["Q"] = (y0 + 8, min(c[0] for c in letters) - 10, 20, 595)
            for c in letters:
                nxt_row = [d[0] for d in letters if d[0] > c[0] + 8]
                nxt_col = [d[1] for d in letters if abs(d[0] - c[0]) < 8 and d[1] > c[1]]
                q["regions"][c[2]] = (c[0] - 14, (min(nxt_row) - 14) if nxt_row else bottom,
                                      c[1] + 14, (min(nxt_col) - 4) if nxt_col else 595)
        q["regions"]["ALL"] = (y0 + 8, bottom, 20, 595)
        q["vec"] = {k: any(r[0] <= vy <= r[1] and r[2] <= vx <= r[3] for vy, vx in vecs)
                    for k, r in q["regions"].items()}
        q["sol"] = re.sub(r"^জেনে রাখো\s*[:ঃ]\s*", "", _rows_join(sol)).strip()
        # ticks inside this block -> answer
        for ty, tx in ticks:
            if y0 <= ty < y1:
                o = owner(ty, tx)
                if o:
                    q["ans"] = o
                    r = q["regions"].get(o)
                    if r:                                   # keep the tick out of cropped option images
                        q["regions"][o] = (r[0], r[1], r[2], min(r[3], tx - 2))
        qs.append(q)
    return qs



# ---------------------------------------------------------------- type 3: Retina LMS result PDFs
# "Q1. question / A. .. D. / Your answer: — Correct answer: D / Explanation ...", Unicode Bangla.
# The Chrome print layer has glitches (mid-word line breaks, doubled vowel signs), so text is rebuilt
# from rows (pymupdf) or from poppler's pdftotext (Termux: pkg install poppler).
_STATUS = r"(?:Skipped|Incorrect|Correct)"


def _retina_rows_pymupdf(path):
    out = []
    for page in pymupdf.open(path):
        segs = []
        for b in page.get_text("dict")["blocks"]:
            for l in b.get("lines", []):
                t = "".join(sp["text"] for sp in l["spans"])
                if t:
                    segs.append(((l["bbox"][1] + l["bbox"][3]) / 2, l["bbox"][0], t))
        segs.sort()
        rows = []
        for y, x, t in segs:                      # same visual row -> glue left to right (spaces are explicit)
            if rows and abs(y - rows[-1][0]) < 4:
                rows[-1][1].append((x, t))
            else:
                rows.append([y, [(x, t)]])
        out += ["".join(t for _, t in sorted(r)) for _, r in rows]
    return out


def _retina_rows_poppler(path):
    import subprocess
    txt = subprocess.run(["pdftotext", path, "-"], capture_output=True, text=True, check=True).stdout
    return txt.replace("\f", "\n").splitlines()


def _clean_bn(t: str) -> str:
    t = t.replace("\u200b", "").replace("\u200c", "").replace("\u00a0", " ")
    t = t.replace("ৌৗ", "ৌ")
    t = re.sub(r"([\u0981-\u0983\u09BC\u09BE-\u09CC\u09D7])\1+", r"\1", t)   # doubled signs: াা ঁঁ ঃঃ
    return re.sub(r"\s+", " ", t).strip()


def parse_retina(rows):
    rows = [_clean_bn(r) for r in rows]
    rows = [r for r in rows if r]
    qs, cur, stage, last_opt = [], None, None, None
    for r in rows:
        m = re.match(r"Q(\d+)\.\s*(.*)", r)
        if m and (cur is None or stage in ("exp", "ans")):      # new question only after the previous one ended
            cur = {"n": int(m.group(1)), "status": "", "ans": "", "q": [], "opts": {L: [] for L in "ABCD"},
                   "sol": [], "regions": {}, "vec": {}}
            qs.append(cur)
            stage, last_opt = "q", None
            r = m.group(2)
            if not r:
                continue
        if cur is None:
            continue
        if stage in ("q", "opt"):
            r = re.sub(rf"^{_STATUS}(?=\S)", "", r)             # status badge glued to the row start/end
            r = re.sub(rf"\s*{_STATUS}$", "", r).strip() if not re.match(r"^[ABCD]\. ", r) or True else r
            r = re.sub(rf"(?<=\S){_STATUS}(?=[ABCD]\.)", "", r)
            if re.fullmatch(_STATUS, r) or not r:
                continue
            ym = re.match(r"Your answer:\s*(.*?)\s*Correct answer:\s*([ABCD])", r)
            if ym:
                yours = ym.group(1).strip("— -").strip()
                cur["ans"] = ym.group(2)
                cur["status"] = "skipped" if not yours else ("correct" if yours[:1] == cur["ans"] else "incorrect")
                stage = "ans"
                continue
            om = re.match(r"([ABCD])\.\s*(.*)", r)
            if om and (stage == "q" or om.group(1) > (last_opt or "@")):
                last_opt = om.group(1)
                cur["opts"][last_opt].append(om.group(2))
                stage = "opt"
            elif stage == "q":
                cur["q"].append(r)
            else:
                cur["opts"][last_opt].append(r)
        elif stage == "ans":
            if r.startswith("Explanation"):
                stage = "exp"
                rest = re.sub(r"^Explanation\s*", "", r)
                rest = re.sub(rf"^{_STATUS}(?=\S)", "", rest)
                if rest:
                    cur["sol"].append(rest)
            elif not re.fullmatch(_STATUS, r):
                pass                                              # stray fragment between answer and explanation
        elif stage == "exp":
            if not re.fullmatch(_STATUS, r):
                cur["sol"].append(r)
    for q in qs:
        q["q"] = " ".join(q["q"]).strip()
        q["opts"] = {L: " ".join(v).strip() for L, v in q["opts"].items()}
        q["sol"] = " ".join(q["sol"]).strip()
    return qs


def read_retina_rows(path):
    if pymupdf:
        return _retina_rows_pymupdf(path)
    try:
        return _retina_rows_poppler(path)
    except (OSError, Exception) as e:
        raise SystemExit("Retina PDFs need pymupdf (pip install pymupdf) or poppler (pkg install poppler): %s" % e)



# ---------------------------------------------------------------- type 4: Retina LMS "Exam Result" (mobile web)
# "Question N / Marks / Skipped / text / A / option / B / ... / Your answer: X / Correct answer: Y / Explanation"
# poppler's pdftotext keeps KaTeX subscripts (B1, B12) that pymupdf drops, so it is preferred here.
_NOISE2 = re.compile(r"(Marks:.*|Skipped|Correct|Incorrect|Correct answer|Your answer|"
                     r"Show Questions with Answer & Explanation|All Questions \(\d+\))")


def _retina2_lines(path):
    import subprocess
    try:
        txt = subprocess.run(["pdftotext", path, "-"], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        if not pymupdf:
            raise SystemExit("this PDF type needs poppler (pkg/apt install poppler-utils) or pymupdf")
        txt = "\n".join(p.get_text() for p in pymupdf.open(path))      # fallback: loses KaTeX subscripts
    return txt.replace("\f", "\n").split("\n")


def parse_retina2(lines):
    lines = [_clean_bn(l) for l in lines]
    lines = [l for l in lines if l]
    heads = [i for i, l in enumerate(lines) if re.fullmatch(r"Question\s+\d+", l)]
    qs = []
    for k, h in enumerate(heads):
        end = heads[k + 1] if k + 1 < len(heads) else len(lines)
        blk = [l for l in lines[h + 1:end] if not _NOISE2.fullmatch(l)]
        q = {"n": int(lines[h].split()[1]), "status": "", "ans": "", "q": "", "opts": {}, "sol": "",
             "regions": {}, "vec": {}}
        ya = next((i for i, l in enumerate(blk) if l.startswith("Your answer:")), None)
        ex = next((i for i, l in enumerate(blk) if l == "Explanation" or l.startswith("Explanation ")), None)
        cut = ya if ya is not None else (ex if ex is not None else len(blk))
        body, tail = blk[:cut], blk[cut:]
        yours = ""
        for l in tail:
            m = re.match(r"Your answer:\s*(.*)", l)
            if m and not yours:
                yours = m.group(1).strip()
            m = re.match(r"Correct answer:\s*([ABCD])", l)
            if m:
                q["ans"] = m.group(1)
        yl = yours[:1] if re.fullmatch(r"[ABCD]", yours) else ""
        q["status"] = "skipped" if not yl else ("correct" if yl == q["ans"] else "incorrect")
        # question text / options by lone letter lines A, B, C, D in order
        pos, want = {}, "A"
        for i, l in enumerate(body):
            if l == want:
                pos[want] = i
                want = chr(ord(want) + 1)
                if want > "D":
                    break
        if "A" in pos:
            q["q"] = " ".join(body[:pos["A"]]).strip()
            order = [L for L in "ABCD" if L in pos]
            for j, L in enumerate(order):
                stop = pos[order[j + 1]] if j + 1 < len(order) else len(body)
                q["opts"][L] = " ".join(body[pos[L] + 1:stop]).strip()
        else:
            q["q"] = " ".join(body).strip()
        if ex is not None:
            first = re.sub(r"^Explanation\s*", "", blk[ex])
            q["sol"] = " ".join(([first] if first else []) + [l for l in blk[ex + 1:]]).strip()
        qs.append(q)
    return qs

# ---------------------------------------------------------------- type detection
def _probe_text(path, pages=3):
    if pymupdf:
        d = pymupdf.open(path)
        return "\n".join(d[i].get_text() for i in range(min(pages, len(d))))
    try:
        import subprocess
        return subprocess.run(["pdftotext", "-l", str(pages), path, "-"], capture_output=True, text=True).stdout
    except OSError:
        _pdfminer_shim()
        from pdfminer.high_level import extract_text
        return extract_text(path, maxpages=pages)


def detect_kind(path):
    """'retina' (Q1. ...), 'retina2' (Question 1 / Marks / A on its own line), or 'map' (MAP daily / monthly)"""
    t = _probe_text(path)
    if re.search(r"Correct answer:\s*[ABCD]", t):
        if re.search(r"(?m)^Q\d+\.", t):
            return "retina"
        if re.search(r"(?m)^Question\s+\d+\s*$", t):
            return "retina2"
    return "map"


# ---------------------------------------------------------------- markdown
def _crop(doc, region, path, dpi=160):
    """Crop a region to a PNG file; path=None returns PNG bytes instead."""
    y0, y1, x0, x1 = region
    pi = int(y0 // 10000)
    off = pi * 10000
    page = doc[pi]
    clip = pymupdf.Rect(x0, max(0, y0 - off), min(x1, page.rect.width), min(y1 - off, page.rect.height))
    clip.y1 = max(clip.y0 + 4, clip.y1 - 2)              # drop the next row's top edge
    pix = page.get_pixmap(dpi=dpi, clip=clip)
    if path is None:
        data = pix.tobytes("png")
        try:                                             # trim blank margins if Pillow exists
            import io
            from PIL import Image, ImageChops
            im = Image.open(io.BytesIO(data)).convert("RGB")
            # erase the green answer tick / answer-box border so crops never reveal the answer
            im.putdata([(255, 255, 255) if g - max(r, b) > 18 else (r, g, b)
                        for r, g, b in im.getdata()])
            box = ImageChops.difference(im, Image.new("RGB", im.size, (255, 255, 255))).getbbox()
            if box:
                pad = 4
                im = im.crop((max(0, box[0] - pad), max(0, box[1] - pad),
                              min(im.width, box[2] + pad), min(im.height, box[3] + pad)))
                out = io.BytesIO(); im.save(out, "PNG", optimize=True); data = out.getvalue()
        except Exception:
            pass
        return data
    pix.save(str(path))


def to_markdown(qs, keep_status=False, doc=None, img_dir=None, tag="Q"):
    blocks, warn, embedded = [], [], {}
    for k, q in enumerate(qs, 1):
        def img(key):                                    # crop a formula region -> ![](img/..)
            if not (doc and img_dir and q["regions"].get(key)):
                return ""
            if img_dir == ":data":                       # embed as data URI (no files to store)
                import base64
                png = _crop(doc, q["regions"][key], None, dpi=130)
                tok = f"@@IMG{len(embedded)}@@"
                embedded[tok] = "![](data:image/png;base64," + base64.b64encode(png).decode() + ")"
                return tok
            name = f"{tag}{k}_{key}.png"
            Path(img_dir).mkdir(parents=True, exist_ok=True)
            _crop(doc, q["regions"][key], Path(img_dir) / name)
            return f"![]({Path(img_dir).name}/{name})"

        opts = []
        unresolved = []
        for L in "ABCD":
            o = q["opts"].get(L, "")
            if q["vec"].get(L) or not o:
                im = img(L)
                if im:
                    o = im                              # image only: PDF text for these is garbled fragments
                else:
                    unresolved.append(L)
                    o = o or "(formula – see image above)"
                warn.append(f"Q{q['n']}: option {L} has a formula drawn as vector"
                            + (" -> cropped to image" if im else " -> fill by hand"))
            opts.append(o)
        if unresolved and q["regions"].get("ALL"):       # no per-option region: crop the whole block once
            q["regions"]["Q"] = q["regions"]["ALL"]
            q["vec"]["Q"] = True
            q["q"] = ""
            opts = [f"Option {L}" for L in "ABCD"]      # the image already shows the real options
            warn[:] = [w for w in warn if not w.startswith(f"Q{q['n']}: option")]
            warn.append(f"Q{q['n']}: options not separable -> whole question cropped as one image")
        if q["vec"].get("Q") or not q["q"]:
            im = img("Q")
            q["q"] = (q["q"] + "\n\n" + im).strip() if im else (q["q"] or "(question is an image – see PDF)")
            warn.append(f"Q{q['n']}: question has a formula drawn as vector" + (" -> cropped to image" if im else " -> check by hand"))
        if not q["ans"]:
            warn.append(f"Q{q['n']}: no tick found, answer left empty")
        blocks.append(
            f"### Q {k}\n\n{q['q']}\n\n"
            + "\n".join(f"* {o}" for o in opts)
            + f"\n\n## answer\n{q['ans']}\n\n## explanation\n{q['sol']}\n\n## status\n"
            + (q["status"] if keep_status else "")
            + "\n"
        )
    text = repair_markdown("\n".join(blocks))
    for tok, uri in embedded.items():                    # restore after text repair
        text = text.replace(tok, uri)
    return text, warn


def convert(pdf_path, keep_status=False, backend="auto", img_dir=None, kind=None):
    """-> (markdown, warnings). kind: 'map' | 'retina' | None (auto-detect)"""
    kind = kind or detect_kind(pdf_path)
    if kind == "retina":
        return to_markdown(parse_retina(read_retina_rows(pdf_path)), keep_status)
    if kind == "retina2":
        return to_markdown(parse_retina2(_retina2_lines(pdf_path)), keep_status)
    doc = pymupdf.open(pdf_path) if (pymupdf and img_dir) else None
    tag = re.sub(r"\W", "", Path(pdf_path).stem)[:12] + "_"
    return to_markdown(parse(*read_pdf(pdf_path, backend)), keep_status, doc, img_dir, tag)


def check_env():
    """python pdf2md.py --check : shows which backends exist and which PDF types will work"""
    import shutil
    pm = bool(pymupdf)
    shimmed = _pdfminer_shim()
    try:
        import pdfminer  # noqa: F401
        pmn = True
    except ImportError:
        pmn = False
    pop = shutil.which("pdftotext")
    print("python      :", sys.version.split()[0])
    print("pymupdf     :", ("OK " + pymupdf.__version__) if pm else "missing   -> pip install pymupdf")
    print("pdfminer.six:", ("OK" + (" (broken 'cryptography' bypassed - fine)" if shimmed else "")) if pmn
          else "missing   -> pip install --no-deps pdfminer.six && pip install charset-normalizer")
    print("pdftotext   :", pop or "missing   -> Termux: pkg install poppler | Linux: apt install poppler-utils")
    print("bijoy2unicode: OK")  # imported at the top, the script would not start without it
    print("\nPDF types that will work:")
    print("  map     (MAP daily / monthly) :", "yes" if (pm or pmn) else "NO (need pymupdf or pdfminer.six)")
    print("  retina  (Q1. style)           :", "yes" if (pm or pop) else "NO (need pymupdf or poppler)")
    print("  retina2 (Question 1 / Marks)  :", "yes (best quality)" if pop else
          ("yes, but formulas/subscripts like B12 are lost (install poppler)" if pm else "NO (need poppler or pymupdf)"))


def collect_pdfs(paths):
    out = []
    for p in map(Path, paths):
        out += sorted(f for f in p.rglob("*") if f.suffix.lower() == ".pdf") if p.is_dir() else [p]
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", nargs="*", help="PDF file(s) and/or folder(s) (batch)")
    ap.add_argument("--check", action="store_true", help="show installed backends and what will work")
    ap.add_argument("-o", "--out", help="output .md file (single PDF) or output folder (batch)")
    ap.add_argument("--status", action="store_true", help="keep correct/incorrect/skipped from PDF")
    ap.add_argument("--images", metavar="DIR", help="crop formulas the PDF drew as vectors into PNGs in DIR "
                    "(needs pymupdf) and embed them as ![](DIR/x.png)")
    ap.add_argument("--backend", choices=["auto", "pymupdf", "miner"], default="auto")
    ap.add_argument("--type", choices=["auto", "map", "retina", "retina2"], default="auto")
    a = ap.parse_args()
    if a.check:
        return check_env()
    if not a.pdf:
        ap.error("give a PDF file or folder (or use --check)")
    pdfs = collect_pdfs(a.pdf)
    if not pdfs:
        sys.exit("no PDF found")
    single = len(pdfs) == 1 and not (a.out and not a.out.lower().endswith(".md"))
    outdir = None if single else Path(a.out or "questions")
    rows, fail = [], 0
    for p in pdfs:
        try:
            kind = None if a.type == "auto" else a.type
            kind = kind or detect_kind(str(p))
            out = Path(a.out) if single and a.out else (p.with_suffix(".md") if single else outdir / (p.stem + ".md"))
            img = (out.parent / a.images) if a.images else None
            md, warn = convert(str(p), a.status, "miner" if a.backend == "miner" else "auto", img, kind)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(md, encoding="utf-8")
            n = md.count("### Q ")
            na = len(re.findall(r"(?m)^## answer\n[ABCD]$", md))
            rows.append((p.name, kind, n, na, len(warn), out))
            for w in warn[:8]:
                print(f"  WARN {p.name}: {w}", file=sys.stderr)
            if len(warn) > 8:
                print(f"  WARN {p.name}: ... {len(warn) - 8} more", file=sys.stderr)
        except (Exception, SystemExit) as e:             # keep going in batch mode
            fail += 1
            print(f"FAIL {p.name}: {e}   (run: python pdf2md.py --check)", file=sys.stderr)
    print(f"\n{'file':34} {'type':7} {'Q':>4} {'ans':>4} {'warn':>4}  output")
    for name, kind, n, na, nw, out in rows:
        print(f"{name[:34]:34} {kind:7} {n:>4} {na:>4} {nw:>4}  {out}")
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
