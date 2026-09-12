"""Jeux de donnees CANARY, 100 % synthetiques.

Aucun contenu reel : texte genere, marqueur `CANARY-E2E` dans le nom de fichier,
le titre, la session et chaque message. La distribution « realistic » reprend
seulement des TAILLES mesurees en production le 2026-09-12 (sources en attente :
moyenne 168 Ko, max 3,49 Mo ; reponses read : mediane ~40 Ko, p90 ~150 Ko),
jamais un octet de conversation.

Profils :
- tiny          : N petites conversations (~1 Ko).
- realistic     : N conversations de tailles log-normales bornees.
- pathological  : N-3 normales + 1 geante (> MAX_PROJECTION_CHARS) + 1 sans
                  frontmatter + 1 dont la source changera entre read et write.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

MARK = "CANARY-E2E"
SOURCE = "canary-cli"


@dataclass
class Conv:
    name: str
    body: str
    kind: str = "normal"


def _message_block(rng: random.Random, i: int, target_chars: int) -> str:
    words = ("deploiement", "service", "timer", "erreur", "relance", "config",
             "journal", "test", "sonde", "fichier", "canary", "synthetique")
    phrase = " ".join(rng.choice(words) for _ in range(12)) + ". "
    user = (phrase * max(1, target_chars // (2 * len(phrase))))[: target_chars // 2]
    bot = (phrase * max(1, target_chars // (2 * len(phrase))))[: target_chars // 2]
    return (f"## 👤 User — 2026-09-01 10:{i % 60:02d}:00\n\n[{MARK}] {user}\n\n"
            f"## 🤖 Assistant — 2026-09-01 10:{i % 60:02d}:05 · canary-model\n\n"
            f"[{MARK}] {bot}\n\n")


def conversation(idx: int, size_chars: int, seed: int = 0) -> Conv:
    rng = random.Random(seed * 100_003 + idx)  # noqa: S311 -- donnees de test
    session = f"canary-{seed:04d}-{idx:05d}"
    head = (f"---\nsource: {SOURCE}\nsession_id: {session}\n"
            f"title: {MARK} conversation {idx}\nconvia_sanitized: 2\n---\n\n"
            f"# {MARK} conversation {idx}\n\n")
    parts = [head]
    n_msgs = max(1, size_chars // 4000)
    per = max(200, size_chars // n_msgs)
    for i in range(n_msgs):
        parts.append(_message_block(rng, i, per))
    return Conv(name=f"2026-09-01_{MARK.lower()}-{seed:04d}-{idx:05d}.md", body="".join(parts))


def tiny(n: int = 50, seed: int = 1) -> list[Conv]:
    return [conversation(i, 800, seed) for i in range(n)]


def realistic(n: int = 50, seed: int = 2, cap: int = 400_000) -> list[Conv]:
    rng = random.Random(seed)  # noqa: S311 -- donnees de test
    out = []
    for i in range(n):
        size = int(min(cap, max(2_000, rng.lognormvariate(10.3, 1.1))))  # mediane ~30 Ko
        out.append(conversation(i, size, seed))
    return out


def pathological(n: int = 50, seed: int = 3) -> list[Conv]:
    convs = [conversation(i, 3_000, seed) for i in range(n - 3)]
    huge = conversation(n - 3, 400_000, seed)
    huge.kind = "huge"
    bare = Conv(name=f"2026-09-01_{MARK.lower()}-{seed:04d}-bare.md",
                body=f"[{MARK}] conversation sans frontmatter\n" * 20, kind="no_frontmatter")
    stale = conversation(n - 1, 3_000, seed)
    stale.kind = "will_go_stale"
    return [*convs, huge, bare, stale]


def analysis_markdown(idx: int) -> str:
    return (f"# Analyse {MARK} {idx}\n\n- Probleme : synthetique\n- Resolution : synthetique\n"
            f"- Lecon : aucune donnee reelle ({MARK})\n")


def wiki_doc(idx: int) -> str:
    return (f"# {MARK} document wiki {idx}\n\n"
            + f"Paragraphe synthetique {idx} sur un service fictif canary. " * 30)


def wiki_extraction(slug: str) -> dict[str, object]:
    body = (f"Corps synthetique {MARK} largement suffisant pour depasser les deux cents "
            "caracteres exiges par la validation serveur de la fiche. " * 3)
    return {
        "language": "fr", "confidence": 0.8,
        "note": {"slug": slug, "title": f"{MARK} {slug}", "tags": ["canary", "synthetique"],
                 "doc_date": "", "summary": "Fiche canary synthetique.",
                 "sections": [{"heading": "Resume", "markdown": body + " Voir {{E:ent-canary}}."}],
                 "warnings": []},
        "entities": [{"slug": "ent-canary", "name": "Ent Canary", "kind": "entity",
                      "subtype": "systeme", "aliases": [], "tags": ["canary"],
                      "definition": "Entite canary.", "evidence": "document canary",
                      "salience": "primary"}],
        "relations": [], "issues": [],
    }
