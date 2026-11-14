# MAT Final Practice Hub

A Flask application for practising admission-test MCQs, tracking question status, maintaining Markdown flashcards, and generating explanations or card formatting with Gemini.

## Project structure

```text
app.py          Flask entry point and storage wiring
tools/          Configuration, question, flashcard, AI, health and web routes
pdf2md/         PDF-to-Markdown converter and PDF upload route
templates/      Browser interface
```

The split is intentionally minimal. Parsing and business rules live in their domain modules, while `app.py` wires storage and services together.

## Requirements

- Python 3.10+
- Dependencies from `requirements.txt`

Install and run locally:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open <http://127.0.0.1:5000>.

For production:

```bash
gunicorn app:app
```

## Data storage

By default, data is stored locally:

```text
questions/          Question Markdown files
flashcard/B.md      Biology flashcards
flashcard/C.md      Chemistry flashcards
flashcard/P.md      Physics flashcards
flashcard/GK.md     GK flashcards
flashcard/E.md      English flashcards
flashcard/general.md General / uncategorized flashcards
```

Question filenames must follow one of these formats:

```text
U_OFF_P_B_3.md
R_ON_S_GK_2.md
U_ON_MT_1.md
```

GitHub-backed storage is enabled only when both `GITHUB_TOKEN` and `DATA_REPO` are configured.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `QUESTIONS_DIR` | `questions/` | Local question directory |
| `FLASHCARD_DIR` | `flashcard/` | Local flashcard directory |
| `GITHUB_TOKEN` | empty | GitHub storage token |
| `DATA_REPO` | empty | Repository in `owner/repo` form |
| `DATA_BRANCH` | `data` | Data branch |
| `DATA_PATH` | `questions` | Question path in the repository |
| `DATA_FC_PATH` | `flashcard` | Flashcard path in the repository |
| `SYNC_SECS` | `60` | Background GitHub sync interval |
| `GEMINI_API_KEY` | empty | Server-side Gemini API key |
| `GEMINI_MODEL` | `gemini-flash-lite-latest` | Gemini model |
| `PORT` | `5000` | Web server port |

The Gemini key and model are configured only on the server (environment variables);
they are no longer editable from the Settings screen.

## Main API groups

- `/api/index`, `/api/questions`, `/api/file/<name>`, `/api/status` — questions
- `/api/cards` — flashcards
- `/api/ai/explain`, `/api/ai/keep`, `/api/ai/explanation`, `/api/ai/format-card` — Gemini features and explanation editing
- `/api/ai/usage`, `/api/ai/usage/reset` — in-memory AI usage counters
- `/healthz`, `/api/health`, `/system/health` — health checks
- `/api/reload`, `/api/sync` — cache and GitHub synchronization

## Notes

- Local writes use a temporary file followed by an atomic replacement.
- GitHub changes are cached and committed in the background.
- The application currently has no built-in user authentication; deploy it behind appropriate access controls if it is internet-facing.
- For GitHub-backed storage, use a single Gunicorn worker because the cache and sync queue are process-local.
- AI explanations can be edited after they are generated: open **Explain with AI**, then use
  the **✎ Edit** button in the solution box (or in the *New (AI)* column when comparing) to
  change the text before or after saving. Edits are stored through `/api/ai/explanation`.
- AI usage counters (requests, input tokens, output tokens, total tokens) live only in the
  current process's RAM. They are shown in **Settings → AI usage** and reset to zero whenever
  the server restarts or the process memory is cleared.

## Importing exam PDFs

Open **Files → Add PDF**, select a MAP or Retina exam-result PDF, and choose the
coaching, mode, phase, subject and file number. The app converts it to Markdown
and saves the new question file directly to `DATA_PATH` on `DATA_BRANCH` when
GitHub storage is configured, or to `QUESTIONS_DIR` otherwise. It will not
replace an existing file. Conversion warnings should be reviewed; vector
formulas cannot currently be imported as images.

The standalone converter is also available as `python pdf2md/converter.py
--help`. It can optionally crop formula images when used from the command line.
