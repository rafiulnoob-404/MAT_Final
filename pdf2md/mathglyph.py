"""Repair math/symbol glyphs that survive a PDF -> Markdown conversion.

Exam PDFs very often set the *math* part of a question (the `=`, the `×`, the
exponents, `α`, `Δ`, `π`, ...) in a Type-1 symbol font.  When the text is
extracted, those bytes are decoded as MacRoman, so the Markdown keeps the
*typographic* look-alike instead of the math character:

    stored   renders as   meaning
    ------   ----------   ------------------------------------
    ¡        −            minus sign                (130x in the data)
    £        ×            multiplication            (23x)
    µ  ¶     (  )         brackets around a fraction (3x each)
    ¢        Δ            delta
    ¤        +            plus   (H+¤ lost H⁺)
    ®        α            alpha
    ¯        ▁            a label the PDF dropped
    ±        °            degree
    «        ⇌            equilibrium arrow
    º        λ            wavelength (7000ºA is really 7000 Å)
    ¼        π            pi
    ¹        µ            micro (micrometre)
    ¸        ≥            greater-or-equal
    :        .            decimal point in numbers (1:0 -> 1.0)
    )        ⇒            "therefore" arrow the math font lost

`repair(text)` fixes one span, `repair_markdown(text)` fixes a whole
question/card document while copying Bangla runs verbatim.
"""

import re

# Characters that are essentially never legitimate in this corpus and are very
# common as a mistranslated math glyph.  '°' and '±' are handled contextually in
# _degrees() because they are also legitimate unit symbols.
GLYPHS = {
    "\u00a1": "-",          # ¡  -> minus
    "\u00a2": "\u0394",     # ¢  -> Δ
    "\u00a3": "\u00d7",     # £  -> ×
    "\u00a4": "+",          # ¤  -> +
    "\u00a5": "\u2192",     # ¥  -> →
    "\u00a6": "|",          # ¦  -> |
    "\u00a7": "\u221a",     # §  -> √
    "\u00a9": "\u2192",     # ©  -> →
    "\u00aa": "\u221d",     # ª  -> ∝
    "\u00ab": "\u21cc",     # «  -> ⇌
    "\u00ac": "\u2260",     # ¬  -> ≠
    "\u00ad": "",           # soft hyphen
    "\u00ae": "\u03b1",     # ®  -> α
    "\u00af": "\u2192",     # ¯  -> → (stand-in for a dropped label)
    "\u00b4": "\u2032",     # ´  -> ′
    "\u00b5": "(",          # µ  -> (
    "\u00b6": ")",          # ¶  -> )
    "\u00b8": "\u2265",     # ¸  -> ≥
    "\u00bb": "\u2265",     # »  -> ≥
    "\u00bc": "\u03c0",     # ¼  -> π
    "\u00bd": "\u2211",     # ½  -> ∑
    "\u00be": "\u222b",     # ¾  -> ∫
    "\u00bf": "\u221e",     # ¿  -> ∞
}

SUP = str.maketrans("0123456789+-", "\u2070\u00b9\u00b2\u00b3\u2074\u2075\u2076\u2077\u2078\u2079\u207a\u207b")
SUB = str.maketrans("0123456789+-", "\u2080\u2081\u2082\u2083\u2084\u2085\u2086\u2087\u2088\u2089\u208a\u208b")

NUL = "\u0000"
DEG = "\uf8ff"   # private-use sentinel: "this degree ring is a real °"

# " ) " between two expressions is the "therefore" arrow the math font dropped.
# Matched on the ORIGINAL text, before µ/¶ become real brackets.  Requires a
# closing bracket or word before it, so ordinary "( b )" grouping is untouched.
_ARROW = re.compile(r"(?<=[\w\]])\s*\)\s*(?=[A-Za-z\u0980-\u09ff(\[]\w)")
# unit with a flattened exponent: cm-1 -> cm⁻¹
_UNITS = "mol|cm|mm|nm|km|dm|kg|mg|mL|kJ|Pa|Hz|dB|m|g|L|s|J|K|W|V|A|N|C|T|F|Hz"
_UNIT_EXP = re.compile(rf"(?<![A-Za-z])({_UNITS})[ ]?-(\d)(?!\d)")
# powers of ten: 10-4 -> 10⁻⁴ (the sign is what tells us it was an exponent)
_POW10 = re.compile(r"(?<![\d.])10([\u2212-])(\d{1,2})(?!\d)")
# ion charge flattened into the formula: H+ -> H⁺, Ca2+ -> Ca²⁺
_ION = re.compile(r"(?<![A-Za-z0-9])((?:[A-Z][a-z]?\d*){1,3})[ ]?(\d*)([+]{1,3}|-[+-]?)(?![A-Za-z0-9])")
# electron configuration: 1s22s22p6 -> 1s²2s²2p⁶
_SHELL = re.compile(r"(?<=[spdf])(1[0-4]|[0-9])")
_SHELL_RUN = re.compile(r"(?<![\w.])(?:\d[spdf](?:1[0-4]|[0-9])){2,}(?![\w])")


def _arrows(text):
    return _ARROW.sub(" \u21d2 ", text)


def _degrees(text):
    """'±' (0xB1) and '°' (0xB0) are degree signs next to a unit, labels elsewhere."""
    text = re.sub(r"\u00b1(?=\s*[CF]\b)", DEG, text)     # 25±C
    text = text.replace("\u00b1", DEG)                   # 10± - 25±C
    text = re.sub(r"\u00ba\s*A(?![A-Za-z])", "\u00c5", text)         # 7000ºA -> Å
    text = re.sub(r"\u00ba(?![A-Za-z])", "\u03bb", text)             # º -> λ
    text = re.sub(r"(?<=\d)\u00b0", DEG, text)                 # 20°C
    text = re.sub(r"\u00b0(?=\s*[CF]\b)", DEG, text)
    # a ring before a dash is a Greek label, but only when it is not a degree
    # in a temperature range like "10° - 25°C"
    text = re.sub(r"(?<![\d\s\uf8ff])\u00b0(?=\s*-)", "\u03b3", text)
    return text.replace("\u00b0", DEG).replace(DEG, "\u00b0")


def _protect_micro(text):
    """A 'µ' directly in front of a unit is a real micro sign, not a bracket."""
    return re.sub(r"\u00b5(?=\s*(?:m|g|L|s|F|A|W|V|mol|M)\b)", NUL + "U" + NUL, text)


def _numbers(text):
    """1:0 -> 1.0, 14:28 -> 14.28 (the PDF sets the decimal point as ':')."""
    return re.sub(r"(?<=\d):(?=\d)", ".", text)


def _powers(text):
    text = _POW10.sub(lambda m: "10" + (m.group(1).translate(SUP) if m.group(1) == "-" else "")
                      + m.group(2).translate(SUP), text)
    # unit exponents: cm-1 -> cm⁻¹  (the flattened sign is the tell)
    return _UNIT_EXP.sub(lambda m: m.group(1) + (m.group(2).translate(SUP) if m.group(2) == "-"
                                                 else "\u207b" + m.group(2).translate(SUP)), text)


def _units(text):
    """cm-1 already lost its minus in _powers; put a superscript 1 back."""
    return re.sub(rf"(?<=[A-Za-z0-9])({_UNITS})(?=\u00b9|-)", lambda m: m.group(1), text)


def _ions(text):
    def fix(m):
        body, num, sign = m.group(1), m.group(2), m.group(3)
        if not re.search(r"[A-Z]", body):
            return m.group(0)
        if not num and sign == "+":
            return body + "\u207a"
        if not num:
            return body + "\u207b"
        body = re.sub(r"(?<=[A-Za-z])(\d)", lambda d: d.group(1).translate(SUB), body)
        return body + num.translate(SUB) + ("\u207a" if "+" in sign else "\u207b")
    return _ION.sub(fix, text)


def _shells(text):
    """1s22s22p6 -> 1s²2s²2p⁶ (only when it looks like an electron configuration)."""
    return _SHELL_RUN.sub(lambda m: _SHELL.sub(lambda s: s.group(1).translate(SUP), m.group(0)), text)


def _spaces(text):
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s+([,.;:?!)\]])", r"\1", text)
    text = re.sub(r"([(\[])\s+", r"\1", text)
    return text.strip()


def repair(text):
    """Fix one plain-text span (no Bangla-aware logic)."""
    if not text:
        return text
    trigger = (any(ch in GLYPHS for ch in text) or "\u00b9" in text
               or re.search(r"\d:\d|\d[spdf]\d|[\u00b0\u00b1\u00ba\u00b5]", text))
    if not trigger:
        return text
    # preserve leading/trailing whitespace exactly (newlines separate Markdown blocks)
    core = text.strip()
    if not core:
        return text
    head = text[:len(text) - len(text.lstrip())]
    tail = text[len(text.rstrip()):]
    core = _arrows(core)              # must run before µ/¶ become brackets
    core = _degrees(core)
    core = _protect_micro(core)
    core = "".join(GLYPHS.get(ch, ch) for ch in core)
    # '¹' before a unit is a micro sign (¹m = µm), but a real superscript
    # exponent differs: it follows '⁻' and belongs to the unit (mol L⁻¹ s⁻¹)
    core = re.sub(r"(?<![\u207b\u2070-\u2079])\u00b9(?=\s*(?:m|g|L|s|F|A|W|V|mol)\b)",
                  "\u00b5", core)
    core = core.replace(NUL + "U" + NUL, "\u00b5")
    core = core.replace("++", "\u207a")                       # H+¤ lost its superscript +
    core = _numbers(core)
    core = _powers(core)
    core = _ions(core)
    core = _shells(core)
    core = _spaces(core)
    return head + core + tail


_BANGLA = r"[\u0980-\u09ff\u200c\u200d\u0964\u0965]+"


def repair_markdown(text):
    """Repair all non-Bangla spans of a question/card Markdown document.

    Bangla runs are copied verbatim, so no Bangla vowel sign is ever rewritten,
    and the function is idempotent.
    """
    if not text:
        return text
    out, pos = [], 0
    for m in re.finditer(_BANGLA, text):
        out.append(repair(text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(repair(text[pos:]))
    return "".join(out)


def repair_file(path):
    """Repair a Markdown file in place; returns True when it changed."""
    from pathlib import Path
    p = Path(path)
    original = p.read_text(encoding="utf-8")
    fixed = repair_markdown(original)
    if fixed == original:
        return False
    p.write_text(fixed, encoding="utf-8")
    return True
