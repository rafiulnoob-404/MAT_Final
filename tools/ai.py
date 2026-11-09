"""Gemini helpers and AI API routes."""
import json
import os
import re
import time
import urllib.error
import urllib.request

from flask import Blueprint, jsonify, request
from tools import settings
from tools.cards import clean_body
from tools.questions import ans_index, parse_name

MODEL_OK = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")

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
            settings.GEM_BASE + model + ":generateContent", data=body, method="POST",
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
            "bold key terms (**text**), inline code highlights (`concept`), and subtle icons "
            "(e.g., ✅, ❌, 💡) to make the text scannable and visually engaging.\n"
            "2. **Explain the Correct Answer:** State the correct option clearly and explain "
            "the underlying reasoning or formula behind it briefly.\n"
            "3. **Eliminate Incorrect Options:** Explain briefly why each of the remaining "
            "options is incorrect.\n"
            "4. **Apply Formatting Syntax:** Use inline LaTeX ($...$) for mathematical or "
            "scientific expressions, and HTML <u> or <mark> tags to emphasize key terms.\n"
            "5. **Enforce Strict Constraints:** Exclude all intros, preambles, greetings, "
            "and section headings. Begin the explanation directly on the first line.\n"
            "6. **Provide a Mnemonic or Trick:** Include a catchy mnemonic, shortcut, or "
            "memory trick if applicable to help retain the concept easily.\n"
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


    @bp.post("/api/ai/format-card")
    def ai_format_card():
        body = (body_json().get("body") or "").strip()
        if not body:
            return jsonify(error="empty card"), 400
        if len(body) > 8000:
            return jsonify(error="card too long (max 8000 chars)"), 400
        key = (request.headers.get("X-Gemini-Key") or os.environ.get("GEMINI_API_KEY", "")).strip()
        model = (request.headers.get("X-Gemini-Model") or settings.GEM_MODEL).strip()
        prompt = ("Reformat and refine the provided flashcard text into clean, highly structured, and scannable Markdown.\n\n"
                  "Strict Execution Rules:\n"
                  "1. **Language & Accuracy:** Do NOT allow any Banglish (Romanized Bangla). "
                  "Standardize the language into pure English or proper Bangla. Correct any grammatical errors, "
                  "spelling mistakes, or incomplete sentences while retaining the original facts; "
                  "do not introduce unsupported facts.\n"
                  "2. **Structural Formatting:** Keep any existing headings/headlines intact. "
                  "Format main concepts into concise paragraphs or clean Markdown bullet points. "
                  "Use a compact table only if the data is strictly tabular.\n"
                  "3. **Rich Text styling:** Bold key terms or style them using italics or inline code "
                  "(`key term`) for clarity. In bullet points, bold primary keywords, examples, and key values. "
                  "Preserve all existing inline LaTeX ($...$), <u>, and <mark> tags as they are.\n"
                  "4. **Complete Reference Removal:** Strip out all references, book names, author names, "
                  "page numbers, and custom source codes completely, including source tags, citations, "
                  "footnotes, and brackets containing page, book, or topic references regardless of format "
                  "(e.g., [Author 2026, p. 342], [Book, p. 217], Ha-315;, Alim-444;).\n"
                  "5. **Strict Output Constraints:** No preamble, conversational filler, or intros. "
                  "Do not use code fences around the final output. Output only the finalized flashcard content.\n\n"
                  "Flashcard Text:\n" + body)
        text = gemini(prompt, key, model)
        text = re.sub(r"^```(?:markdown|md)?[ \t]*\n|\n```[ \t]*$", "", text.strip())
        output = clean_body(strip_refs(text))
        if not output:
            raise AIError("Gemini returned an empty card.", 502)
        return jsonify(body=output)


    return bp
