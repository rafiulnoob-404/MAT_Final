"""Flashcard Markdown parsing, subject-wise storage logic and API routes."""

import re

from flask import Blueprint, jsonify, request

CARD_HEAD = re.compile(r"^### Card (\d+)[ \t]*\n(?:<!-- color: (#[0-9a-fA-F]{6}) -->[ \t]*\n)?", re.M)
COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
DEFAULT_COLOR = "#fff3a3"

DEFAULT_SUBJECTS = {
    "B": "B.md",
    "C": "C.md",
    "P": "P.md",
    "GK": "GK.md",
    "E": "E.md",
    "general": "general.md",
}
DEFAULT_SUBJECT = "general"


def parse_cards(text, subject=None):
    matches = list(CARD_HEAD.finditer(text or ""))
    cards = []
    for i, match in enumerate(matches):
        body = text[match.end(): matches[i + 1].start() if i + 1 < len(matches) else len(text)].strip()
        card = {"id": int(match.group(1)), "color": match.group(2) or DEFAULT_COLOR, "body": body}
        if subject is not None:
            card["subject"] = subject
        cards.append(card)
    return cards


def serialize_cards(cards):
    return "# Flashcards\n\n" + "\n".join(
        f"### Card {card['id']}\n<!-- color: {card['color']} -->\n\n{card['body']}\n" for card in cards)


def clean_body(body):
    body = (body or "").strip()
    return re.sub(r"^(### Card )", r"\\\1", body, flags=re.M)


class CardService:
    def __init__(self, load, save, lock, subjects=None, default_subject=DEFAULT_SUBJECT,
                 legacy_filename="card.md"):
        self.load_text = load
        self.save_text = save
        self.lock = lock
        self.subjects = dict(subjects or DEFAULT_SUBJECTS)
        self.default_subject = default_subject if default_subject in self.subjects else next(iter(self.subjects))
        self.legacy_filename = legacy_filename
        self._lookup = {key.lower(): key for key in self.subjects}

    def normalize_subject(self, subject, strict=False):
        if subject is None or str(subject).strip() == "":
            return self.default_subject
        raw = str(subject).strip()
        found = self._lookup.get(raw.lower())
        if found:
            return found
        if strict:
            raise ValueError(f"invalid subject: {raw}")
        return self.default_subject

    def filenames(self):
        return list(self.subjects.values())

    @staticmethod
    def validate_body(body):
        body = clean_body(body)
        if not body:
            raise ValueError("empty card")
        return body

    def _migrate_legacy_unlocked(self):
        """If general.md (and all other subject files) are empty, import legacy card.md into general.md."""
        if not self.legacy_filename or self.legacy_filename in self.subjects.values():
            return
        if any(parse_cards(self.load_text(fname)) for fname in self.subjects.values()):
            return
        legacy_text = self.load_text(self.legacy_filename)
        legacy_cards = parse_cards(legacy_text, subject=self.default_subject)
        if legacy_cards:
            self.write_subject(self.default_subject, legacy_cards)

    def read_subject(self, subject):
        subj = self.normalize_subject(subject, strict=True)
        return parse_cards(self.load_text(self.subjects[subj]), subject=subj)

    def write_subject(self, subject, cards):
        subj = self.normalize_subject(subject, strict=True)
        self.save_text(self.subjects[subj], serialize_cards(cards))

    def _read_all_unlocked(self):
        self._migrate_legacy_unlocked()
        by_subject = {}
        for subj in self.subjects:
            by_subject[subj] = self.read_subject(subj)
        return by_subject

    def list(self, subject=None):
        with self.lock:
            by_subject = self._read_all_unlocked()
            if subject and str(subject).strip().lower() != "all":
                subj = self.normalize_subject(subject, strict=True)
                cards = list(by_subject[subj])
            else:
                cards = [card for items in by_subject.values() for card in items]
            cards.sort(key=lambda item: item["id"])
            return cards

    def add(self, body, color=None, subject=None):
        body = self.validate_body(body)
        color = color if COLOR.match(color or "") else DEFAULT_COLOR
        subj = self.normalize_subject(subject, strict=True)
        with self.lock:
            by_subject = self._read_all_unlocked()
            max_id = max((item["id"] for items in by_subject.values() for item in items), default=0)
            card = {"id": max_id + 1, "color": color, "body": body, "subject": subj}
            target_cards = by_subject[subj]
            target_cards.append(card)
            self.write_subject(subj, target_cards)
        return card

    def edit(self, card_id, body=None, color=None, subject=None, source_subject=None):
        new_body = self.validate_body(body) if body is not None else None
        new_subject = self.normalize_subject(subject, strict=True) if subject is not None else None
        src_hint = self.normalize_subject(source_subject, strict=True) if source_subject else None
        with self.lock:
            by_subject = self._read_all_unlocked()
            order = ([src_hint] + [s for s in self.subjects if s != src_hint]) if src_hint else list(self.subjects)
            found_subj = None
            found_card = None
            for subj in order:
                match = next((item for item in by_subject[subj] if item["id"] == card_id), None)
                if match is not None:
                    found_subj = subj
                    found_card = match
                    break
            if not found_card:
                return None
            if new_body is not None:
                found_card["body"] = new_body
            if COLOR.match(color or ""):
                found_card["color"] = color
            target_subj = new_subject or found_subj
            if target_subj != found_subj:
                by_subject[found_subj] = [item for item in by_subject[found_subj] if item["id"] != card_id]
                self.write_subject(found_subj, by_subject[found_subj])
                found_card["subject"] = target_subj
                # Ensure no duplicate ID in target_subj if manually edited externally
                if any(item["id"] == card_id for item in by_subject[target_subj]):
                    max_id = max((item["id"] for items in by_subject.values() for item in items), default=0)
                    found_card["id"] = max_id + 1
                by_subject[target_subj].append(found_card)
                by_subject[target_subj].sort(key=lambda item: item["id"])
                self.write_subject(target_subj, by_subject[target_subj])
            else:
                found_card["subject"] = found_subj
                self.write_subject(found_subj, by_subject[found_subj])
            return dict(found_card)

    def delete(self, card_id, subject=None):
        subj_hint = self.normalize_subject(subject, strict=True) if subject else None
        with self.lock:
            by_subject = self._read_all_unlocked()
            targets = [subj_hint] if subj_hint else list(self.subjects)
            for subj in targets:
                before = by_subject[subj]
                after = [card for card in before if card["id"] != card_id]
                if len(after) != len(before):
                    self.write_subject(subj, after)


def create_blueprint(service):
    bp = Blueprint("cards", __name__)

    @bp.get("/api/cards")
    def cards_list():
        subject = request.args.get("subject")
        try:
            return jsonify(service.list(subject=subject))
        except ValueError as error:
            return jsonify(error=str(error)), 400

    @bp.post("/api/cards")
    def cards_add():
        body = request.get_json(silent=True)
        body = body if isinstance(body, dict) else {}
        try:
            card = service.add(body.get("body"), body.get("color"), body.get("subject"))
        except ValueError as error:
            return jsonify(error=str(error)), 400
        return jsonify(card)

    @bp.put("/api/cards/<int:card_id>")
    def cards_edit(card_id):
        body = request.get_json(silent=True)
        body = body if isinstance(body, dict) else {}
        try:
            card = service.edit(
                card_id,
                body=body.get("body") if "body" in body else None,
                color=body.get("color"),
                subject=body.get("subject") if "subject" in body else None,
                source_subject=body.get("source_subject") or request.args.get("subject"),
            )
        except ValueError as error:
            return jsonify(error=str(error)), 400
        if not card:
            return jsonify(error="not found"), 404
        return jsonify(card)

    @bp.delete("/api/cards/<int:card_id>")
    def cards_delete(card_id):
        try:
            service.delete(card_id, subject=request.args.get("subject"))
        except ValueError as error:
            return jsonify(error=str(error)), 400
        return jsonify(ok=True)

    return bp
