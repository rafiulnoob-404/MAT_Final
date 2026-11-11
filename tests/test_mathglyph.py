"""Tests for the math-glyph repair that fixes garbled symbols in imported PDFs.

The corpus stores MacRoman look-alikes for math characters (¡ for −, £ for ×,
µ/¶ for brackets, ¢ for Δ, ® for α, ...).  Every case below is a real string
taken from the question files.
"""

import unittest

from pdf2md.mathglyph import repair, repair_markdown


class GlyphRepairTests(unittest.TestCase):
    def check(self, given, expected):
        self.assertEqual(repair_markdown(given), expected)

    def test_minus_sign(self):
        self.check("* ∆G ¸ 0", "* ∆G ≥ 0")
        self.check("* 25 ¡ 30 ±", "* 25 - 30 °")

    def test_multiplication(self):
        self.check("* দ্রবণের £ 10.0 = 1:0 £ 10¡4 M", "* দ্রবণের × 10.0 = 1.0 × 10⁻⁴ M")

    def test_powers_of_ten(self):
        self.check("* 1 £ 10¡4 M", "* 1 × 10⁻⁴ M")
        self.check("10¡10", "10⁻¹⁰")
        self.check("* 14:28 £ 10¡3ºA", "* 14.28 × 10⁻³Å")

    def test_brackets_and_decimal_point(self):
        self.check("পরিমাণ= µ5:0 £ 0:4 ¶ g = 2:0g", "পরিমাণ= (5.0 × 0.4) g = 2.0g")

    def test_chemistry(self):
        self.check("N2O4(g) « 2NO2(g); 25±C এ বিক্রিয়া",
                   "N2O4(g) ⇌ 2NO2(g); 25°C এ বিক্রিয়া")
        self.check("* 1s22s22p6", "* 1s²2s²2p⁶")

    def test_protein_and_bond_labels(self):
        self.check("যকৃত ° -গ্লোবিউলিন ছাড়া", "যকৃত γ -গ্লোবিউলিন ছাড়া")
        self.check("দুটি ¼ বন্ধন", "দুটি π বন্ধন")

    def test_micro_and_wavelength(self):
        self.check("* 0:78 ¹m ¡ 1000 ¹m", "* 0.78 µm - 1000 µm")
        self.check("14:28 £ 103cm¡1", "14.28 × 103cm⁻¹")

    def test_plus_ion(self):
        self.check("pOH কত? H+¤", "pOH কত? H⁺")

    def test_bangla_is_never_touched(self):
        bangla = "সালোকসংশ্লেষণের জন্য তাপমাত্রা প্রয়োজন।"
        self.check(bangla, bangla)

    def test_repair_is_idempotent(self):
        samples = [
            "= 1:0 £ 10¡4M হলে, pOH কত? H+¤",
            "* ∆G ¸ 0",
            "* 0:78 ¹m ¡ 1000 ¹m",
            "N2O4(g) « 2NO2(g); 25±C এ",
            "পরিমাণ= µ5:0 £ 0:4 ¶ g = 2:0g",
        ]
        for sample in samples:
            once = repair_markdown(sample)
            self.assertEqual(repair_markdown(once), once, sample)

    def test_plain_text_untouched(self):
        plain = "কোনটি সঠিক? (*) 4.0"
        self.assertEqual(repair(plain), plain)


if __name__ == "__main__":
    unittest.main()
