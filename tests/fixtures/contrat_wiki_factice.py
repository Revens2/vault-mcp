"""Contrat FACTICE pour les tests : meme forme que llm_wiki_extract, contenu minimal.

Ce n'est PAS le contrat wiki-extract-v4 : il eprouve le mecanisme (chargement,
digest, fail closed, portail canonique). Le vrai contrat est eprouve par les
tests `*_reel` quand le module canonique est present (VPS).
"""
import re
import sys

# Comme le vrai module : effet de bord sur sys.path, que le chargeur doit annuler.
sys.path.insert(0, "/chemin/factice/qui-ne-doit-pas-rester")

CONTRACT_VERSION = "wiki-extract-v4"


def _s(desc=None, enum=None, nullable=False):
    d = {"type": "STRING"}
    if desc:
        d["description"] = desc
    if enum:
        d["enum"] = enum
        d["format"] = "enum"
    if nullable:
        d["nullable"] = True
    return d


RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "propertyOrdering": ["language", "confidence", "note", "entities", "relations", "issues"],
    "required": ["language", "confidence", "note", "entities", "relations", "issues"],
    "properties": {
        "language": _s("Code langue."),
        "confidence": {"type": "NUMBER"},
        "note": {
            "type": "OBJECT",
            "required": ["slug", "title", "tags", "summary", "sections", "warnings"],
            "properties": {
                "slug": _s("slug"),
                "title": _s("titre"),
                "tags": {"type": "ARRAY", "items": _s("tag")},
                "doc_date": _s("date", nullable=True),
                "summary": _s("resume"),
                "sections": {
                    "type": "ARRAY",
                    "items": {"type": "OBJECT", "required": ["heading", "markdown"],
                              "properties": {"heading": _s(), "markdown": _s()}},
                },
                "warnings": {
                    "type": "ARRAY",
                    "items": {"type": "OBJECT", "required": ["kind", "text"],
                              "properties": {"kind": _s(), "about": _s(), "text": _s()}},
                },
            },
        },
        "entities": {
            "type": "ARRAY",
            "items": {"type": "OBJECT", "required": ["slug", "name"],
                      "properties": {"slug": _s(), "name": _s(),
                                     "kind": _s(None, enum=["entity", "concept"]),
                                     "subtype": _s(), "aliases": {"type": "ARRAY", "items": _s()},
                                     "tags": {"type": "ARRAY", "items": _s()},
                                     "definition": _s(), "evidence": _s(),
                                     "salience": _s(None, enum=["primary", "secondary", "passing"])}},
        },
        "relations": {
            "type": "ARRAY",
            "items": {"type": "OBJECT",
                      "properties": {"from": _s(), "to": _s(), "type": _s(), "evidence": _s(),
                                     "confidence": {"type": "NUMBER"}}},
        },
        "issues": {
            "type": "ARRAY",
            "items": {"type": "OBJECT", "properties": {"code": _s(), "detail": _s()}},
        },
    },
}

SYSTEM_PREFIX = "Consignes factices d'extraction."

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def validate(doc, source_path=None):
    if any(i.get("code") == "rejet-canonique" for i in doc.get("issues") or []):
        return False, ["rejet canonique factice"], [], doc
    ents = doc.get("entities") or []
    kept = [e for e in ents if not _DATE.search(e.get("slug") or "")]
    warns = ["entite datee rejetee"] if len(kept) != len(ents) else []
    doc["entities"] = kept
    return True, [], warns, doc
