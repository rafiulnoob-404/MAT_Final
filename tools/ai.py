"""Gemini helpers and AI API routes."""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

from flask import Blueprint, jsonify, request
from tools import settings
from tools.cards import clean_body
from tools.questions import ans_index, parse_name

MODEL_OK = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")

# In-memory usage counters for this process only. They intentionally live in RAM:
# a server restart (or a lost process) resets them to zero.
USAGE_LOCK = threading.Lock()
USAGE_SINCE = time.time()
USAGE = {
    "requests": 0,      # HTTP calls attempted against the Gemini API (retries included)
    "failed": 0,        # calls that ended in an error
    "prompt_tokens": 0,  # input tokens reported by usageMetadata
    "output_tokens": 0,  # generated tokens reported by usageMetadata
    "total_tokens": 0,
    "last_model": "",
    "last_at": None,
}


def _iso(timestamp):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp)) if timestamp else None


def record_usage(model, payload=None, failed=False):
    """Count one Gemini HTTP call and, on success, add its token usage."""
    with USAGE_LOCK:
        USAGE["requests"] += 1
        if failed:
            USAGE["failed"] += 1
        USAGE["last_model"] = model or ""
        USAGE["last_at"] = time.time()
        if payload:
            meta = payload.get("usageMetadata") or {}
            prompt = int(meta.get("promptTokenCount") or 0)
            output = int(meta.get("candidatesTokenCount") or 0)
            total = int(meta.get("totalTokenCount") or 0) or (
                prompt + output + int(meta.get("thoughtsTokenCount") or 0))
            USAGE["prompt_tokens"] += prompt
            USAGE["output_tokens"] += output
            USAGE["total_tokens"] += total


def usage_snapshot():
    with USAGE_LOCK:
        data = dict(USAGE, since=_iso(USAGE_SINCE))
    data["last_at"] = _iso(data["last_at"])
    return data


def reset_usage():
    global USAGE_SINCE
    with USAGE_LOCK:
        for key in USAGE:
            USAGE[key] = 0 if isinstance(USAGE[key], int) else ("" if key == "last_model" else None)
        USAGE_SINCE = time.time()

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


def gemini(prompt, key, model, max_tokens=2048, temperature=0.3, strict=False):
    if not key or not key.isascii():
        raise AIError("Gemini API key missing. Set GEMINI_API_KEY on the server or add it in Settings.", 400)
    if not MODEL_OK.match(model or ""):
        raise AIError("Invalid Gemini model name.", 400)
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                       "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens}}).encode()
    message, payload = "Gemini request failed.", None
    for attempt in range(3):
        req = urllib.request.Request(
            settings.GEM_BASE + model + ":generateContent", data=body, method="POST",
            headers={"Content-Type": "application/json", "x-goog-api-key": key})
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                payload = json.load(response)
            record_usage(model, payload if isinstance(payload, dict) else None)
            break
        except urllib.error.HTTPError as error:
            record_usage(model, failed=True)
            message = gem_err_text(error)
            if error.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            code = 429 if error.code == 429 else 400 if error.code in (400, 401, 403, 404) else 502
            raise AIError(message, code)
        except (urllib.error.URLError, OSError, ValueError) as error:
            record_usage(model, failed=True)
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
    if strict and candidate.get("finishReason") == "MAX_TOKENS":
        raise AIError("Gemini output was cut off. Try a shorter card.", 502)
    return text


def clean_exp(text):
    text = (text or "").strip()
    return re.sub(r"^([ \t]*#{2,3})(?=\s*(?:Q\s*\d|answer\b|explanation\b|status\b))",
                  r"\1#", text, flags=re.M | re.I)


def ai_prompt(question):
    options = "\n".join(f"{'ABCD'[i]}. {option}" for i, option in enumerate(question["opts"][:4]))
    index = ans_index(question)
    known = f"Correct answer: {'ABCD'[index]}" if 0 <= index < 4 else "Correct answer: unknown"
    # The machine-readable line is removed before the explanation is shown.
    answer_instruction = "" if 0 <= index < 4 else (
        "For the first line only, output exactly `ANSWER: A`, `ANSWER: B`, `ANSWER: C`, "
        "`ANSWER: D`, or `ANSWER: UNKNOWN`. Use UNKNOWN when the answer is ambiguous or "
        "not among the options. Follow it with a blank line, then the explanation.\n\n"
    )
    return ("You are a tutor of an admission candidate. Explain the given MCQ directly, "
            "using the same language as the question (Bangla or English).\n\n"
            "Output Format Rules:\n"
            "1. **Style with Rich Formatting:** Do not output plain text. Use bullet points, "
            "bold key terms (**text**), highlighted key concepts (<mark>concept</mark>) "
            "instead of inline code backticks, Markdown blockquotes (> ...) for important "
            "notes or memory tricks, and subtle icons (e.g., ✅, ❌, 💡, 🧠) to make the "
            "text scannable and visually engaging.\n"
            "2. **Explain the Correct Answer:** State the correct option clearly and explain "
            "the underlying reasoning or formula behind it briefly.\n"
            "3. **Eliminate Incorrect Options:** Explain briefly why each of the remaining "
            "options is incorrect.\n"
            "4. **Apply Formatting Syntax:** Use inline LaTeX ($...$) for mathematical or "
            "scientific expressions, HTML <u> or <mark> tags to highlight key terms (do not "
            "use backtick inline code for text or terms), and Markdown blockquotes (>) for "
            "callouts or key takeaways.\n"
            "5. **Enforce Strict Constraints:** Exclude all intros, preambles, greetings, "
            "and section headings. Begin the explanation directly on the first line.\n"
            "6. **Provide a Mnemonic or Trick:** Include a catchy mnemonic, shortcut, or "
            "memory trick formatted in a Markdown blockquote (> ...) if applicable to help "
            "retain the concept easily.\n"
            "7. **Handle Errors and Missing Answers:** State any factual ambiguity, question "
            "errors, or missing correct options explicitly, and provide the actual correct "
            "answer if needed.\n\n" + answer_instruction +
            "Question:\n" + "\n".join(question["stem"]) + "\n\n" + options + "\n\n" + known)


def body_json():
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def get_question(name, number, questions, lock):
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


def create_blueprint(questions, lock):
    bp = Blueprint("ai", __name__)

    @bp.post("/api/ai/explain")
    def ai_explain():
        body = body_json()
        name = body.get("file")
        number, question = get_question(name, body.get("n"), questions, lock)
        key = (request.headers.get("X-Gemini-Key") or os.environ.get("GEMINI_API_KEY", "")).strip()
        model = (request.headers.get("X-Gemini-Model") or settings.GEM_MODEL).strip()
        text = gemini(ai_prompt(question), key, model)
        letter = ""
        match = re.match(r"\A[ \t]*ANSWER:[ \t]*\(?([A-D]|UNKNOWN)\)?[ \t]*(?:\r?\n|$)", text, re.I)
        if match:
            answer, text = match.group(1).upper(), text[match.end():]
            if answer in "ABCD" and len(answer) == 1 and "ABCD".index(answer) < len(question["opts"]):
                letter = answer
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


    @bp.post("/api/ai/keep")
    def ai_keep():
        body = body_json()
        choice = body.get("choice")
        if choice not in ("keep1", "keep2", "both"):
            return jsonify(error="bad choice"), 400
        number, question = get_question(body.get("file"), body.get("n"), questions, lock)
        new = clean_exp(body.get("text"))
        if choice == "keep1":
            return jsonify(ok=True, explanation=question["exp"])
        if not new:
            return jsonify(error="empty explanation"), 400
        explanation = new if choice == "keep2" or not question["exp"].strip() else question["exp"].strip() + "\n\n---\n\n" + new
        questions.update(body["file"], number, exp=explanation)
        return jsonify(ok=True, explanation=explanation)


    @bp.post("/api/ai/explanation")
    def ai_save_explanation():
        """Store a manually edited explanation for a question."""
        body = body_json()
        text = clean_exp(body.get("text"))
        if not text:
            return jsonify(error="empty explanation"), 400
        if len(text) > 20000:
            return jsonify(error="explanation too long (max 20000 chars)"), 400
        number, _ = get_question(body.get("file"), body.get("n"), questions, lock)
        updated = questions.update(body["file"], number, exp=text)
        return jsonify(ok=True, explanation=updated["exp"], ans=ans_index(updated))


    @bp.post("/api/ai/format-card")
    def ai_format_card():
        data = body_json()
        body = (data.get("body") or "").strip()
        instructions = (data.get("instructions") or "").strip()
        if not body:
            return jsonify(error="empty card"), 400
        if len(body) > 8000:
            return jsonify(error="card too long (max 8000 chars)"), 400
        key = (request.headers.get("X-Gemini-Key") or os.environ.get("GEMINI_API_KEY", "")).strip()
        model = (request.headers.get("X-Gemini-Model") or settings.GEM_MODEL).strip()
        prompt = (
            "You format study flashcards for a Bangladeshi medical/engineering admission test. "
            "Rewrite the card below as clean, scannable Markdown.\n\n"
            "RULES\n"
            "1. Keep EVERY fact, number, name and example. Never add, guess or drop information. "
            "Fix only spelling, grammar and broken sentences.\n"
            "2. Language: Bangla. Do not use Banglish. If the text is English, translate it to Bangla. "
            "After each scientific/technical term, add the English in parentheses once, "
            "e.g. মাইটোকন্ড্রিয়া (Mitochondria). Numbers, units, symbols and $...$ LaTeX stay unchanged.\n"
            "3. Structure: keep existing headings. A short title line in bold if none exists. "
            "Use '- ' bullets for lists (one idea per bullet, short). Use '> ' blockquote only for "
            "mnemonics/tricks/key rules. Keep existing tables exactly as tables (same rows and columns); "
            "make a table only when the data is clearly rows x columns.\n"
            "4. Emphasis: **bold** the key term at the start of a bullet. Use <mark>...</mark> sparingly "
            "(at most one or two per bullet, only the single most testable word or value). "
            "Never use backticks; never highlight whole sentences. Keep <u> tags that already exist.\n"
            "5. Remove all references: book/author names, page numbers, source codes and bracketed "
            "citations such as [আজমল স্যার-২০২৬, পৃ-৩৪২], [উন্মেষ মেডিট্রিক্স, পৃ-২১৭] or Ha-315;.\n"
            "6. Output ONLY the finished card. No intro, no explanation, no code fences. "
            "Keep it about the same length as the input.\n\n"
            + (f"EXTRA USER INSTRUCTIONS (follow them, but never break rules 1 and 6):\n{instructions}\n\n"
               if instructions else "")
            + "Flashcard Text:\n" + body)
        text = gemini(prompt, key, model, max_tokens=8192, temperature=0.2, strict=True)
        text = re.sub(r"^```(?:markdown|md)?[ \t]*\n|\n```[ \t]*$", "", text.strip())
        output = clean_body(strip_refs(text))
        if not output:
            raise AIError("Gemini returned an empty card.", 502)
        return jsonify(body=output)


    @bp.get("/api/ai/usage")
    def ai_usage():
        # Process-local counters: vanished on restart / RAM clear.
        return jsonify(**usage_snapshot(),
                       model=settings.GEM_MODEL,
                       server_key=bool(os.environ.get("GEMINI_API_KEY")))

    @bp.post("/api/ai/usage/reset")
    def ai_usage_reset():
        reset_usage()
        return jsonify(ok=True, **usage_snapshot())


    return bp
