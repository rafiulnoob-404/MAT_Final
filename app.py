"""Flask application entry point and external-service wiring."""

import atexit
import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

from tools import settings
from tools.cards import CardService, create_blueprint as cards_blueprint
from tools.health import create_blueprint as health_blueprint
from pdf2md.web import create_blueprint as pdf_converter_blueprint
from tools.ai import AIError, create_blueprint as ai_blueprint
from tools.web import bp as web_blueprint
from tools.questions import QuestionService, create_blueprint as questions_blueprint

app = Flask(__name__)

if bool(settings.GH_TOKEN) != bool(settings.GH_REPO):
    print("[config] set BOTH GITHUB_TOKEN and DATA_REPO to enable GitHub storage", flush=True)

LOC = {"q": settings.QDIR, "f": settings.FDIR}
GHP = {"q": settings.GH_PATH, "f": settings.GH_FC}
cache = {}        # (kind, name) -> [text, sha]
dirty = set()     # keys changed but not yet committed to GitHub
lock = threading.RLock()


# ---------------- storage ----------------
def gh_req(method, path, body=None, query=""):
    url = f"https://api.github.com/repos/{settings.GH_REPO}/contents/{path}{query}"
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {settings.GH_TOKEN}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "practice-hub", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def gh_fetch(kind, name):
    try:
        result = gh_req("GET", f"{GHP[kind]}/{name}", query=f"?ref={settings.GH_BRANCH}")
        value = [base64.b64decode(result["content"]).decode("utf-8"), result["sha"]]
    except urllib.error.HTTPError as error:
        if error.code != 404 or kind != "f":
            raise
        value = ["", None]
    cache[(kind, name)] = value
    return value


def gh_commit(key):
    kind, name = key
    path = f"{GHP[kind]}/{name}"
    for attempt in range(2):
        text, sha = cache[key]
        body = {"message": f"update {name}",
                "content": base64.b64encode(text.encode("utf-8")).decode(),
                "branch": settings.GH_BRANCH}
        if sha:
            body["sha"] = sha
        try:
            result = gh_req("PUT", path, body)
            cache[key][1] = result["content"]["sha"]
            return
        except urllib.error.HTTPError as error:
            if error.code in (409, 422) and attempt == 0:
                try:
                    cache[key][1] = gh_req("GET", path, query=f"?ref={settings.GH_BRANCH}")["sha"]
                except urllib.error.HTTPError as fetch_error:
                    if fetch_error.code != 404:
                        raise
                    cache[key][1] = None
                continue
            raise


def flush():
    """Commit all pending files to GitHub (one commit per changed file)."""
    with lock:
        for key in list(dirty):
            try:
                gh_commit(key)
                dirty.discard(key)
            except Exception as error:
                print(f"[sync] {key} failed: {error}", flush=True)
        return len(dirty)


def sync_loop():
    while True:
        time.sleep(settings.SYNC_SECS)
        flush()


def load(kind, name):
    if settings.GH:
        if (kind, name) not in cache:
            gh_fetch(kind, name)
        return cache[(kind, name)][0]
    try:
        with open(os.path.join(LOC[kind], name), encoding="utf-8") as source:
            return source.read()
    except FileNotFoundError:
        return ""


def save(kind, name, text):
    if settings.GH:
        if (kind, name) not in cache:
            gh_fetch(kind, name)
        cache[(kind, name)][0] = text
        dirty.add((kind, name))
        return
    os.makedirs(LOC[kind], exist_ok=True)
    path = os.path.join(LOC[kind], name)
    with open(path + ".tmp", "w", encoding="utf-8", newline="\n") as target:
        target.write(text)
    os.replace(path + ".tmp", path)


def list_question_names():
    if settings.GH:
        items = gh_req("GET", settings.GH_PATH, query=f"?ref={settings.GH_BRANCH}")
        return [item["name"] for item in items if item["type"] == "file"]
    os.makedirs(settings.QDIR, exist_ok=True)
    return os.listdir(settings.QDIR)


def reload_storage():
    with lock:
        for key in list(cache):
            if key not in dirty:
                del cache[key]


if settings.GH:
    threading.Thread(target=sync_loop, daemon=True).start()
    atexit.register(flush)


# ---------------- services ----------------
questions = QuestionService(
    load=lambda name: load("q", name),
    save=lambda name, text: save("q", name, text),
    list_names=list_question_names,
    lock=lock,
    cache_names=settings.GH,
)
cards = CardService(
    load=lambda name: load("f", name),
    save=lambda name, text: save("f", name, text),
    lock=lock,
    subjects=settings.FC_SUBJECTS,
    default_subject=settings.FC_DEFAULT_SUBJECT,
    legacy_filename=settings.FC_LEGACY_FILE,
)


# ---------------- Gemini ----------------
# ---------------- web and errors ----------------
@app.errorhandler(AIError)
def on_ai_error(error):
    return jsonify(error=str(error)), error.code


@app.errorhandler(Exception)
def on_error(error):
    if isinstance(error, HTTPException):
        return error
    print(f"[error] {request.path}: {error!r}", flush=True)
    return jsonify(error=str(error)), 500


def repair_stored_text():
    """Rewrite every stored question/card file with math glyphs corrected."""
    from pdf2md.mathglyph import repair_markdown
    changed, scanned = [], 0
    with lock:
        for kind, names in (("q", questions.list_names()), ("f", cards.filenames())):
            for name in names:
                scanned += 1
                try:
                    text = load(kind, name)
                except Exception:
                    continue
                fixed = repair_markdown(text)
                if fixed != text:
                    save(kind, name, fixed)
                    changed.append(name)
        questions.clear_caches()
    return {"scanned": scanned, "changed": changed}


def save_new_question_file(name, text):
    """Create a file without replacing existing progress, then invalidate the index."""
    with lock:
        if settings.GH:
            if ("q", name) in dirty:
                raise FileExistsError(name)
            body = {"message": f"add {name} from PDF",
                    "content": base64.b64encode(text.encode("utf-8")).decode(),
                    "branch": settings.GH_BRANCH}
            try:
                result = gh_req("PUT", f"{GHP['q']}/{name}", body)
            except urllib.error.HTTPError as error:
                if error.code in (409, 422):
                    # Verify that a conflict really is an existing file.
                    try:
                        gh_req("GET", f"{GHP['q']}/{name}", query=f"?ref={settings.GH_BRANCH}")
                    except urllib.error.HTTPError as lookup_error:
                        if lookup_error.code != 404:
                            raise
                    else:
                        raise FileExistsError(name) from error
                raise
            cache[("q", name)] = [text, result["content"]["sha"]]
            location = f"GitHub {settings.GH_BRANCH}/{settings.GH_PATH}"
        else:
            os.makedirs(settings.QDIR, exist_ok=True)
            with open(os.path.join(settings.QDIR, name), "x", encoding="utf-8", newline="\n") as target:
                target.write(text)
            location = "local questions folder"
        questions.clear_caches()
        return location


app.register_blueprint(web_blueprint)
app.register_blueprint(ai_blueprint(questions, lock))
app.register_blueprint(pdf_converter_blueprint(save_new_question_file, repair_stored_text))
app.register_blueprint(questions_blueprint(questions))
app.register_blueprint(cards_blueprint(cards))
app.register_blueprint(health_blueprint(
    github_enabled=settings.GH,
    repo=settings.GH_REPO,
    branch=settings.GH_BRANCH,
    path=settings.GH_PATH,
    token_set=bool(settings.GH_TOKEN),
    sync_secs=settings.SYNC_SECS,
    gemini_env=lambda: bool(os.environ.get("GEMINI_API_KEY")),
    gemini_model=settings.GEM_MODEL,
    pending=lambda: len(dirty),
    ignored_files=lambda: questions.legacy_names,
    flush=flush,
    reload_caches=questions.clear_caches,
    reload_storage=reload_storage,
))


if __name__ == "__main__":
    if not settings.GH:
        os.makedirs(settings.QDIR, exist_ok=True)
        os.makedirs(settings.FDIR, exist_ok=True)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0" if os.environ.get("PORT") else "127.0.0.1", port=port, debug=False)
