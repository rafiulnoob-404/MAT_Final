"""Flashcard Markdown parsing, business logic and API routes."""

import re

from flask import Blueprint, jsonify, request

CARD_HEAD = re.compile(r"^### Card (\d+)[ \t]*\n(?:<!-- color: (#[0-9a-fA-F]{6}) -->[ \t]*\n)?", re.M)
COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
DEFAULT_COLOR = "#fff3a3"


def parse_cards(text):
    matches = list(CARD_HEAD.finditer(text))
    cards = []
    for i, match in enumerate(matches):
        body = text[match.end(): matches[i + 1].start() if i + 1 < len(matches) else len(text)].strip()
        cards.append({"id": int(match.group(1)), "color": match.group(2) or DEFAULT_COLOR, "body": body})
    return cards


def serialize_cards(cards):
    return "# Flashcards\n\n" + "\n".join(
        f"### Card {card['id']}\n<!-- color: {card['color']} -->\n\n{card['body']}\n" for card in cards)


def clean_body(body):
    body = (body or "").strip()
    return re.sub(r"^(### Card )", r"\\\1", body, flags=re.M)


class CardService:
    def __init__(self, load, save, lock, filename="card.md"):
        self.load_text = load
        self.save_text = save
        self.lock = lock
        self.filename = filename

    @staticmethod
    def validate_body(body):
        body = clean_body(body)
        if not body:
            raise ValueError("empty card")
        return body

    def read(self):
        return parse_cards(self.load_text(self.filename))

    def write(self, cards):
        self.save_text(self.filename, serialize_cards(cards))

    def list(self):
        with self.lock:
            return self.read()

    def add(self, body, color=None):
        body = self.validate_body(body)
        color = color if COLOR.match(color or "") else DEFAULT_COLOR
        with self.lock:
            cards = self.read()
            card = {"id": max([item["id"] for item in cards], default=0) + 1,
                    "color": color, "body": body}
            cards.append(card)
            self.write(cards)
        return card

    def edit(self, card_id, body, color=None):
        body = self.validate_body(body)
        with self.lock:
            cards = self.read()
            card = next((item for item in cards if item["id"] == card_id), None)
            if not card:
                return None
            card["body"] = body
            if COLOR.match(color or ""):
                card["color"] = color
            self.write(cards)
        return card

    def delete(self, card_id):
        with self.lock:
            cards = [card for card in self.read() if card["id"] != card_id]
            self.write(cards)


def create_blueprint(service):
    bp = Blueprint("cards", __name__)

    @bp.get("/api/cards")
    def cards_list():
        return jsonify(service.list())

    @bp.post("/api/cards")
    def cards_add():
        body = request.get_json(silent=True)
        body = body if isinstance(body, dict) else {}
        try:
            card = service.add(body.get("body"), body.get("color"))
        except ValueError as error:
            return jsonify(error=str(error)), 400
        return jsonify(card)

    @bp.put("/api/cards/<int:card_id>")
    def cards_edit(card_id):
        body = request.get_json(silent=True)
        body = body if isinstance(body, dict) else {}
        try:
            card = service.edit(card_id, body.get("body"), body.get("color"))
        except ValueError as error:
            return jsonify(error=str(error)), 400
        if not card:
            return jsonify(error="not found"), 404
        return jsonify(card)

    @bp.delete("/api/cards/<int:card_id>")
    def cards_delete(card_id):
        service.delete(card_id)
        return jsonify(ok=True)

    return bp
