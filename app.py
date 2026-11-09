"""Flask application entry point and external-service wiring."""

import atexit
import base64
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

import Config
from Cards import CardService, clean_body, create_blueprint as cards_blueprint
from Health import create_blueprint as health_blueprint
from Questions import QuestionService, ans_index, create_blueprint as questions_blueprint, parse_name

app = Flask(__name__)

if bool(Config.GH_TOKEN) != bool(Config.GH_REPO):
    print("[config] set BOTH GITHUB_TOKEN and DATA_REPO to enable GitHub storage", flush=True)

MODEL_OK = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")
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
class AIError(Exception):
    def __init__(self, message, code=502):
        super().__init__(message)
        self.code = code


def gem_err_text(error):
    try:
        payload = json.loads(error.read().decode("utf-8", "replace"))
        message = (payload.get("error") or {}).get("message") or ""
    except Exception:
        message = ""
    if error.code in (400, 401, 403) and re.search(r"api key|API_KEY|permission|unauthor", message, re.I):
        return "Gemini rejected the API key (invalid, expired or blocked)."
    if error.code == 404:
        return "Gemini model not found. Change the model name in Settings."
    if error.code == 429:
        return "Gemini quota/rate limit reached. Try again shortly."
    return f"Gemini error {error.code}: {message[:200] or error.reason}"


def gemini(prompt, key, model):
    if not key or not key.isascii():
        raise AIError("Gemini API key missing. Set GEMINI_API_KEY on the server or add it in Settings.", 400)
    if not MODEL_OK.match(model or ""):
        raise AIError("Invalid Gemini model name.", 400)
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                       "generationConfig": {"temperature": 0.3, "maxOutputTokens": 2048}}).encode()
    message, payload = "Gemini request failed.", None
    for attempt in range(3):
        req = urllib.request.Request(
            Config.GEM_BASE + model + ":generateContent", data=body, method="POST",
            headers={"Content-Type": "application/json", "x-goog-api-key": key})
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                payload = json.load(response)
            break
        except urllib.error.HTTPError as error:
            message = gem_err_text(error)
            if error.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            code = 429 if error.code == 429 else 400 if error.code in (400, 401, 403, 404) else 502
            raise AIError(message, code)
        except (urllib.error.URLError, OSError, ValueError) as error:
            message = f"Could not reach Gemini: {getattr(error, 'reason', error)}"
            if attempt < 2:
                time.sleep(1.5)
                continue
            raise AIError(message, 502)
    if not isinstance(payload, dict):
        raise AIError(message, 502)
    candidates = payload.get("candidates") or []
    if not candidates:
        reason = (payload.get("promptFeedback") or {}).get("blockReason")
        raise AIError(f"Gemini blocked the request ({reason})." if reason else "Gemini returned no answer.", 502)
    candidate = candidates[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts if not part.get("thought")).strip()
    if not text:
        raise AIError(f"Empty Gemini response ({candidate.get('finishReason', 'unknown')}).", 502)
    return text


def clean_exp(text):
    text = (text or "").strip()
    return re.sub(r"^([ \t]*#{2,3})(?=\s*(?:Q\s*\d|answer\b|explanation\b|status\b))",
                  r"\1#", text, flags=re.M | re.I)


def ai_prompt(question):
    options = "\n".join(f"{'ABCD'[i]}. {option}" for i, option in enumerate(question["opts"][:4]))
    index = ans_index(question)
    known = f"Correct answer: {'ABCD'[index]}" if 0 <= index < 4 else "Correct answer: unknown"
    ask = "" if 0 <= index < 4 else "The first line must be exactly `ANSWER: <A|B|C|D>`, then a blank line.\n"
    return ("You are an admission-test tutor. Explain this MCQ in the same language as the question "
            "(Bangla or English). Output a short paragraph or a compact markdown table. HTML <u>/<mark> "
            "and LaTeX ($...$) are allowed. No preamble, no headings.\n" + ask +
            "\nQuestion:\n" + "\n".join(question["stem"]) + "\n\n" + options + "\n\n" + known)


def body_json():
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def get_question(name, number):
    if not parse_name(name):
        raise AIError("bad file name", 400)
    try:
        number = int(number)
    except (TypeError, ValueError):
        raise AIError("bad question number", 400)
    with lock:
        _, items = questions.read(name)
        question = next((item for item in items if item["n"] == number), None)
        if not question:
            raise AIError("question not found", 404)
        return number, {**question, "opts": list(question["opts"]), "stem": list(question["stem"])}


@app.post("/api/ai/explain")
def ai_explain():
    body = body_json()
    name = body.get("file")
    number, question = get_question(name, body.get("n"))
    key = (request.headers.get("X-Gemini-Key") or os.environ.get("GEMINI_API_KEY", "")).strip()
    model = (request.headers.get("X-Gemini-Model") or Config.GEM_MODEL).strip()
    text = gemini(ai_prompt(question), key, model)
    letter = ""
    match = re.match(r"\s*ANSWER:\s*\(?([A-D])\)?[^\n]*\n?", text, re.I)
    if match:
        letter, text = match.group(1).upper(), text[match.end():]
    explanation = clean_exp(text)
    if not explanation:
        raise AIError("Gemini returned an empty explanation.", 502)
    if not question["exp"].strip():
        fields = {"exp": explanation}
        if letter and ans_index(question) < 0:
            fields["ans"] = letter
        updated = questions.update(name, number, **fields)
        return jsonify(saved=True, explanation=explanation, ans=ans_index(updated))
    return jsonify(saved=False, explanation=explanation, existing=question["exp"])


@app.post("/api/ai/keep")
def ai_keep():
    body = body_json()
    choice = body.get("choice")
    if choice not in ("keep1", "keep2", "both"):
        return jsonify(error="bad choice"), 400
    number, question = get_question(body.get("file"), body.get("n"))
    new = clean_exp(body.get("text"))
    if choice == "keep1":
        return jsonify(ok=True, explanation=question["exp"])
    if not new:
        return jsonify(error="empty explanation"), 400
    explanation = new if choice == "keep2" or not question["exp"].strip() else question["exp"].strip() + "\n\n---\n\n" + new
    questions.update(body["file"], number, exp=explanation)
    return jsonify(ok=True, explanation=explanation)


REF_LINE = re.compile(
    r"^[ \t]*(?:\*\*)?(?:references?|refs?|sources?|citations?|bibliography|তথ্যসূত্র|উৎস)"
    r"(?:\*\*)?[ \t]*[:：\-–].*$", re.I | re.M)


def strip_refs(text):
    text = re.sub(r"\[\s*(?:ref(?:erence)?s?\.?\s*)?\d+(?:\s*[,\-–]\s*\d+)*\s*\]", "", text, flags=re.I)
    text = re.sub(r"\[\^[^\]]*\]", "", text)
    text = re.sub(r"\(\s*(?:ref|source)s?\b[^)]*\)", "", text, flags=re.I)
    text = REF_LINE.sub("", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


@app.post("/api/ai/format-card")
def ai_format_card():
    body = (body_json().get("body") or "").strip()
    if not body:
        return jsonify(error="empty card"), 400
    if len(body) > 8000:
        return jsonify(error="card too long (max 8000 chars)"), 400
    key = (request.headers.get("X-Gemini-Key") or os.environ.get("GEMINI_API_KEY", "")).strip()
    model = (request.headers.get("X-Gemini-Model") or Config.GEM_MODEL).strip()
    prompt = ("Reformat this flashcard text as clean, compact markdown. Rules:\n"
              "- Keep the same language (Bangla/English) and exact meaning; do not add or drop facts.\n"
              "- Fix broken line breaks, spacing, bullets, numbering; use a table only if data is tabular.\n"
              "- Bold key terms sparingly. Keep LaTeX ($...$), <u> and <mark> as is.\n"
              "- Remove all references: [1], [^1], footnotes, citations, URLs used as sources, "
              "'Reference/Source:' lines.\n"
              "- No preamble, no code fences, no headings unless already present.\n\nTEXT:\n" + body)
    text = gemini(prompt, key, model)
    text = re.sub(r"^```(?:markdown|md)?[ \t]*\n|\n```[ \t]*$", "", text.strip())
    output = clean_body(strip_refs(text))
    if not output:
        raise AIError("Gemini returned an empty card.", 502)
    return jsonify(body=output)


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


@app.get("/")
def index():
    return render_template("index.html")


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
