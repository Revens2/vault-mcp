"""File d'attente durable des conversations à analyser.

Pourquoi une base et pas un simple parcours de répertoire à la volée : l'analyse est
faite par une tâche externe qui n'a aucune mémoire d'un run à l'autre et qui peut
mourir au milieu d'un lot. L'état « cette conversation attend une analyse » doit
survivre à ça, et à un redémarrage du serveur MCP.

L'événement est matérialisé dès que la conversation devient éligible — capturée,
arrivée dans le miroir, sanitization A terminée, pas d'analyse pour son hash courant.
Le schéma porte déjà `event_id`, `sequence`, `created_at`, `source_path`,
`source_hash` et `status` : brancher un vrai webhook plus tard consiste à lire la
table après `sequence`, sans rien refondre. Aujourd'hui le consommateur interroge
(polling), parce qu'une tâche ChatGPT native ne peut pas être réveillée par un MCP.

Identité logique d'une analyse : (source_path, source_sha256, analysis_version).
Même identité → jamais retraité. Source modifiée → nouveau hash → nouvel événement.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ANALYSIS_VERSION = "convia-analysis-v1"

RAW_ROOT = Path(os.environ.get("CONVIA_RAW_ROOT", "/srv/vault-mirror/raw/assets/ConvIA"))
ANALYSIS_SUBPATH = "raw/assets/ConvIA-Analysis"
DB_PATH = Path(os.environ.get("CONVIA_QUEUE_DB", "/var/lib/vault-mcp/convia.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_analysis (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT    NOT NULL,
    source_path  TEXT    NOT NULL,
    source_hash  TEXT    NOT NULL,
    source_agent TEXT    NOT NULL,
    session_id   TEXT    NOT NULL DEFAULT '',
    title        TEXT    NOT NULL DEFAULT '',
    updated_at   TEXT    NOT NULL DEFAULT '',
    size_bytes   INTEGER NOT NULL DEFAULT 0,
    version      TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'pending',
    analysed_at  TEXT,
    analysis_path TEXT,
    UNIQUE (source_path, source_hash, version)
);
CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_analysis (status, event_id);

CREATE TABLE IF NOT EXISTS scan_state (
    cle    TEXT PRIMARY KEY,
    valeur TEXT NOT NULL
);
"""


class QueueError(RuntimeError):
    pass


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    # WAL : le scanner écrit pendant que les outils MCP lisent, sans se bloquer.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


_FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def _frontmatter_fields(path: Path) -> dict[str, str]:
    """Champs utiles du frontmatter, lus sans YAML.

    Volontairement naïf et borné aux 8 premiers Ko : le corpus est une entrée hostile
    (adr/0008) et un parseur YAML complet dessus est une surface d'attaque gratuite
    pour trois chaînes de caractères.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(8192)
    except OSError:
        return {}
    match = _FRONT.match(head)
    if not match:
        return {}
    out: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if line.startswith((" ", "-", "\t")) or ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip().strip("'\"")
    return out


@dataclass
class PendingItem:
    event_id: int
    source_path: str
    source_agent: str
    session_id: str
    title: str
    updated_at: str
    source_hash: str
    size_bytes: int

    def as_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "path": self.source_path,
            "source": self.source_agent,
            "session_id": self.session_id,
            "title": self.title,
            "updated_at": self.updated_at,
            "hash": self.source_hash,
            "size_bytes": self.size_bytes,
        }


def _relative(path: Path) -> str:
    """Chemin tel que le Vault le nomme, pas le chemin disque du miroir."""
    try:
        return "raw/assets/ConvIA/" + str(path.relative_to(RAW_ROOT)).replace(os.sep, "/")
    except ValueError:
        return str(path)


def _is_conversation(path: Path) -> bool:
    """Vrai pour `raw/assets/ConvIA/<source>/<fichier>.md`.

    Un `.md` pose directement a la racine ConvIA n'est PAS une conversation :
    l'interpreter comme un provider créerait une source fantome (agent = nom du
    dossier racine, ex. « ConvIA » — incident canary E2E du 2026-09-07, file
    d'analyse polluée). L'invariant minimal — le chemin relatif doit contenir au
    moins `<source>/<fichier>` — suffit et reste extensible : n'importe quel
    nouveau dossier source est accepté sans liste fermée de providers.
    `_attachments/` (joints recopiés a cote des conversations) n'est pas non
    plus une conversation.
    """
    if "_attachments" in path.parts:
        return False
    try:
        return len(path.relative_to(RAW_ROOT).parts) >= 2
    except ValueError:
        return False


def scan(limit: int = 0) -> dict[str, int]:
    """Réconcilie la file avec l'état du miroir. Idempotent.

    Ne calcule un hash que pour un fichier dont la taille ou le mtime a bougé depuis
    le dernier passage : sur 639 conversations, tout hacher à chaque tour coûterait
    plusieurs secondes pour aucune information nouvelle.
    """
    stats = {"vus": 0, "nouveaux": 0, "modifies": 0, "inchanges": 0,
             "reprises": 0, "mal_places": 0}
    if not RAW_ROOT.is_dir():
        return stats
    stats["reprises"] = reconcile_lost_analyses()

    conn = connect()
    try:
        known: dict[str, tuple[str, str]] = {
            row["source_path"]: (row["source_hash"], row["status"])
            for row in conn.execute(
                "SELECT source_path, source_hash, status FROM pending_analysis"
                " WHERE version = ? ORDER BY event_id DESC",
                (ANALYSIS_VERSION,),
            )
        }
        seen_paths: set[str] = set()
        for path in sorted(RAW_ROOT.rglob("*.md")):
            # Layout valide : <source>/<fichier>.md. Un .md a la racine ConvIA
            # ou sous _attachments/ est ignore (jamais en file, jamais en
            # source) ; les racines sont comptees pour observabilite.
            if not _is_conversation(path):
                if "_attachments" not in path.parts:
                    stats["mal_places"] += 1
                continue
            stats["vus"] += 1
            rel = _relative(path)
            if rel in seen_paths:
                continue
            seen_paths.add(rel)
            try:
                size = path.stat().st_size
            except OSError:
                continue
            digest = sha256_of(path)
            previous = known.get(rel)
            if previous and previous[0] == digest:
                stats["inchanges"] += 1
                continue
            fields = _frontmatter_fields(path)
            agent = fields.get("source") or path.parent.name
            conn.execute(
                "INSERT OR IGNORE INTO pending_analysis"
                " (created_at, source_path, source_hash, source_agent, session_id,"
                "  title, updated_at, size_bytes, version, status)"
                " VALUES (?,?,?,?,?,?,?,?,?,'pending')",
                (
                    _now(), rel, digest, agent, fields.get("session_id", ""),
                    fields.get("title", ""), fields.get("updated_at", ""),
                    size, ANALYSIS_VERSION,
                ),
            )
            stats["modifies" if previous else "nouveaux"] += 1
            if limit and (stats["nouveaux"] + stats["modifies"]) >= limit:
                break
        conn.execute(
            "INSERT INTO scan_state (cle, valeur) VALUES ('last_scan', ?)"
            " ON CONFLICT(cle) DO UPDATE SET valeur = excluded.valeur",
            (_now(),),
        )
        conn.commit()
    finally:
        conn.close()
    return stats


MIRROR_ROOT = Path(os.environ.get("CONVIA_MIRROR_ROOT", "/srv/vault-mirror"))
# Un depot accepte par le spool n est pas encore une note sur le Drive. Le pousseur
# est fiable mais pas infaillible, et une intention peut finir en echec longtemps
# apres. Au-dela de ce delai, une analyse annoncee ecrite mais introuvable dans le
# miroir est consideree perdue et la conversation repart en file. Large a dessein :
# Drive puis miroir prennent deja jusqu a 30 min, et un reindex en cours peut
# retarder le pousseur de plusieurs heures.
RECONCILE_AFTER_S = 24 * 3600


def reconcile_lost_analyses() -> int:
    """Remet en file les analyses marquees ecrites mais absentes du miroir.

    Sans ce filet, un echec du spool laisserait une conversation `done` sans note :
    silencieusement jamais analysee, et invisible dans le backlog.
    """
    conn = connect()
    remises = 0
    try:
        limite = datetime.now(UTC).timestamp() - RECONCILE_AFTER_S
        for row in conn.execute(
            "SELECT event_id, analysis_path, analysed_at FROM pending_analysis"
            " WHERE status = 'done' AND analysis_path IS NOT NULL"
        ):
            try:
                ecrit_a = datetime.strptime(
                    row["analysed_at"], "%Y-%m-%dT%H:%M:%SZ"
                ).replace(tzinfo=UTC).timestamp()
            except (TypeError, ValueError):
                continue
            if ecrit_a > limite:
                continue
            if (MIRROR_ROOT / row["analysis_path"]).is_file():
                continue
            conn.execute(
                "UPDATE pending_analysis SET status = 'pending', analysed_at = NULL,"
                " analysis_path = NULL WHERE event_id = ?",
                (row["event_id"],),
            )
            remises += 1
        conn.commit()
    finally:
        conn.close()
    return remises


def list_pending(limit: int = 10, sources: list[str] | None = None) -> list[PendingItem]:
    conn = connect()
    try:
        sql = ("SELECT * FROM pending_analysis WHERE status = 'pending' AND version = ?")
        params: list[object] = [ANALYSIS_VERSION]
        if sources:
            sql += " AND source_agent IN (%s)" % ",".join("?" * len(sources))
            params.extend(sources)
        # Le plus ancien d'abord : le backlog se vide par la tête, pas par la queue.
        sql += " ORDER BY event_id ASC LIMIT ?"
        params.append(max(1, min(limit or 10, 100)))
        return [
            PendingItem(
                event_id=row["event_id"], source_path=row["source_path"],
                source_agent=row["source_agent"], session_id=row["session_id"],
                title=row["title"], updated_at=row["updated_at"],
                source_hash=row["source_hash"], size_bytes=row["size_bytes"],
            )
            for row in conn.execute(sql, params)
        ]
    finally:
        conn.close()


def find_entry(source_path: str, source_hash: str) -> sqlite3.Row | None:
    conn = connect()
    try:
        return conn.execute(
            "SELECT * FROM pending_analysis WHERE source_path = ? AND source_hash = ?"
            " AND version = ?",
            (source_path, source_hash, ANALYSIS_VERSION),
        ).fetchone()
    finally:
        conn.close()


def mark_done(source_path: str, source_hash: str, analysis_path: str) -> bool:
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE pending_analysis SET status = 'done', analysed_at = ?, analysis_path = ?"
            " WHERE source_path = ? AND source_hash = ? AND version = ? AND status != 'done'",
            (_now(), analysis_path, source_path, source_hash, ANALYSIS_VERSION),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def status() -> dict[str, object]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) n FROM pending_analysis WHERE version = ? GROUP BY status",
            (ANALYSIS_VERSION,),
        ).fetchall()
        counts = {row["status"]: row["n"] for row in rows}
        oldest = conn.execute(
            "SELECT created_at, source_path FROM pending_analysis"
            " WHERE status = 'pending' AND version = ? ORDER BY event_id ASC LIMIT 1",
            (ANALYSIS_VERSION,),
        ).fetchone()
        last = conn.execute(
            "SELECT analysed_at, analysis_path FROM pending_analysis"
            " WHERE status = 'done' ORDER BY analysed_at DESC LIMIT 1"
        ).fetchone()
        by_source = {
            row["source_agent"]: row["n"]
            for row in conn.execute(
                "SELECT source_agent, COUNT(*) n FROM pending_analysis"
                " WHERE status = 'pending' AND version = ? GROUP BY source_agent",
                (ANALYSIS_VERSION,),
            )
        }
        scan_row = conn.execute(
            "SELECT valeur FROM scan_state WHERE cle = 'last_scan'"
        ).fetchone()
        return {
            "analysis_version": ANALYSIS_VERSION,
            "analysis_pending": counts.get("pending", 0),
            "analysis_done": counts.get("done", 0),
            "pending_by_source": by_source,
            "oldest_analysis_pending": dict(oldest) if oldest else None,
            "last_analysis": dict(last) if last else None,
            "last_scan": scan_row["valeur"] if scan_row else None,
            "raw_root": str(RAW_ROOT),
            "corpus_files": _corpus_count(),
            "corpus_age_s": _corpus_age_s(),
        }
    finally:
        conn.close()


def _corpus_count() -> int:
    if not RAW_ROOT.is_dir():
        return 0
    return sum(1 for p in RAW_ROOT.rglob("*.md") if _is_conversation(p))


def _corpus_age_s() -> float | None:
    """Âge de la conversation la plus récente : c'est la sonde de fraîcheur de la chaîne.

    Une capture Windows arrêtée laisse tous les timers en succès en tournant à vide —
    c'est arrivé du 16 au 22 août 2026. Seule la fraîcheur du produit le montre.
    Seules les conversations `<source>/<fichier>.md` comptent : un .md pose a la
    racine ConvIA (canary, artefact) ne doit ni gonfler le corpus ni le rajeunir.
    """
    if not RAW_ROOT.is_dir():
        return None
    # `newest is None` distingue « aucun fichier » de « fichier daté de l epoch ».
    # Un `if newest:` confondrait les deux et rendrait None sur un corpus present
    # mais date de 1970 -- exactement le cas qu on cherche a signaler.
    newest: float | None = None
    for path in RAW_ROOT.rglob("*.md"):
        if not _is_conversation(path):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        newest = mtime if newest is None else max(newest, mtime)
    return round(time.time() - newest, 1) if newest is not None else None


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "status":
        print(json.dumps(status(), indent=1, ensure_ascii=False))
    else:
        print(json.dumps(scan(), indent=1))
