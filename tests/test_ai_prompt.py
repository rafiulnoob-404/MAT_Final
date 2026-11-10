import threading
import unittest
from unittest.mock import patch

from flask import Flask
from tools.ai import ai_prompt, create_blueprint
from tools.cards import parse_cards, serialize_cards
from tools.questions import parse as parse_questions, serialize as serialize_questions


class FakeQuestions:
    def __init__(self, answer="", options=None):
        self.question = {"n": 1, "stem": ["What is 2 + 2?"],
                         "opts": options or ["3", "4", "5", "6"],
                         "ans": answer, "exp": "", "st": ""}

    def read(self, name):
        return "", [self.question]

    def update(self, name, number, **fields):
        self.question.update(fields)
        return self.question


class PromptTests(unittest.TestCase):
    def test_prompt_contains_user_rules_and_question(self):
        question = FakeQuestions("B").question
        prompt = ai_prompt(question)
        for expected in ("Style with Rich Formatting", "Eliminate Incorrect Options",
                         "Provide a Mnemonic or Trick", "Handle Errors and Missing Answers",
                         "HTML <u> or <mark>", "instead of inline code backticks",
                         "Markdown blockquotes (> ...)",
                         "What is 2 + 2?", "B. 4", "Correct answer: B"):
            self.assertIn(expected, prompt)
        self.assertNotIn("inline code highlights (`concept`)", prompt)
        self.assertNotIn("ANSWER: UNKNOWN", prompt)
        self.assertFalse(any('\u0980' <= char <= '\u09ff' for char in prompt))

    def run_explain(self, answer, reply, options=None):
        questions = FakeQuestions(answer, options)
        app = Flask(__name__)
        app.register_blueprint(create_blueprint(questions, threading.RLock()))
        with patch('tools.ai.gemini', return_value=reply):
            response = app.test_client().post('/api/ai/explain', json={"file": "U_OFF_P_B_1.md", "n": 1})
        self.assertEqual(response.status_code, 200)
        return questions.question, response.get_json()

    def test_unknown_answer_saves_selected_option_and_strips_metadata(self):
        question, result = self.run_explain("", "ANSWER: B\n\n- ✅ **B** is correct.")
        self.assertEqual(question['ans'], 'B')
        self.assertEqual(result['explanation'], '- ✅ **B** is correct.')

    def test_ambiguous_answer_stays_empty(self):
        question, result = self.run_explain("", "ANSWER: UNKNOWN\n\n- No option is correct.")
        self.assertEqual(question['ans'], '')
        self.assertEqual(result['ans'], -1)
        self.assertEqual(result['explanation'], '- No option is correct.')

    def test_flashcard_prompt_rules_and_original_input(self):
        app = Flask(__name__)
        app.register_blueprint(create_blueprint(FakeQuestions(), threading.RLock()))
        original = "প্রশ্ন $x$ <u>value</u>"
        with patch('tools.ai.gemini', return_value="**Fact**") as gemini:
            response = app.test_client().post('/api/ai/format-card', json={"body": original})
        self.assertEqual(response.status_code, 200)
        prompt = gemini.call_args.args[0]
        instructions, supplied = prompt.split("Flashcard Text:\n", 1)
        self.assertEqual(supplied, original)
        self.assertIn("[আজমল স্যার-২০২৬, পৃ-৩৪২]", instructions)
        self.assertIn("[উন্মেষ মেডিট্রিক্স, পৃ-২১৭]", instructions)
        for rule in ("Banglish", "existing headings/headlines", "strictly tabular",
                     "Markdown blockquotes (> ...)", "Preserve existing blockquotes",
                     "<u>", "<mark>", "instead of inline code backticks",
                     "author names", "page numbers", "No preamble"):
            self.assertIn(rule, instructions)
        self.assertNotIn("inline code (`key term`)", instructions)
        self.assertEqual(response.get_json()['body'], '**Fact**')

    def test_does_not_save_nonexistent_or_existing_option(self):
        question, _ = self.run_explain("", "ANSWER: D\n\n- Missing option.", ["3", "4"])
        self.assertEqual(question['ans'], '')
        question, _ = self.run_explain("A", "ANSWER: B\n\n- The key may be wrong.")
        self.assertEqual(question['ans'], 'A')

    def test_blockquote_and_mark_round_trip(self):
        exp = (
            "* ❌ **একটি:** একটি নিউক্লিয়াস থাকে মসৃণ পেশিতে (<mark>Smooth muscle</mark>)।\n\n"
            "🧠 **স্মরণ রাখার শর্টকাট / ট্রিক (Mnemonic):**\n"
            "> <mark>**S**keletal = **S**ynctium (বহুকেন্দ্রিক)</mark>\n"
            "> অর্থাৎ, **অসংখ্য** (<mark>Multiple</mark>)!"
        )
        questions = FakeQuestions("A")
        app = Flask(__name__)
        app.register_blueprint(create_blueprint(questions, threading.RLock()))
        res = app.test_client().post('/api/ai/explanation',
                                     json={"file": "U_ON_P_B_2.md", "n": 1, "text": exp})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()['explanation'], exp)

        md_doc = serialize_questions("", [questions.question])
        _, parsed_qs = parse_questions(md_doc)
        self.assertEqual(parsed_qs[0]['exp'], exp)

        card_doc = serialize_cards([{"id": 1, "color": "#fff3a3", "body": exp}])
        parsed_cards = parse_cards(card_doc)
        self.assertEqual(parsed_cards[0]['body'], exp)


if __name__ == '__main__':
    unittest.main()
