# MAT Final Practice Hub

A Flask application for practising admission-test MCQs, tracking question status, maintaining Markdown flashcards, and generating explanations or card formatting with Gemini.

## Project structure

```text
app.py          Flask entry point, storage wiring, Gemini client and AI routes
Config.py       Environment-based application configuration
Questions.py    Question filename rules, Markdown parser, cache, service and routes
Cards.py        Flashcard parser, service and CRUD routes
Health.py       Health, cache reload and storage sync routes
templates/      Browser interface
```

The split is intentionally minimal. Parsing and business rules live in their domain modules, while `app.py` wires storage and external services together.

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
questions/       Question Markdown files
flashcard/card.md
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

A Gemini key and model can also be supplied from the application's Settings screen.

## Main API groups

- `/api/index`, `/api/questions`, `/api/file/<name>`, `/api/status` — questions
- `/api/cards` — flashcards
- `/api/ai/explain`, `/api/ai/keep`, `/api/ai/format-card` — Gemini features
- `/healthz`, `/api/health`, `/system/health` — health checks
- `/api/reload`, `/api/sync` — cache and GitHub synchronization

## Notes

- Local writes use a temporary file followed by an atomic replacement.
- GitHub changes are cached and committed in the background.
- The application currently has no built-in user authentication; deploy it behind appropriate access controls if it is internet-facing.
- For GitHub-backed storage, use a single Gunicorn worker because the cache and sync queue are process-local.
