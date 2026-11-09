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

import Config
from routes.Cards import CardService, create_blueprint as cards_blueprint
from routes.Health import create_blueprint as health_blueprint
from routes.pdf import create_blueprint as pdf_converter_blueprint
from routes.ai import AIError, create_blueprint as ai_blueprint
from routes.web import bp as web_blueprint
from routes.Questions import QuestionService, create_blueprint as questions_blueprint

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

if bool(Config.GH_TOKEN) != bool(Config.GH_REPO):
    print("[config] set BOTH GITHUB_TOKEN and DATA_REPO to enable GitHub storage", flush=True)

LOC = {"q": Config.QDIR, "f": Config.FDIR}
GHP = {"q": Config.GH_PATH, "f": Config.GH_FC}
cache = {}        # (kind, name) -> [text, sha]
dirty = set()     # keys changed but not yet committed to GitHub
lock = threading.RLock()


# ---------------- storage ----------------
def gh_req(method, path, body=None, query=""):
    url = f"https://api.github.com/repos/{Config.GH_REPO}/contents/{path}{query}"
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {Config.GH_TOKEN}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "practice-hub", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def gh_fetch(kind, name):
    try:
        result = gh_req("GET", f"{GHP[kind]}/{name}", query=f"?ref={Config.GH_BRANCH}")
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
                "branch": Config.GH_BRANCH}
        if sha:
            body["sha"] = sha
        try:
            result = gh_req("PUT", path, body)
            cache[key][1] = result["content"]["sha"]
            return
        except urllib.error.HTTPError as error:
            if error.code in (409, 422) and attempt == 0:
                try:
                    cache[key][1] = gh_req("GET", path, query=f"?ref={Config.GH_BRANCH}")["sha"]
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
        time.sleep(Config.SYNC_SECS)
        flush()


def load(kind, name):
    if Config.GH:
        if (kind, name) not in cache:
            gh_fetch(kind, name)
        return cache[(kind, name)][0]
    try:
        with open(os.path.join(LOC[kind], name), encoding="utf-8") as source:
            return source.read()
    except FileNotFoundError:
        return ""


def save(kind, name, text):
    if Config.GH:
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
    if Config.GH:
        items = gh_req("GET", Config.GH_PATH, query=f"?ref={Config.GH_BRANCH}")
        return [item["name"] for item in items if item["type"] == "file"]
    os.makedirs(Config.QDIR, exist_ok=True)
    return os.listdir(Config.QDIR)


def reload_storage():
    with lock:
        for key in list(cache):
            if key not in dirty:
                del cache[key]


if Config.GH:
    threading.Thread(target=sync_loop, daemon=True).start()
    atexit.register(flush)


# ---------------- services ----------------
questions = QuestionService(
    load=lambda name: load("q", name),
    save=lambda name, text: save("q", name, text),
    list_names=list_question_names,
    lock=lock,
    cache_names=Config.GH,
)
cards = CardService(
    load=lambda name: load("f", name),
    save=lambda name, text: save("f", name, text),
    lock=lock,
    filename=Config.FC_FILE,
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


def save_new_question_file(name, text):
    """Create a file without replacing existing progress, then invalidate the index."""
    with lock:
        if Config.GH:
            if ("q", name) in dirty:
                raise FileExistsError(name)
            body = {"message": f"add {name} from PDF",
                    "content": base64.b64encode(text.encode("utf-8")).decode(),
                    "branch": Config.GH_BRANCH}
            try:
                result = gh_req("PUT", f"{GHP['q']}/{name}", body)
            except urllib.error.HTTPError as error:
                if error.code in (409, 422):
                    # Verify that a conflict really is an existing file.
                    try:
                        gh_req("GET", f"{GHP['q']}/{name}", query=f"?ref={Config.GH_BRANCH}")
                    except urllib.error.HTTPError as lookup_error:
                        if lookup_error.code != 404:
                            raise
                    else:
                        raise FileExistsError(name) from error
                raise
            cache[("q", name)] = [text, result["content"]["sha"]]
            location = f"GitHub {Config.GH_BRANCH}/{Config.GH_PATH}"
        else:
            os.makedirs(Config.QDIR, exist_ok=True)
            with open(os.path.join(Config.QDIR, name), "x", encoding="utf-8", newline="\n") as target:
                target.write(text)
            location = "local questions folder"
        questions.clear_caches()
        return location


app.register_blueprint(web_blueprint)
app.register_blueprint(ai_blueprint(questions, lock))
app.register_blueprint(pdf_converter_blueprint(save_new_question_file))
app.register_blueprint(questions_blueprint(questions))
app.register_blueprint(cards_blueprint(cards))
app.register_blueprint(health_blueprint(
    github_enabled=Config.GH,
    repo=Config.GH_REPO,
    branch=Config.GH_BRANCH,
    path=Config.GH_PATH,
    token_set=bool(Config.GH_TOKEN),
    sync_secs=Config.SYNC_SECS,
    gemini_env=lambda: bool(os.environ.get("GEMINI_API_KEY")),
    gemini_model=Config.GEM_MODEL,
    pending=lambda: len(dirty),
    ignored_files=lambda: questions.legacy_names,
    flush=flush,
    reload_caches=questions.clear_caches,
    reload_storage=reload_storage,
))


if __name__ == "__main__":
    if not Config.GH:
        os.makedirs(Config.QDIR, exist_ok=True)
        os.makedirs(Config.FDIR, exist_ok=True)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0" if os.environ.get("PORT") else "127.0.0.1", port=port, debug=False)
