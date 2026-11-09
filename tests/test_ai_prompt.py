import threading
import unittest
from unittest.mock import patch

from flask import Flask
from tools.ai import ai_prompt, create_blueprint


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
                         "HTML <u> or <mark>", "What is 2 + 2?", "B. 4", "Correct answer: B"):
            self.assertIn(expected, prompt)
        self.assertNotIn("ANSWER: UNKNOWN", prompt)

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
        self.assertFalse(any('\u0980' <= char <= '\u09ff' for char in instructions))
        for rule in ("Romanized Bangla", "existing headings/headlines", "strictly tabular",
                     "<u>", "<mark>", "author names", "page numbers", "No preamble"):
            self.assertIn(rule, instructions)
        self.assertEqual(response.get_json()['body'], '**Fact**')

    def test_does_not_save_nonexistent_or_existing_option(self):
        question, _ = self.run_explain("", "ANSWER: D\n\n- Missing option.", ["3", "4"])
        self.assertEqual(question['ans'], '')
        question, _ = self.run_explain("A", "ANSWER: B\n\n- The key may be wrong.")
        self.assertEqual(question['ans'], 'A')


if __name__ == '__main__':
    unittest.main()
