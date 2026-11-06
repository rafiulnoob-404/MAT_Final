import atexit, base64, json, os, re, threading, time, urllib.error, urllib.request
from collections import OrderedDict
from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

try:
    import psutil
except ImportError:  # optional (health endpoint only)
    psutil = None

app = Flask(__name__)
BASE = os.path.dirname(os.path.abspath(__file__))
QDIR = os.environ.get("QUESTIONS_DIR", os.path.join(BASE, "questions"))
FDIR = os.environ.get("FLASHCARD_DIR", os.path.join(BASE, "flashcard"))
FC_FILE = "card.md"

# ---- GitHub storage (used only if GITHUB_TOKEN + DATA_REPO are set) ----
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GH_REPO = os.environ.get("DATA_REPO", "").strip().strip("/")
GH_BRANCH = os.environ.get("DATA_BRANCH", "data")
GH_PATH = os.environ.get("DATA_PATH", "questions").strip("/")
GH_FC = os.environ.get("DATA_FC_PATH", "flashcard").strip("/")
try:
    SYNC_SECS = max(10, int(os.environ.get("SYNC_SECS", "60")))  # batch commits every 1 min
except ValueError:
    SYNC_SECS = 60
GH = bool(GH_TOKEN and GH_REPO)
if bool(GH_TOKEN) != bool(GH_REPO):
    print("[config] set BOTH GITHUB_TOKEN and DATA_REPO to enable GitHub storage", flush=True)

# ---- Gemini ----
GEM_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-lite-latest")
GEM_BASE = "https://generativelanguage.googleapis.com/v1beta/models/"
MODEL_OK = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")

LOC = {"q": QDIR, "f": FDIR}
GHP = {"q": GH_PATH, "f": GH_FC}
cache = {}        # (kind, name) -> [text, sha]
dirty = set()     # keys changed but not yet committed to GitHub
names_cache = []
legacy_names = []  # .md files ignored because they don't follow the naming convention
lock = threading.RLock()


def gh_req(method, path, body=None, query=""):
    url = f"https://api.github.com/repos/{GH_REPO}/contents/{path}{query}"
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {GH_TOKEN}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "practice-hub",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def gh_fetch(kind, name):
    try:
        j = gh_req("GET", f"{GHP[kind]}/{name}", query=f"?ref={GH_BRANCH}")
        v = [base64.b64decode(j["content"]).decode("utf-8"), j["sha"]]
    except urllib.error.HTTPError as e:
        if e.code != 404 or kind != "f":
            raise
        v = ["", None]  # flashcard file doesn't exist yet
    cache[(kind, name)] = v
    return v


def gh_commit(key):
    kind, name = key
    path = f"{GHP[kind]}/{name}"
    for attempt in range(2):
        text, sha = cache[key]
        body = {"message": f"update {name}",
                "content": base64.b64encode(text.encode("utf-8")).decode(),
                "branch": GH_BRANCH}
        if sha:
            body["sha"] = sha
        try:
            res = gh_req("PUT", path, body)
            cache[key][1] = res["content"]["sha"]
            return
        except urllib.error.HTTPError as e:
            if e.code in (409, 422) and attempt == 0:
                try:
                    cache[key][1] = gh_req("GET", path, query=f"?ref={GH_BRANCH}")["sha"]
                except urllib.error.HTTPError as e2:
                    if e2.code != 404:
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
            except Exception as e:
                print(f"[sync] {key} failed: {e}", flush=True)
        return len(dirty)


def sync_loop():
    while True:
        time.sleep(SYNC_SECS)
        flush()


if GH:
    threading.Thread(target=sync_loop, daemon=True).start()
    atexit.register(flush)


def load(kind, name):
    if GH:
        if (kind, name) not in cache:
            gh_fetch(kind, name)
        return cache[(kind, name)][0]
    try:
        with open(os.path.join(LOC[kind], name), encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def save(kind, name, text):
    if GH:
        if (kind, name) not in cache:
            gh_fetch(kind, name)
        cache[(kind, name)][0] = text
        dirty.add((kind, name))   # committed later by sync_loop
        return
    os.makedirs(LOC[kind], exist_ok=True)
    p = os.path.join(LOC[kind], name)
    with open(p + ".tmp", "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(p + ".tmp", p)


# ---------------- file naming: [Coaching]_[Mode]_[Phase]_[Subject]_[Number].md ----------------
# U|R _ ON|OFF _ P|S _ B|C|P|GK|E _ n.md      e.g. U_OFF_P_B_3.md
# U|R _ ON|OFF _ MT _ n.md  (model test, no subject)   e.g. U_ON_MT_1.md
FN = re.compile(r"(U|R)_(ON|OFF)_(?:(P|S)_(B|C|P|GK|E)|(MT))_(\d+)\.md", re.I)
PH_ORDER = {"P": 0, "S": 1, "MT": 2}
ST_CODE = {"correct": "c", "incorrect": "i", "skipped": "s", "": ""}


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


# ---------------- questions ----------------
HEAD = re.compile(r"^[ \t]*###\s*Q\s*(\d+)[^\n]*\n", re.I | re.M)
SEC = re.compile(r"^[ \t]*##\s*(answer|explanation|status)[ \t]*:?[ \t]*([^\n]*)\n?", re.I | re.M)
KEYMAP = {"answer": "ans", "explanation": "exp", "status": "st"}


def parse(t):
    ms = list(HEAD.finditer(t))
    head = t[: ms[0].start()] if ms else t
    qs = []
    for i, m in enumerate(ms):
        blk = t[m.end(): ms[i + 1].start() if i + 1 < len(ms) else len(t)]
        ps = SEC.split(blk)
        q = {"n": int(m.group(1)), "stem": [], "opts": [], "ans": "", "exp": "", "st": ""}
        for l in ps[0].split("\n"):
            o = re.match(r"^\s*[*\-]\s+(.*)", l)
            if o:
                q["opts"].append(o.group(1).strip())
            elif l.strip():
                q["stem"].append(l.strip())
        for k in range(1, len(ps) - 2, 3):
            q[KEYMAP[ps[k].lower()]] = ((ps[k + 1] or "") + "\n" + (ps[k + 2] or "")).strip()
        v = q["st"].lower()
        q["st"] = ("incorrect" if re.search("incorrect|wrong", v) else
                   "correct" if "correct" in v else "skipped" if "skip" in v else "")
        qs.append(q)
    return head, qs


def ser(head, qs):
    out = []
    for q in qs:
        stem = "\n".join(q["stem"]) + "\n\n" if q["stem"] else ""
        opts = "\n".join("* " + o for o in q["opts"])
        out.append(f"### Q {q['n']}\n\n{stem}{opts}\n\n## answer\n{q['ans']}\n\n## explanation\n{q['exp']}\n\n## status\n{q['st']}\n")
    return head + "\n".join(out)


def ans_index(q):
    a = re.sub(r"[*`]", "", q["ans"]).strip()
    m = re.match(r"^\(?([A-Da-d1-4ক-ঘ১-৪])\)?(?:[\s.):]|$)", a)
    if m:
        return "abcd1234কখগঘ১২৩৪".index(m.group(1).lower()) % 4
    for i, o in enumerate(q["opts"]):
        if o.lower() == a.lower():
            return i
    return -1


def list_names():
    global names_cache
    if GH:
        if not names_cache:
            items = gh_req("GET", GH_PATH, query=f"?ref={GH_BRANCH}")
            md = [i["name"] for i in items if i["type"] == "file" and i["name"].lower().endswith(".md")]
            names_cache = [n for n in md if parse_name(n)]
            legacy_names[:] = [n for n in md if not parse_name(n)]
        return names_cache
    os.makedirs(QDIR, exist_ok=True)
    md = [n for n in os.listdir(QDIR) if n.lower().endswith(".md")]
    legacy_names[:] = [n for n in md if not parse_name(n)]
    return sorted((n for n in md if parse_name(n)), key=name_key)


# parsed-file LRU (bounded memory) + compact per-file status summary
pcache = OrderedDict()   # name -> (sig, head, qs)
sums = {}                # name -> (sig, [[n, code], ...])
PCACHE_MAX = 24


def sig_of(text):
    return (len(text), hash(text))


def read(name):
    if not parse_name(name):
        raise ValueError("bad file name")
    text = load("q", name)
    sig = sig_of(text)
    c = pcache.get(name)
    if c and c[0] == sig:
        pcache.move_to_end(name)
        return c[1], c[2]
    head, qs = parse(text)
    pcache[name] = (sig, head, qs)
    while len(pcache) > PCACHE_MAX:
        pcache.popitem(last=False)
    return head, qs


def summary(name):
    text = load("q", name)
    sig = sig_of(text)
    c = sums.get(name)
    if c and c[0] == sig:
        return c[1]
    _, qs = read(name)
    v = [[q["n"], ST_CODE[q["st"]]] for q in qs]
    sums[name] = (sig, v)
    return v


def write(name, head, qs):
    if not parse_name(name):
        raise ValueError("bad file name")
    save("q", name, ser(head, qs))
    pcache.pop(name, None)


def update(name, n, **fields):
    with lock:
        head, qs = read(name)
        q = next((x for x in qs if x["n"] == n), None)
        if not q:
            raise ValueError("question not found")
        q.update(fields)
        write(name, head, qs)
        return dict(q)


def qdict(name, q):
    return {"file": name, "n": q["n"], "stem": "\n".join(q["stem"]), "opts": q["opts"],
            "ans": ans_index(q), "exp": q["exp"], "st": q["st"]}


# ---------------- flashcards (flashcard/card.md) ----------------
CARD_HEAD = re.compile(r"^### Card (\d+)[ \t]*\n(?:<!-- color: (#[0-9a-fA-F]{6}) -->[ \t]*\n)?", re.M)
COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def parse_cards(t):
    ms = list(CARD_HEAD.finditer(t))
    out = []
    for i, m in enumerate(ms):
        body = t[m.end(): ms[i + 1].start() if i + 1 < len(ms) else len(t)].strip()
        out.append({"id": int(m.group(1)), "color": m.group(2) or "#fff3a3", "body": body})
    return out


def ser_cards(cs):
    return "# Flashcards\n\n" + "\n".join(
        f"### Card {c['id']}\n<!-- color: {c['color']} -->\n\n{c['body']}\n" for c in cs)


def clean_body(b):
    b = (b or "").strip()
    return re.sub(r"^(### Card )", r"\\\1", b, flags=re.M)  # keep body from faking a card header


def cards_read():
    return parse_cards(load("f", FC_FILE))


def cards_write(cs):
    save("f", FC_FILE, ser_cards(cs))


def body_json():
    b = request.get_json(silent=True)
    return b if isinstance(b, dict) else {}


@app.get("/api/cards")
def cards_list():
    with lock:
        return jsonify(cards_read())


@app.post("/api/cards")
def cards_add():
    b = body_json()
    body = clean_body(b.get("body"))
    if not body:
        return jsonify(error="empty card"), 400
    color = b.get("color") if COLOR.match(b.get("color") or "") else "#fff3a3"
    with lock:
        cs = cards_read()
        c = {"id": max([x["id"] for x in cs], default=0) + 1, "color": color, "body": body}
        cs.append(c)
        cards_write(cs)
    return jsonify(c)


@app.put("/api/cards/<int:cid>")
def cards_edit(cid):
    b = body_json()
    body = clean_body(b.get("body"))
    if not body:
        return jsonify(error="empty card"), 400
    with lock:
        cs = cards_read()
        c = next((x for x in cs if x["id"] == cid), None)
        if not c:
            return jsonify(error="not found"), 404
        c["body"] = body
        if COLOR.match(b.get("color") or ""):
            c["color"] = b["color"]
        cards_write(cs)
    return jsonify(c)


@app.delete("/api/cards/<int:cid>")
def cards_del(cid):
    with lock:
        cs = [x for x in cards_read() if x["id"] != cid]
        cards_write(cs)
    return jsonify(ok=True)


# ---------------- Gemini ----------------
class AIError(Exception):
    def __init__(self, msg, code=502):
        super().__init__(msg)
        self.code = code


def gem_err_text(e):
    try:
        j = json.loads(e.read().decode("utf-8", "replace"))
        msg = (j.get("error") or {}).get("message") or ""
    except Exception:
        msg = ""
    if e.code in (400, 401, 403) and re.search(r"api key|API_KEY|permission|unauthor", msg, re.I):
        return "Gemini rejected the API key (invalid, expired or blocked)."
    if e.code == 404:
        return "Gemini model not found. Change the model name in Settings."
    if e.code == 429:
        return "Gemini quota/rate limit reached. Try again shortly."
    return f"Gemini error {e.code}: {msg[:200] or e.reason}"


def gemini(prompt, key, model):
    if not key or not key.isascii():
        raise AIError("Gemini API key missing. Set GEMINI_API_KEY on the server or add it in Settings.", 400)
    if not MODEL_OK.match(model or ""):
        raise AIError("Invalid Gemini model name.", 400)
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                       "generationConfig": {"temperature": 0.3, "maxOutputTokens": 2048}}).encode()
    msg, j = "Gemini request failed.", None
    for attempt in range(3):
        req = urllib.request.Request(
            GEM_BASE + model + ":generateContent", data=body, method="POST",
            headers={"Content-Type": "application/json", "x-goog-api-key": key})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                j = json.load(r)
            break
        except urllib.error.HTTPError as e:
            msg = gem_err_text(e)
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise AIError(msg, 429 if e.code == 429 else 400 if e.code in (400, 401, 403, 404) else 502)
        except (urllib.error.URLError, OSError, ValueError) as e:  # network, timeout, bad JSON
            msg = f"Could not reach Gemini: {getattr(e, 'reason', e)}"
            if attempt < 2:
                time.sleep(1.5)
                continue
            raise AIError(msg, 502)
    if not isinstance(j, dict):
        raise AIError(msg, 502)
    cands = j.get("candidates") or []
    if not cands:
        br = (j.get("promptFeedback") or {}).get("blockReason")
        raise AIError(f"Gemini blocked the request ({br})." if br else "Gemini returned no answer.", 502)
    c = cands[0]
    parts = (c.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    if not text:
        raise AIError(f"Empty Gemini response ({c.get('finishReason', 'unknown')}).", 502)
    return text


def clean_exp(t):
    """Stop AI/user text from forging question or section headers in the md file."""
    t = (t or "").strip()
    return re.sub(r"^([ \t]*#{2,3})(?=\s*(?:Q\s*\d|answer\b|explanation\b|status\b))", r"\1#", t,
                  flags=re.M | re.I)


def ai_prompt(q):
    opts = "\n".join(f"{'ABCD'[i]}. {o}" for i, o in enumerate(q["opts"][:4]))
    i = ans_index(q)
    known = f"Correct answer: {'ABCD'[i]}" if 0 <= i < 4 else "Correct answer: unknown"
    ask = "" if 0 <= i < 4 else "The first line must be exactly `ANSWER: <A|B|C|D>`, then a blank line.\n"
    return ("You are an admission-test tutor. Explain this MCQ in the same language as the question "
            "(Bangla or English). Output a short paragraph or a compact markdown table. HTML <u>/<mark> "
            "and LaTeX ($...$) are allowed. No preamble, no headings.\n" + ask +
            "\nQuestion:\n" + "\n".join(q["stem"]) + "\n\n" + opts + "\n\n" + known)


def get_q(name, n):
    if not parse_name(name):
        raise AIError("bad file name", 400)
    try:
        n = int(n)
    except (TypeError, ValueError):
        raise AIError("bad question number", 400)
    with lock:
        _, qs = read(name)
        q = next((x for x in qs if x["n"] == n), None)
        if not q:
            raise AIError("question not found", 404)
        return n, {**q, "opts": list(q["opts"]), "stem": list(q["stem"])}


@app.post("/api/ai/explain")
def ai_explain():
    b = body_json()
    name = b.get("file")
    n, q = get_q(name, b.get("n"))
    key = (request.headers.get("X-Gemini-Key") or os.environ.get("GEMINI_API_KEY", "")).strip()
    model = (request.headers.get("X-Gemini-Model") or GEM_MODEL).strip()
    text = gemini(ai_prompt(q), key, model)      # network call outside the lock
    letter = ""
    m = re.match(r"\s*ANSWER:\s*\(?([A-D])\)?[^\n]*\n?", text, re.I)
    if m:
        letter, text = m.group(1).upper(), text[m.end():]
    exp = clean_exp(text)
    if not exp:
        raise AIError("Gemini returned an empty explanation.", 502)
    if not q["exp"].strip():
        # no explanation yet: legacy behaviour - generate and save straight into the md
        fields = {"exp": exp}
        if letter and ans_index(q) < 0:
            fields["ans"] = letter
        nq = update(name, n, **fields)
        return jsonify(saved=True, explanation=exp, ans=ans_index(nq))
    return jsonify(saved=False, explanation=exp, existing=q["exp"])


@app.post("/api/ai/keep")
def ai_keep():
    b = body_json()
    choice = b.get("choice")
    if choice not in ("keep1", "keep2", "both"):
        return jsonify(error="bad choice"), 400
    n, q = get_q(b.get("file"), b.get("n"))
    new = clean_exp(b.get("text"))
    if choice == "keep1":
        return jsonify(ok=True, explanation=q["exp"])
    if not new:
        return jsonify(error="empty explanation"), 400
    exp = new if choice == "keep2" or not q["exp"].strip() else q["exp"].strip() + "\n\n---\n\n" + new
    update(b["file"], n, exp=exp)
    return jsonify(ok=True, explanation=exp)


# ---------------- routes ----------------
@app.errorhandler(AIError)
def on_ai_error(e):
    return jsonify(error=str(e)), e.code


@app.errorhandler(Exception)
def on_error(e):
    if isinstance(e, HTTPException):
        return e
    print(f"[error] {request.path}: {e!r}", flush=True)
    return jsonify(error=str(e)), 500


@app.get("/healthz")
def healthz():
    return "ok", 200   # Render health check (no GitHub calls)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/index")
def api_index():
    """Light index only: file meta + [n, status] per question. No question text."""
    out = []
    with lock:
        for n in list_names():
            m = parse_name(n)
            if m:
                out.append({"f": n, **m, "q": summary(n)})
    return jsonify(out)


@app.post("/api/questions")
def api_questions():
    """Full question objects for a small batch: {"items": [[file, n], ...]}."""
    items = body_json().get("items")
    if not isinstance(items, list):
        return jsonify(error="items required"), 400
    by = {}
    try:
        for f, n in items[:60]:
            by.setdefault(f, set()).add(int(n))
    except (TypeError, ValueError):
        return jsonify(error="bad items"), 400
    out = []
    with lock:
        for f, ns in by.items():
            if not parse_name(f):
                continue
            _, qs = read(f)
            out.extend(qdict(f, q) for q in qs if q["n"] in ns)
    return jsonify(out)


@app.get("/api/file/<name>")
def api_file(name):
    if not parse_name(name):
        return jsonify(error="bad file name"), 400
    off = max(0, request.args.get("offset", 0, type=int))
    lim = min(100, max(1, request.args.get("limit", 25, type=int)))
    with lock:
        _, qs = read(name)
        return jsonify(total=len(qs), items=[qdict(name, q) for q in qs[off:off + lim]])


@app.post("/api/reload")
def reload_cache():
    # re-pull from GitHub; pending (uncommitted) files are pushed first and never dropped
    flush()
    with lock:
        for k in list(cache):
            if k not in dirty:
                del cache[k]
        names_cache.clear()
        pcache.clear()
        sums.clear()
    return jsonify(ok=True)


@app.post("/api/sync")
def sync_now():
    return jsonify(ok=True, pending=flush() if GH else 0)


@app.post("/api/status")
def status():
    b = body_json()
    if b.get("status") not in ("correct", "incorrect", "skipped"):
        return jsonify(error="bad status"), 400
    try:
        update(b.get("file"), int(b.get("n")), st=b["status"])
    except (ValueError, TypeError) as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True)


@app.get("/api/health")
def health():
    return jsonify(github=GH, repo=GH_REPO, branch=GH_BRANCH, path=GH_PATH,
                   token_set=bool(GH_TOKEN), pending=len(dirty), sync_secs=SYNC_SECS,
                   gemini_env=bool(os.environ.get("GEMINI_API_KEY")), gemini_model=GEM_MODEL,
                   ignored_files=len(legacy_names), ignored_sample=legacy_names[:5])


@app.get("/system/health")
def health_system():
    if psutil is None:
        return jsonify(status="unavailable", error="psutil not installed"), 501
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    cpu_usage = psutil.cpu_percent(interval=None)
    ram_usage = ram.percent
    disk_free = round(disk.free / (1024 ** 2), 2)
    is_healthy = ram_usage < 90 and disk.percent < 90
    return jsonify(
        status="healthy" if is_healthy else "warning",
        cpu_usage_pct=cpu_usage,
        ram_used_pct=ram_usage,
        disk_free_mb=disk_free
    ), 200 if is_healthy else 500


if __name__ == "__main__":
    if not GH:
        os.makedirs(QDIR, exist_ok=True)
        os.makedirs(FDIR, exist_ok=True)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0" if os.environ.get("PORT") else "127.0.0.1", port=port, debug=False)
