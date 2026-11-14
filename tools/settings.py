"""Application configuration loaded from environment variables."""

import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QDIR = os.environ.get("QUESTIONS_DIR", os.path.join(BASE, "questions"))
FDIR = os.environ.get("FLASHCARD_DIR", os.path.join(BASE, "flashcard"))
FC_SUBJECTS = {
    "B": "B.md",
    "C": "C.md",
    "P": "P.md",
    "GK": "GK.md",
    "E": "E.md",
    "general": "general.md",
}
FC_DEFAULT_SUBJECT = "general"
FC_FILE = FC_SUBJECTS[FC_DEFAULT_SUBJECT]
FC_LEGACY_FILE = "card.md"

# GitHub-backed storage (enabled only when both token and repository are set).
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GH_REPO = os.environ.get("DATA_REPO", "").strip().strip("/")
GH_BRANCH = os.environ.get("DATA_BRANCH", "data")
GH_PATH = os.environ.get("DATA_PATH", "questions").strip("/")
GH_FC = os.environ.get("DATA_FC_PATH", "flashcard").strip("/")
try:
    SYNC_SECS = max(10, int(os.environ.get("SYNC_SECS", "60")))
except ValueError:
    SYNC_SECS = 60
GH = bool(GH_TOKEN and GH_REPO)

# Gemini.
GEM_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest")
GEM_BASE = "https://generativelanguage.googleapis.com/v1beta/models/"
