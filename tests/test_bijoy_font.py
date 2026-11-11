"""Regression tests for Bijoy/SutonnyMJ font detection during PDF import.

A PDF whose Bangla is set in a Bijoy-encoded font must be converted to Unicode.
Detection used to rely only on the font name, so fonts shipped as "MJ-Normal",
"Jomuna" or with a subset prefix were silently left as raw Bijoy and showed up
in the UI as text like "Møvm wK¬bv‡i †KvbwU ...".
"""

import unittest

from pdf2md.converter import _is_bijoy, _looks_bijoy, _maybe_bijoy, bijoy_to_unicode

RAW_BIJOY = "Møvm wK¬bv‡i †KvbwU mvi‡dKU¨v›U wn‡m‡e e¨eüZ nq?"


class FontDetectionTests(unittest.TestCase):
    def test_common_bijoy_font_names(self):
        for name in ("SutonnyMJ", "SutonnyMJ-Bold", "ABCDF+SutonnyMJ", "Sutonny MJ",
                     "MJ-Normal", "MJ", "MJ-Bold", "Jomuna", "AkaashMJ", "AdvMJ",
                     "SUTONNYMJ", "sutonnymj"):
            self.assertTrue(_is_bijoy(name), name)

    def test_unicode_font_names_are_not_bijoy(self):
        for name in ("Arial", "TimesNewRomanPSMT", "Helvetica", "SolaimanLipi",
                     "Kalpurush", "Nikosh", "Garamond"):
            self.assertFalse(_is_bijoy(name), name)

    def test_subset_prefix_does_not_hide_the_family(self):
        self.assertTrue(_is_bijoy("ABCDEF+MJ-Normal"))

    def test_unhelpful_font_name_falls_back_to_content(self):
        self.assertFalse(_is_bijoy("F1"))
        self.assertTrue(_looks_bijoy(RAW_BIJOY))
        self.assertIn("সারফেকট্যান্ট", _maybe_bijoy(RAW_BIJOY, "F1"))

    def test_english_and_unicode_bangla_are_left_alone(self):
        self.assertEqual(_maybe_bijoy("What is 2 + 2?", "Arial"), "What is 2 + 2?")
        self.assertEqual(_maybe_bijoy("Which one is correct?", "F1"), "Which one is correct?")
        bangla = "কোনটি সঠিক?"
        self.assertEqual(_maybe_bijoy(bangla, "F1"), bangla)
        self.assertFalse(_looks_bijoy(bangla))

    def test_real_sentences_convert_correctly(self):
        cases = {
            "†R‡b iv‡Lv:": "জেনে রাখো:",
            "†gwW‡Kj gv÷vi cÖkœe¨vsK": "মেডিকেল মাস্টার প্রশ্নব্যাংক",
            "†mvwWqvg jivBj mvj‡dU": "সোডিয়াম লরাইল সালফেট",
            "AvB‡mv‡cÖvcvBj A¨vj‡Kvnj": "আইসোপ্রোপাইল অ্যালকোহল",
            "†dbj": "ফেনল",
        }
        for raw, expected in cases.items():
            self.assertIn(expected, _maybe_bijoy(raw, "MJ-Normal"), raw)

    def test_unicode_text_with_bullets_is_not_mistaken_for_bijoy(self):
        # real explanation lines from the stored corpus
        for line in (
            "Az-294; (কশেরুকার প্রকারভেদ); • সারভাইকাল কশেরুকা → 7 টি",
            "Ha-242; (Cycas এর অর্থনৈতিক গুরুত্ব); • Cycas revoluta → বীজ খাদ্য",
            "• **র‍্যাবডোম:** **আলোক রশ্মি গ্রহণ** করে",
            "N2O4(g) « 2NO2(g); 25°C এ বিক্রিয়া",
            "Solution: pH = 14 - pOH = 10",
        ):
            self.assertFalse(_looks_bijoy(line), line)

    def test_long_bijoy_lines_are_detected_without_font_names(self):
        for line in (
            "Møvm wK¬bv‡i †KvbwU mvi‡dKU¨v›U wn‡m‡e e¨eüZ nq?",
            "[nvRvix s¨vi-2026, c„-636; †gwW‡Kj gv÷vi cÖkœe¨vsK, c„-127]",
            "bvBUªvBW Avq‡bi B‡jKUªb web¨vm †KvbwU?",
        ):
            self.assertTrue(_looks_bijoy(line), line)

    def test_glyph_conversion_keeps_word_boundaries(self):
        # "Møvm" must be গ্লাস, not গস্নাস (ø is ্ল in SutonnyMJ)
        self.assertEqual(bijoy_to_unicode("Møvm").strip(), "গ্লাস")


if __name__ == "__main__":
    unittest.main()
