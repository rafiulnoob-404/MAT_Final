"""Question filename validation, Markdown parsing, caching and API routes."""

import re
from collections import OrderedDict

from flask import Blueprint, jsonify, request

# [Coaching]_[Mode]_[Phase]_[Subject]_[Number].md
FN = re.compile(r"(U|R)_(ON|OFF)_(?:(P|S)_(B|C|P|GK|E)|(MT))_(\d+)\.md", re.I)
PH_ORDER = {"P": 0, "S": 1, "MT": 2}
ST_CODE = {"correct": "c", "incorrect": "i", "skipped": "s", "": ""}
HEAD = re.compile(r"^[ \t]*###\s*Q\s*(\d+)[^\n]*\n", re.I | re.M)
SEC = re.compile(r"^[ \t]*##\s*(answer|explanation|status)[ \t]*:?[ \t]*([^\n]*)\n?", re.I | re.M)
KEYMAP = {"answer": "ans", "explanation": "exp", "status": "st"}


def parse_name(name):
    m = FN.fullmatch(name or "")
    if not m:
        return None
    co, mo, ph, sb, mt, no = m.groups()
    return {"co": co.upper(), "mo": mo.upper(), "ph": (mt or ph).upper(),
            "sb": (sb or "").upper(), "no": int(no)}


def name_key(name):
    m = parse_name(name)
    return (m["co"], m["mo"], PH_ORDER[m["ph"]], m["sb"], m["no"])


def parse(text):
    matches = list(HEAD.finditer(text))
    head = text[:matches[0].start()] if matches else text
    questions = []
    for i, match in enumerate(matches):
        block = text[match.end(): matches[i + 1].start() if i + 1 < len(matches) else len(text)]
        parts = SEC.split(block)
        question = {"n": int(match.group(1)), "stem": [], "opts": [], "ans": "", "exp": "", "st": ""}
        for line in parts[0].split("\n"):
            option = re.match(r"^\s*[*\-]\s+(.*)", line)
            if option:
                question["opts"].append(option.group(1).strip())
            elif line.strip():
                question["stem"].append(line.strip())
        for pos in range(1, len(parts) - 2, 3):
            question[KEYMAP[parts[pos].lower()]] = ((parts[pos + 1] or "") + "\n" + (parts[pos + 2] or "")).strip()
        status = question["st"].lower()
        question["st"] = ("incorrect" if re.search("incorrect|wrong", status) else
                          "correct" if "correct" in status else
                          "skipped" if "skip" in status else "")
        questions.append(question)
    return head, questions


def serialize(head, questions):
    out = []
    for question in questions:
        stem = "\n".join(question["stem"]) + "\n\n" if question["stem"] else ""
        options = "\n".join("* " + option for option in question["opts"])
        out.append(f"### Q {question['n']}\n\n{stem}{options}\n\n## answer\n{question['ans']}\n\n"
                   f"## explanation\n{question['exp']}\n\n## status\n{question['st']}\n")
    return head + "\n".join(out)


def ans_index(question):
    answer = re.sub(r"[*`]", "", question["ans"]).strip()
    match = re.match(r"^\(?([A-Da-d1-4ক-ঘ১-৪])\)?(?:[\s.):]|$)", answer)
    if match:
        return "abcd1234কখগঘ১২৩৪".index(match.group(1).lower()) % 4
    for i, option in enumerate(question["opts"]):
        if option.lower() == answer.lower():
            return i
    return -1


class QuestionService:
    """Question business logic independent of Flask and the storage backend."""

    def __init__(self, load, save, list_names, lock, cache_max=24, cache_names=False):
        self.load_text = load
        self.save_text = save
        self.list_storage_names = list_names
        self.lock = lock
        self.cache_max = cache_max
        self.cache_names = cache_names
        self.pcache = OrderedDict()
        self.summaries = {}
        self.names_cache = []
        self.legacy_names = []

    @staticmethod
    def signature(text):
        return len(text), hash(text)

    def list_names(self):
        if self.cache_names and self.names_cache:
            return self.names_cache
        names = self.list_storage_names()
        markdown = [name for name in names if name.lower().endswith(".md")]
        valid = [name for name in markdown if parse_name(name)]
        self.names_cache = valid if self.cache_names else sorted(valid, key=name_key)
        self.legacy_names[:] = [name for name in markdown if not parse_name(name)]
        return self.names_cache

    def read(self, name):
        if not parse_name(name):
            raise ValueError("bad file name")
        text = self.load_text(name)
        signature = self.signature(text)
        cached = self.pcache.get(name)
        if cached and cached[0] == signature:
            self.pcache.move_to_end(name)
            return cached[1], cached[2]
        head, questions = parse(text)
        self.pcache[name] = (signature, head, questions)
        while len(self.pcache) > self.cache_max:
            self.pcache.popitem(last=False)
        return head, questions

    def summary(self, name):
        text = self.load_text(name)
        signature = self.signature(text)
        cached = self.summaries.get(name)
        if cached and cached[0] == signature:
            return cached[1]
        _, questions = self.read(name)
        value = [[question["n"], ST_CODE[question["st"]]] for question in questions]
        self.summaries[name] = (signature, value)
        return value

    def write(self, name, head, questions):
        if not parse_name(name):
            raise ValueError("bad file name")
        self.save_text(name, serialize(head, questions))
        self.pcache.pop(name, None)

    def update(self, name, number, **fields):
        with self.lock:
            head, questions = self.read(name)
            question = next((item for item in questions if item["n"] == number), None)
            if not question:
                raise ValueError("question not found")
            question.update(fields)
            self.write(name, head, questions)
            return dict(question)

    @staticmethod
    def as_dict(name, question):
        return {"file": name, "n": question["n"], "stem": "\n".join(question["stem"]),
                "opts": question["opts"], "ans": ans_index(question),
                "exp": question["exp"], "st": question["st"]}

    def clear_caches(self):
        self.names_cache.clear()
        self.pcache.clear()
        self.summaries.clear()


def create_blueprint(service):
    bp = Blueprint("questions", __name__)

    @bp.get("/api/index")
    def api_index():
        out = []
        with service.lock:
            for name in service.list_names():
                meta = parse_name(name)
                if meta:
                    out.append({"f": name, **meta, "q": service.summary(name)})
        return jsonify(out)

    @bp.post("/api/questions")
    def api_questions():
        body = request.get_json(silent=True)
        items = body.get("items") if isinstance(body, dict) else None
        if not isinstance(items, list):
            return jsonify(error="items required"), 400
        grouped = {}
        try:
            for filename, number in items[:60]:
                grouped.setdefault(filename, set()).add(int(number))
        except (TypeError, ValueError):
            return jsonify(error="bad items"), 400
        out = []
        with service.lock:
            for filename, numbers in grouped.items():
                if not parse_name(filename):
                    continue
                _, questions = service.read(filename)
                out.extend(service.as_dict(filename, q) for q in questions if q["n"] in numbers)
        return jsonify(out)

    @bp.get("/api/file/<name>")
    def api_file(name):
        if not parse_name(name):
            return jsonify(error="bad file name"), 400
        offset = max(0, request.args.get("offset", 0, type=int))
        limit = min(100, max(1, request.args.get("limit", 25, type=int)))
        with service.lock:
            _, questions = service.read(name)
            return jsonify(total=len(questions),
                           items=[service.as_dict(name, q) for q in questions[offset:offset + limit]])

    @bp.post("/api/status")
    def status():
        body = request.get_json(silent=True)
        body = body if isinstance(body, dict) else {}
        if body.get("status") not in ("correct", "incorrect", "skipped"):
            return jsonify(error="bad status"), 400
        try:
            service.update(body.get("file"), int(body.get("n")), st=body["status"])
        except (ValueError, TypeError) as error:
            return jsonify(error=str(error)), 400
        return jsonify(ok=True)

    return bp
