"""Convert an uploaded exam PDF and save it as a Practice Hub question file."""
import tempfile
from pathlib import Path

from flask import Blueprint, jsonify, request
from tools.questions import parse_name
from pdf2md.converter import convert, detect_kind


def create_blueprint(save_new):
    bp = Blueprint("pdf_converter", __name__)

    @bp.post("/api/pdf-convert")
    def pdf_convert():
        file = request.files.get("pdf")
        name = request.form.get("name", "").strip()
        if not file or not file.filename or not file.filename.lower().endswith(".pdf"):
            return jsonify(error="Choose a PDF file."), 400
        if not parse_name(name):
            return jsonify(error="Invalid question filename. Use a name such as U_OFF_P_B_1.md."), 400
        try:
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "upload.pdf"
                file.save(path)
                kind = detect_kind(str(path))
                # Image files are not currently supported by Practice Hub storage.
                text, warnings = convert(str(path), bool(request.form.get("status")), kind=kind)
            count = text.count("### Q ")
            if not count:
                return jsonify(error="No questions found. Please check that this is a supported exam result PDF.", warnings=warnings), 422
            location = save_new(name, text)
            return jsonify(ok=True, filename=name, kind=kind, questions=count,
                           warnings=warnings, location=location)
        except FileExistsError:
            return jsonify(error=f"{name} already exists. Choose a different number; existing files are not overwritten."), 409
        except Exception as exc:
            return jsonify(error=f"Could not convert or save PDF: {exc}"), 500

    return bp
