"""Logique des outils `convia_*` et `wiki_ingest_*`, hors du serveur.

Séparé de `server.py` pour une raison simple : ce fichier est testable sans monter
un serveur MCP, une authentification et un spool. `server.py` ne garde que le
décorateur `@mcp.tool()` et la traduction des erreurs.

Décision d'architecture : pas de nouveau serveur MCP pour ConvIA. ConvIA est une
fonction du Vault/RAG, la surface est donc une extension namespacée de vault-mcp.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

from vault_mcp import convia_queue, convia_view

# Toute lecture ConvIA est confinée ici. Un chemin qui n'en descend pas est refusé
# avant tout accès disque : le namespace est la frontière, pas une convention.
NAMESPACE = "raw/assets/ConvIA/"
ANALYSIS_NAMESPACE = "raw/assets/ConvIA-Analysis/"

MAX_PROJECTION_CHARS = 120_000
MAX_ANALYSIS_CHARS = 60_000

INGEST_UNIT = os.environ.get("WIKI_INGEST_UNIT", "llm-wiki-ingest.service")
INGEST_STATE_DIR = Path(os.environ.get("WIKI_INGEST_STATE", "/var/lib/llm-wiki"))
INGEST_BIN = os.environ.get("WIKI_INGEST_BIN", "/usr/local/bin/llm_wiki_ingest.sh")

# Correspondance dossier brut -> dossier d'analyse. Le modèle ne choisit jamais le
# chemin de sortie : il est dérivé de la source, donc reproductible et non forgeable.
_SOURCE_FOLDER = {
    "claude-cli": "Claude-CLI", "claude-desktop": "Claude-Desktop",
    "agy-cli": "AGY-CLI", "agy-desktop": "AGY-Desktop",
    "codex": "Codex", "freebuff-cli": "Freebuff-CLI",
    "freebuff-desktop": "Freebuff-Desktop", "freebuff": "Freebuff",
    "chatgpt-web": "ChatGPT-Web",
}


class ConviaError(RuntimeError):
    """Refus explicite : message destiné à l'appelant, jamais une trace interne."""


def _check_namespace(path: str, prefix: str = NAMESPACE) -> str:
    """Normalise et confine. Refuse toute traversée, même encodée.

    Les `..` sont rejetés avant résolution plutôt qu'après : résoudre d'abord, c'est
    accepter que le chemin ait pu sortir du namespace le temps d'un `os.path.realpath`.
    """
    clean = (path or "").strip().replace("\\", "/").lstrip("/")
    if not clean:
        raise ConviaError("chemin vide")
    if ".." in clean.split("/") or "\x00" in clean:
        raise ConviaError("chemin refusé : traversée")
    if not clean.startswith(prefix):
        raise ConviaError(f"chemin hors du namespace ConvIA : {clean}")
    return clean


def _disk_path(relative: str) -> Path:
    suffix = relative[len(NAMESPACE):]
    candidate = (convia_queue.RAW_ROOT / suffix).resolve()
    root = convia_queue.RAW_ROOT.resolve()
    if root != candidate and root not in candidate.parents:
        raise ConviaError("chemin refusé : hors du miroir ConvIA")
    return candidate


# ------------------------------------------------------------------ convia_status
def status() -> dict[str, object]:
    """Une seule vue pour répondre à « ConvIA fonctionne ? ».

    Agrège l'état déjà tenu ailleurs (file d'analyse, miroir, état llm-wiki) plutôt
    que d'inventer de nouveaux fichiers d'état à maintenir en parallèle.
    """
    etat = convia_queue.status()
    age = etat.get("corpus_age_s")
    etat["capture_fraiche"] = bool(age is not None and age < 48 * 3600)
    etat["sources"] = sorted(etat.get("pending_by_source", {}))
    etat["wiki_ingest"] = ingest_status()
    erreurs: list[str] = []
    if age is None:
        erreurs.append("corpus ConvIA introuvable dans le miroir")
    elif age > 48 * 3600:
        erreurs.append("corpus ConvIA figé depuis %.0f h" % (age / 3600))
    etat["active_errors"] = erreurs
    return etat


# -------------------------------------------------- convia_list_pending_analysis
def list_pending(limit: int = 10, sources: list[str] | None = None) -> dict[str, object]:
    """Métadonnées seulement : aucun contenu de conversation ne sort d'ici.

    La taille annoncée est celle de la PROJECTION, pas du raw : c'est elle qui
    consommera le contexte de l'analyseur, donc la seule qui l'aide à dimensionner
    son lot.
    """
    items = []
    for item in convia_queue.list_pending(limit=limit, sources=sources):
        data = item.as_dict()
        try:
            _, stats = _projection_of(item.source_path)
            data["projection_bytes"] = stats["bytes_projection"]
            data["reduction_pct"] = stats["reduction_pct"]
        except ConviaError:
            data["projection_bytes"] = None
        items.append(data)
    total = convia_queue.status()["analysis_pending"]
    return {"pending_total": total, "returned": len(items), "items": items}


# ------------------------------------------------------- convia_read_for_analysis
def _projection_of(path: str) -> tuple[str, dict[str, object]]:
    relative = _check_namespace(path)
    disk = _disk_path(relative)
    if not disk.is_file():
        raise ConviaError(f"conversation introuvable : {relative}")
    raw = disk.read_text(encoding="utf-8", errors="replace")
    view, stats = convia_view.project_markdown(raw)
    stats["source_sha256"] = convia_queue.sha256_of(disk)
    stats["source_path"] = relative
    return view, stats


def read_for_analysis(path: str, offset: int = 0, limit: int = 0) -> dict[str, object]:
    """Renvoie la projection de niveau B, JAMAIS le raw.

    C'est le seul chemin de lecture ouvert à la tâche d'analyse. Le raw reste
    accessible par `read_note` pour un humain qui enquête, pas pour la boucle
    automatique : ce serait exactement le « capture raw → analyse → nettoyage »
    que cette architecture existe pour empêcher.
    """
    view, stats = _projection_of(path)
    total = len(view)
    if limit and limit > 0:
        fragment = view[max(0, offset): max(0, offset) + limit]
    elif total > MAX_PROJECTION_CHARS:
        fragment = view[:MAX_PROJECTION_CHARS]
    else:
        fragment = view[max(0, offset):] if offset else view
    return {
        "path": stats["source_path"],
        "source_sha256": stats["source_sha256"],
        "analysis_version": convia_queue.ANALYSIS_VERSION,
        "content": fragment,
        "chars_total": total,
        "chars_returned": len(fragment),
        "truncated": len(fragment) < total - max(0, offset),
        "metrics": {k: stats[k] for k in
                    ("bytes_raw", "bytes_projection", "reduction_pct",
                     "messages", "tool_calls_seen", "tool_calls_kept")},
    }


# ----------------------------------------------------------- convia_write_analysis
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def _slug(text: str, budget: int = 60) -> str:
    normalised = unicodedata.normalize("NFKD", text)
    ascii_only = normalised.encode("ascii", "ignore").decode("ascii").lower()
    slug = _SLUG_STRIP.sub("-", ascii_only).strip("-")
    return (slug[:budget].rstrip("-")) or "conversation"


def analysis_path_for(source_path: str, source_agent: str, title: str) -> str:
    """Chemin de sortie déterministe, dérivé de la source.

    Le modèle ne le choisit pas. Deux analyses de la même conversation retombent
    forcément sur le même fichier, donc la seconde est un refus de doublon et non
    une note fantôme de plus dans le Vault.
    """
    folder = _SOURCE_FOLDER.get(source_agent, source_agent or "Autres")
    stem = Path(source_path).stem
    return f"{ANALYSIS_NAMESPACE}{folder}/{stem}__analyse.md"


def build_analysis_note(
    source_path: str, source_hash: str, source_agent: str, session_id: str,
    title: str, markdown: str,
) -> str:
    """Assemble le frontmatter de provenance devant l'analyse rendue par le modèle."""
    front = [
        "---",
        "source: convia-analysis",
        f"analysis_of: {source_path}",
        f"source_session_id: {session_id}",
        f"source_sha256: {source_hash}",
        f"source_agent: {source_agent}",
        f"analysis_version: {convia_queue.ANALYSIS_VERSION}",
        f"analyzed_at: {datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"title: {json.dumps(title or Path(source_path).stem, ensure_ascii=False)}",
        "---",
        "",
    ]
    return "\n".join(front) + convia_view.redact(markdown.strip()) + "\n"


def prepare_analysis(
    source_path: str, source_hash: str, analysis_version: str, markdown: str
) -> dict[str, object]:
    """Valide et prépare l'écriture. N'écrit pas : le dépôt reste au serveur.

    Vérifie que la source existe, que le hash annoncé est bien celui du fichier
    ACTUEL (une analyse d'une version périmée serait fausse dès son écriture), que
    la version d'analyse est celle en cours, et qu'il ne s'agit pas d'un doublon.
    """
    relative = _check_namespace(source_path)
    if analysis_version != convia_queue.ANALYSIS_VERSION:
        raise ConviaError(
            f"version d'analyse inconnue : {analysis_version} "
            f"(attendue {convia_queue.ANALYSIS_VERSION})"
        )
    if not markdown or not markdown.strip():
        raise ConviaError("analyse vide")
    if len(markdown) > MAX_ANALYSIS_CHARS:
        raise ConviaError(f"analyse trop longue : {len(markdown)} > {MAX_ANALYSIS_CHARS}")

    disk = _disk_path(relative)
    if not disk.is_file():
        raise ConviaError(f"conversation introuvable : {relative}")
    actual = convia_queue.sha256_of(disk)
    if actual != source_hash:
        raise ConviaError(
            "hash périmé : la conversation a changé depuis la lecture "
            f"(attendu {source_hash[:12]}…, actuel {actual[:12]}…). Relire avant d'analyser."
        )

    entry = convia_queue.find_entry(relative, source_hash)
    if entry is not None and entry["status"] == "done":
        raise ConviaError(
            f"analyse déjà produite pour cette version : {entry['analysis_path']}"
        )

    agent = entry["source_agent"] if entry else Path(relative).parent.name
    session = entry["session_id"] if entry else ""
    title = entry["title"] if entry else ""
    target = analysis_path_for(relative, agent, title)
    return {
        "path": target,
        "content": build_analysis_note(relative, source_hash, agent, session, title, markdown),
        "source_path": relative,
        "source_hash": source_hash,
    }


def confirm_analysis(source_path: str, source_hash: str, analysis_path: str) -> bool:
    """Marque la file APRÈS un dépôt accepté, jamais avant."""
    return convia_queue.mark_done(source_path, source_hash, analysis_path)


# ---------------------------------------------------------------- wiki_ingest_*
def _read_state(name: str) -> str | None:
    try:
        return (INGEST_STATE_DIR / name).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _systemctl(*args: str, privileged: bool = False) -> tuple[int, str]:
    """`systemctl` borné à UNE unité, jamais paramétrable par l'appelant.

    Pas de `shell(command)`, pas de `systemctl(unit)`, pas de sudo générique : la
    seule chose que le MCP puisse démarrer est le moteur d'ingestion prévu, via une
    règle sudoers qui ne cite que cette commande et cette unité.

    `privileged` n'est vrai que pour `start`. `is-active` et `show` sont lisibles
    sans privilège : les passer par sudo élargirait la règle sudoers pour rien.
    """
    prefix = ["/usr/bin/sudo", "-n"] if privileged else []
    try:
        proc = subprocess.run(
            [*prefix, "/usr/bin/systemctl", *args, INGEST_UNIT],
            capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _is_running() -> bool:
    code, _ = _systemctl("is-active", "--quiet")
    return code == 0


def ingest_status() -> dict[str, object]:
    """État du moteur réel, lu là où il l'écrit déjà.

    Rien n'est réimplémenté ici : le manifeste, les jalons de reprise et les
    compteurs appartiennent à `llm_wiki_ingest.sh`.
    """
    running = _is_running()
    due = _read_state("ingest-due-at")
    resume_at = None
    quota_wait = False
    if due and due.isdigit():
        remaining = int(due) - int(time.time())
        if remaining > 0:
            quota_wait = True
            resume_at = datetime.fromtimestamp(int(due), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    code, out = _systemctl("show", "--property=ExecMainStartTimestamp",
                           "--property=ExecMainStatus", "--property=Result")
    props = dict(
        line.split("=", 1) for line in out.splitlines() if "=" in line
    ) if code == 0 else {}
    return {
        "running": running,
        "unit": INGEST_UNIT,
        "quota_wait": quota_wait,
        "resume_at": resume_at,
        "resume_count": _read_state("resume-count"),
        "last_run": props.get("ExecMainStartTimestamp") or None,
        "last_result": props.get("Result") or None,
        "last_exit": props.get("ExecMainStatus") or None,
    }


def ingest_backlog() -> dict[str, object]:
    """Compteurs du moteur, obtenus en lui demandant plutôt qu'en les recalculant."""
    try:
        proc = subprocess.run(
            [INGEST_BIN, "--status"], capture_output=True, text=True,
            timeout=120, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"erreur": f"{type(exc).__name__}"}
    out = proc.stdout
    def grab(pattern: str) -> int | None:
        match = re.search(pattern, out)
        return int(match.group(1)) if match else None
    return {
        "eligible": grab(r"(\d+) fichiers eligibles"),
        "processed": grab(r"Traites ok\s+(\d+)"),
        "failed": grab(r"Echecs\s+failed\s+(\d+)"),
        "skipped": grab(r"Abandon skipped\s+(\d+)"),
        "remaining": grab(r"Restant a traiter\s+(\d+)"),
    }


def ingest_start() -> dict[str, object]:
    """Démarre le moteur et rend la main tout de suite.

    L'ingestion dure des heures : bloquer la requête MCP pendant ce temps ferait
    expirer l'appelant et laisserait le run orphelin. Le suivi passe par
    `wiki_ingest_status`. `Type=oneshot` + `RefuseManualStart` côté unité rendent
    un second démarrage impossible ; on le vérifie quand même avant, pour rendre
    `already_running` plutôt qu'une erreur systemd.
    """
    if _is_running():
        return {"state": "already_running", "unit": INGEST_UNIT,
                "message": "une ingestion est deja en cours"}
    code, out = _systemctl("start", "--no-block", privileged=True)
    if code != 0:
        return {"state": "error", "unit": INGEST_UNIT, "message": out[:500]}
    return {
        "run_id": datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        "state": "started",
        "unit": INGEST_UNIT,
        "message": "ingestion demarree, suivre avec wiki_ingest_status()",
    }
