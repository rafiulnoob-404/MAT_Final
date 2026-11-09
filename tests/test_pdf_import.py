import io
import os
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import pymupdf

import app as main


def retina_pdf():
    doc = pymupdf.open()
    page = doc.new_page()
    for i, line in enumerate(["Q1. What is 2 + 2?", "A. 3", "B. 4", "C. 5", "D. 6",
                              "Your answer: B Correct answer: B", "Explanation", "Two plus two is four."]):
        page.insert_text((50, 50 + i * 20), line)
    data = doc.tobytes()
    doc.close()
    return data


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.qdir = patch.object(main.settings, 'QDIR', self.tmp.name)
        self.qdir.start()
        self.loc = patch.dict(main.LOC, {'q': self.tmp.name})
        self.loc.start()
        self.gh = patch.object(main.settings, 'GH', False)
        self.gh.start()
        main.questions.clear_caches()
        self.client = main.app.test_client()

    def tearDown(self):
        self.gh.stop(); self.loc.stop(); self.qdir.stop()
        main.questions.clear_caches()
        self.tmp.cleanup()

    def upload(self, name='R_ON_P_B_1.md', data=None):
        return self.client.post('/api/pdf-convert', data={'name': name, 'status': 'on',
                                'pdf': (io.BytesIO(data if data is not None else retina_pdf()), 'exam.pdf')})

    def test_real_conversion_and_index(self):
        response = self.upload()
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.json['questions'], 1)
        self.assertEqual(response.json['kind'], 'retina')
        text = Path(self.tmp.name, 'R_ON_P_B_1.md').read_text(encoding='utf8')
        self.assertIn('### Q 1', text)
        self.assertIn('correct', text)
        self.assertEqual(self.client.get('/api/index').json[0]['f'], 'R_ON_P_B_1.md')
        self.assertEqual(self.upload().status_code, 409)
        self.assertEqual(Path(self.tmp.name, 'R_ON_P_B_1.md').read_text(encoding='utf8'), text)

    def test_bad_inputs_not_saved(self):
        self.assertEqual(self.upload('../evil.md').status_code, 400)
        self.assertNotEqual(self.upload(data=b'not a pdf').status_code, 200)
        self.assertEqual(os.listdir(self.tmp.name), [])

    def test_github_direct_write(self):
        with patch.object(main.settings, 'GH', True), patch.object(main.settings, 'GH_BRANCH', 'data'), \
             patch.object(main, 'gh_req', return_value={'content': {'sha': 'newsha'}}) as gh:
            self.assertEqual(main.save_new_question_file('U_ON_MT_2.md', 'hello'), 'GitHub data/questions')
            self.assertEqual(gh.call_args.args[0:2], ('PUT', 'questions/U_ON_MT_2.md'))
            self.assertEqual(gh.call_args.args[2]['branch'], 'data')
            self.assertNotIn(('q', 'U_ON_MT_2.md'), main.dirty)
            main.cache.pop(('q', 'U_ON_MT_2.md'), None)


if __name__ == '__main__':
    unittest.main()
