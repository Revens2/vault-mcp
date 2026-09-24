"""Worker alternatif de la file Wiki : OpenCode + Muse Spark pour la route `alternate`.

Traite les jobs que le consommateur ChatGPT a routes par
`wiki_ingest_release(action="alternate")` (blocage de plateforme). Un run = un lot
borne puis sortie (unite systemd oneshot, reveillee par un marqueur + un timer de
rattrapage).

Le worker n'ecrit JAMAIS une fiche. Il suit le chemin serveur du consommateur
principal : claim_alternate -> read_job -> submit -> merge_pending (memes
validateurs, meme contrat canonique, meme spool, meme fusion deterministe).
Le modele ne recoit que la projection sure du chunk (`wiki_projection`) et le
contrat du job, par fichiers : aucun contenu non fiable sur une ligne de commande.

Deux familles d'echec, jamais confondues :
* provider/infra (OpenCode absent, 429, reseau, modele indisponible, timeout) :
  le job retourne dans SA file sans consommer de tentative, backoff GLOBAL, le lot
  s'arrete (on ne brule pas les jobs suivants) ;
* reponse inexploitable (JSON invalide, contrat refuse, citation non litterale,
  valeur ressemblant a un secret) : tentative de contenu consommee ; la suivante
  recoit les erreurs exactes ; 3 -> alternate_quarantined + UNE notification.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
import tempfile
import time
from pathlib import Path

from vault_mcp import wiki_jobs as wj
from vault_mcp import wiki_projection as wp

MODEL_ID = os.environ.get("WIKI_ALT_MODEL", "")
PROVIDER = os.environ.get("WIKI_ALT_PROVIDER", "opencode").strip().lower()
AGY_BRIDGE_DIR = Path(os.environ.get("WIKI_ALT_AGY_BRIDGE",
                                     "/var/lib/llm-wiki/agy"))
AGY_BRIDGE_POLL_S = float(os.environ.get("WIKI_ALT_AGY_POLL", "2"))
AGY_MODEL = os.environ.get("WIKI_ALT_AGY_MODEL", "gemini-3.7-flash")
AGY_EFFORT = os.environ.get("WIKI_ALT_AGY_EFFORT", "medium")
# Limite d'un argument execve : au-dela le run tombe avant tout appel.
AGY_PROMPT_MAX = int(os.environ.get("WIKI_ALT_AGY_PROMPT_MAX", "120000"))

OPENCODE_BIN = os.environ.get("WIKI_ALT_OPENCODE", "/usr/local/lib/opencode/opencode")
AGENT = os.environ.get("WIKI_ALT_AGENT", "extract")
STATE_DIR = Path(os.environ.get("WIKI_ALT_STATE", "/var/lib/llm-wiki-opencode"))
NOTIFY_SPOOL = Path(os.environ.get("WIKI_ALT_NOTIFY_SPOOL", "/var/lib/llm-wiki/notify-spool"))
INGEST_REQUEST = Path(os.environ.get(
    "WIKI_INGEST_REQUEST", "/var/lib/vault-mcp/wiki-ingest.request"))
CALL_TIMEOUT_S = int(os.environ.get("WIKI_ALT_TIMEOUT", "600"))
LEASE_S = int(os.environ.get("WIKI_ALT_LEASE", "3600"))
MAX_PROVIDER_FAILURES_PER_JOB = 10
BACKOFF_BASE_S = 300
BACKOFF_MAX_S = 6 * 3600
GLOBAL_ALERT_AFTER = 3
GLOBAL_ALERT_COOLDOWN_S = 6 * 3600

# Consignes courtes passees en argument : texte statique, jamais du contenu source.
MESSAGE_FIRST = ("Traite l'archive documentaire jointe (document.md) selon le contrat joint"
                 " (contrat.json). Reponds uniquement par l'objet JSON `extraction`.")
MESSAGE_RETRY = ("Ta reponse precedente a ete refusee. Les erreurs exactes sont dans"
                 " erreurs.txt. Refais l'extraction du meme document selon le meme contrat"
                 " en corrigeant ces erreurs. Reponds uniquement par l'objet JSON `extraction`.")

# Variables transmises au processus OpenCode : liste blanche, rien d'autre ne passe.
_ENV_PREFIXES = ("OPENCODE_",)
_ENV_KEYS = ("LANG", "LC_ALL", "TZ")


class ProviderError(Exception):
    """Panne globale (provider, OpenCode, reseau) : jamais la faute du document."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


class ModelOutputError(Exception):
    """Reponse du modele inexploitable pour CE document : tentative consommee."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = [str(e)[:300] for e in errors][:20] or ["reponse invalide"]


def log(msg: str) -> None:
    print(f"[llm-wiki-opencode] {msg}", flush=True)


# ------------------------------------------------------------------ OpenCode
_PROVIDER_PATTERNS = (
    ("rate_limit", re.compile(r"\b429\b|rate.?limit|too many requests|FreeUsageLimit|quota",
                              re.I)),
    ("model_unavailable", re.compile(r"ModelNotFound|Model not found|model.*unavailable", re.I)),
    ("auth", re.compile(r"\b40[13]\b|unauthori[sz]ed|forbidden|invalid api key|AuthError",
                        re.I)),
    ("network", re.compile(r"ECONNREFUSED|ECONNRESET|ETIMEDOUT|ENOTFOUND|EAI_AGAIN|fetch failed"
                           r"|socket hang up|network", re.I)),
    ("server", re.compile(r"\b5\d\d\b|Unexpected server error|overloaded|unavailable", re.I)),
)
# Refus de contenu cote provider : propre au document, donc tentative consommee.
_CONTENT_REFUSAL = re.compile(r"content.?(?:policy|filter)|moderation|safety", re.I)


def classify_failure(blob: str) -> str:
    for kind, pattern in _PROVIDER_PATTERNS:
        if pattern.search(blob):
            return kind
    return "unknown"


def parse_events(returncode: int, stdout: bytes, stderr: bytes) -> str:
    """Texte final d'un `opencode run --format json` (un evenement JSON par ligne).

    Seuls step_start / text / step_finish sont admis : l'agent n'a aucun outil,
    un evenement d'outil signale une configuration compromise -> arret global.
    """
    parts: list[str] = []
    finish = None
    errors: list[object] = []
    other: list[str] = []
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        kind = ev.get("type")
        part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
        if kind == "text":
            parts.append(str(part.get("text") or ""))
        elif kind == "step_finish":
            finish = part.get("reason")
        elif kind == "error":
            errors.append(ev.get("error"))
        elif kind != "step_start":
            other.append(str(kind))
    tail = stderr.decode("utf-8", errors="replace")[-4000:]
    if returncode != 0 or errors:
        # Journal en INFO : classer sur les seules lignes d'erreur, sinon un mot banal
        # d'une ligne INFO (« safety », « network »...) fausserait le verdict.
        err_lines = "\n".join(ln for ln in tail.splitlines() if "rror" in ln) or tail
        blob = json.dumps(errors, ensure_ascii=False)[:2000] + "\n" + err_lines
        if _CONTENT_REFUSAL.search(blob) and classify_failure(blob) not in ("rate_limit", "auth"):
            raise ModelOutputError(["refus de contenu par le provider"])
        kind = classify_failure(blob)
        first = next((ln for ln in blob.splitlines() if "rror" in ln), blob.strip()[:200])
        raise ProviderError(kind, f"rc={returncode} {wp.redact(first)[0][:200]}")
    if other:
        raise ProviderError("securite", f"evenement(s) non autorise(s) : {sorted(set(other))}")
    if finish != "stop":
        raise ModelOutputError([f"reponse incomplete (fin={finish})"])
    return "".join(parts)


def _opencode_env(data_home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k.startswith(_ENV_PREFIXES) or k in _ENV_KEYS}
    env.update({
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(STATE_DIR),
        "XDG_CONFIG_HOME": str(STATE_DIR / "config"),
        "XDG_CACHE_HOME": str(STATE_DIR / "cache"),
        "XDG_STATE_HOME": str(STATE_DIR / "state"),
        # Donnees (sessions, donc le document) : ephemeres, detruites apres l'appel.
        "XDG_DATA_HOME": str(data_home),
    })
    return env


def run_opencode(message: str, files: list[Path], title: str, workdir: Path) -> str:
    if not MODEL_ID:
        raise ProviderError("config", "WIKI_ALT_MODEL absent")
    data_home = Path(tempfile.mkdtemp(prefix="data-", dir=workdir))
    cwd = workdir / "cwd"
    cwd.mkdir(exist_ok=True)
    cmd = [OPENCODE_BIN, "run", "--pure", "--agent", AGENT, "-m", MODEL_ID,
           "--format", "json", "--title", title, "--dir", str(cwd),
           "--print-logs", "--log-level", "INFO", message]
    for f in files:
        cmd += ["-f", str(f)]
    try:
        # Liste d'arguments, jamais de shell ; le contenu non fiable passe par -f.
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,  # noqa: S603
                              timeout=CALL_TIMEOUT_S, env=_opencode_env(data_home),
                              cwd=cwd, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ProviderError("timeout", f"aucune reponse en {CALL_TIMEOUT_S}s") from exc
    except OSError as exc:
        raise ProviderError("opencode", f"lancement impossible : {exc.strerror}") from exc
    finally:
        shutil.rmtree(data_home, ignore_errors=True)
    text = parse_events(proc.returncode, proc.stdout, proc.stderr)
    check_model(proc.stderr)
    return text


# Journal INFO d'OpenCode : `providerID=opencode modelID=muse-spark-...` a chaque appel
# au modele. Ni prompt ni document n'y figurent (verifie sur 1.18.30) ; stderr n'est
# jamais conserve, seul l'identifiant du modele est journalise.
_MODEL_LOG = re.compile(r"providerID=(\S+)\s+modelID=(\S+)")


def check_model(stderr: bytes) -> None:
    seen = {f"{p}/{m}" for p, m in _MODEL_LOG.findall(stderr.decode("utf-8", errors="replace"))}
    if seen and seen != {MODEL_ID}:
        raise ProviderError("config", f"modele utilise {sorted(seen)} != {MODEL_ID}")
    log(f"modele {'confirme' if seen else 'NON confirme par le journal'} : {MODEL_ID}")


# ----------------------------------------------------------------- AGY (Gemini)
# Second fournisseur de la route alternative. Existe parce que les deux transports
# precedents peuvent tomber en meme temps : bridge ChatGPT en CHALLENGE_REQUIRED et
# palier gratuit OpenCode ferme (403 « can only be used from within OpenCode ») --
# file sans consommateur depuis le 2026-09-14. Meme contrat, meme projection
# caviardee, meme validation : seul l'appel change.
#
# AGY est agentique ; ici il ne doit rien faire d'autre que repondre. Les artefacts
# sont donc INLINE dans le prompt (aucun --add-dir, aucun outil, aucun acces disque
# au document) et la sortie n'est lue que comme une donnee.
def _agy_prompt(message: str, files: list[Path]) -> str:
    morceaux = [message]
    for f in files:
        contenu = f.read_text(encoding="utf-8")
        morceaux.append(f"=== {f.name} ===\n{contenu}\n=== fin {f.name} ===")
    return "\n\n".join(morceaux)


def _bridge_call(prompt: str, title: str) -> str:
    """Depose la consigne dans le pont AGY et attend la reponse.

    Le worker est confine (NoNewPrivileges, aucune capacite) et n'a ni le profil
    OAuth ni le droit de changer de compte : c'est agy-bridge.service, sous le
    compte detenteur du profil, qui execute l'appel. Voir /usr/local/bin/agy_bridge.py.
    """
    ident = f"{title}-{uuid.uuid4().hex[:12]}"
    req = AGY_BRIDGE_DIR / f"{ident}.req"
    res = AGY_BRIDGE_DIR / f"{ident}.res"
    err = AGY_BRIDGE_DIR / f"{ident}.err"
    tmp = AGY_BRIDGE_DIR / f"{ident}.req.depot"
    try:
        tmp.write_text(prompt, encoding="utf-8")
        # 0660 EXPLICITE : le lecteur (agy-bridge.service, compte convia) n'est
        # pas l'ecrivain (juliann-app) ; les deux partagent le groupe agybridge
        # (repertoire en 2770). Un 0600 casserait le pont (timeout CALL_TIMEOUT_S+180s).
        os.chmod(tmp, 0o660)  # nosemgrep: insecure-file-permissions
        tmp.rename(req)  # publication atomique : jamais de consigne tronquee
    except OSError as exc:
        raise ProviderError("agy", f"pont indisponible : {exc.strerror}") from exc
    limite = time.monotonic() + CALL_TIMEOUT_S + 180
    try:
        while time.monotonic() < limite:
            if res.is_file():
                return res.read_text(encoding="utf-8")
            if err.is_file():
                raise ProviderError("agy", err.read_text(encoding="utf-8")[:200])
            time.sleep(AGY_BRIDGE_POLL_S)
        raise ProviderError("timeout", f"pont AGY muet en {CALL_TIMEOUT_S + 180}s")
    finally:
        for f in (tmp, req, res, err):
            with contextlib.suppress(OSError):
                f.unlink()


def run_agy(message: str, files: list[Path], title: str, workdir: Path) -> str:
    prompt = _agy_prompt(message, files)
    taille = len(prompt.encode("utf-8"))
    if taille > AGY_PROMPT_MAX:
        # Propre a CE document (projection trop grosse) : tentative consommee plutot
        # qu'un backoff global qui figerait toute la file.
        raise ModelOutputError([f"projection trop longue pour un appel AGY ({taille} o)"])
    ligne = _bridge_call(prompt, f"wiki-{title[-8:]}").strip()
    try:
        ev = json.loads(ligne)
    except ValueError as exc:
        raise ProviderError("agy", f"sortie AGY illisible : {exc}") from exc
    if str(ev.get("status") or "").upper() != "SUCCESS":
        blob = json.dumps(ev, ensure_ascii=False)[:2000]
        if _CONTENT_REFUSAL.search(blob) and classify_failure(blob) not in ("rate_limit", "auth"):
            raise ModelOutputError(["refus de contenu par le provider"])
        raise ProviderError(classify_failure(blob),
                            f"status={ev.get('status')} "
                            + wp.redact(str(ev.get("error") or "")[:200])[0])
    usage = ev.get("usage") if isinstance(ev.get("usage"), dict) else {}
    log(f"modele confirme : {AGY_MODEL} ({ev.get('duration_seconds')}s,"
        f" {usage.get('total_tokens')} tokens)")
    return str(ev.get("response") or "")


def run_provider(message: str, files: list[Path], title: str, workdir: Path) -> str:
    """Aiguillage du fournisseur de la route alternative (WIKI_ALT_PROVIDER)."""
    if PROVIDER == "agy":
        return run_agy(message, files, title, workdir)
    if PROVIDER == "opencode":
        return run_opencode(message, files, title, workdir)
    raise ProviderError("config", f"WIKI_ALT_PROVIDER inconnu : {PROVIDER}")


def model_courant() -> str:
    return AGY_MODEL if PROVIDER == "agy" else MODEL_ID


# ------------------------------------------------------------ reponse modele
_FENCED = re.compile(r"\A```(?:json)?[ \t]*\n(.*)\n```\Z", re.S)


def parse_extraction(text: str) -> dict:
    """Seules corrections admises, purement syntaxiques : espaces/BOM, UNE clôture
    ``` englobante, enveloppe {"extraction": {...}}. Jamais de reecriture du contenu."""
    t = text.strip().lstrip("﻿").strip()
    m = _FENCED.match(t)
    if m:
        t = m.group(1).strip()
    if not t:
        raise ModelOutputError(["reponse vide"])
    try:
        obj = json.loads(t)
    except ValueError as exc:
        raise ModelOutputError([f"JSON invalide : {exc.msg} (ligne {exc.lineno},"
                                f" colonne {exc.colno})"]) from exc
    if isinstance(obj, dict) and set(obj) == {"extraction"} and isinstance(
            obj["extraction"], dict):
        obj = obj["extraction"]
    if not isinstance(obj, dict):
        raise ModelOutputError(["la racine doit etre l'objet JSON `extraction`"])
    return obj


def _norm_ws(s: str) -> str:
    return " ".join(s.split()).casefold()


def evidence_errors(extraction: dict, projection_text: str) -> list[str]:
    """Citations `evidence` litterales, prises dans la projection fournie."""
    hay = _norm_ws(projection_text)
    errs = []
    for e in extraction.get("entities") or []:
        if not isinstance(e, dict):
            continue
        ev = e.get("evidence")
        if isinstance(ev, str) and ev.strip() and _norm_ws(ev) not in hay:
            errs.append(f"entities[{str(e.get('slug'))[:60]!r}].evidence n'est pas une"
                        " citation litterale du document : recopier un passage exact")
    return errs[:20]


def secret_errors(extraction: dict) -> list[str]:
    _, counts = wp.redact(json.dumps(extraction, ensure_ascii=False))
    if counts:
        families = ", ".join(sorted(counts))
        return [f"la reponse contient une valeur ressemblant a un secret ({families}) :"
                " ne jamais reproduire de secret"]
    return []


# ------------------------------------------------------------- notifications
def _write_notify(emoji: str, text: str, persist: int = 1) -> None:
    """Depot dans le spool draine par llm_wiki_poll (root) : le worker n'a jamais le
    jeton Telegram. Texte borne, caviarde, sans extrait du document."""
    NOTIFY_SPOOL.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"emoji": emoji, "text": wp.redact(text)[0][:1500], "persist": persist},
                      ensure_ascii=False)
    name = f"{int(time.time())}-opencode-{os.getpid()}-{time.monotonic_ns() % 10**6}.json"
    tmp = NOTIFY_SPOOL / f".tmp.{name}"
    tmp.write_text(body, encoding="utf-8")
    tmp.chmod(0o640)
    tmp.replace(NOTIFY_SPOOL / name)


def _state(name: str) -> dict:
    try:
        return json.loads((STATE_DIR / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(name: str, data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_DIR / f".{name}.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_DIR / name)


def notify_job_failed(job: dict, attempts: int, cause: str) -> bool:
    """UNE notification par mise en quarantaine (deduplication persistante).

    Cle = job + evenement de quarantaine : une meme quarantaine n'alerte qu'une fois,
    mais un nouvel echec apres remise en file par l'admin alerte de nouveau.
    """
    seen = _state("notified.json")
    quarantines = [e["id"] for e in wj.events(job["job_id"], 20)
                   if e["to_status"] == wj.ALT_QUARANTINED]
    key = f"{job['job_id']}:{quarantines[0] if quarantines else 0}"
    if key in seen:
        return False
    src = str(job.get("source") or "").replace("\\", "/").rsplit("/", 1)[-1][:80]
    _write_notify("FAIL", "LLM Wiki fallback OpenCode en échec\n"
                  f"job: {job['job_id'][:8]}\nsource: {src}\nmodel: {MODEL_ID}\n"
                  f"attempts: {attempts}\ncause: {cause[:160]}")
    seen[key] = int(time.time())
    _save_state("notified.json", seen)
    return True


def provider_down(kind: str, detail: str) -> int:
    """Backoff global exponentiel ; alerte globale unique par periode de cooldown."""
    st = _state("provider.json")
    n = int(st.get("failures", 0)) + 1
    backoff = min(BACKOFF_MAX_S, BACKOFF_BASE_S * 2 ** (n - 1))
    now = int(time.time())
    st.update({"failures": n, "until": now + backoff, "last_kind": kind,
               "last_detail": detail[:200], "last_at": now})
    if n >= GLOBAL_ALERT_AFTER and now - int(st.get("alerted_at", 0)) > GLOBAL_ALERT_COOLDOWN_S:
        _write_notify("WARN", "LLM Wiki fallback OpenCode : provider indisponible\n"
                      f"model: {MODEL_ID}\ncause: {kind}\necheces consecutifs: {n}\n"
                      f"prochain essai dans {backoff // 60} min")
        st["alerted_at"] = now
    _save_state("provider.json", st)
    return backoff


def provider_ok() -> None:
    st = _state("provider.json")
    if st.get("failures"):
        st.update({"failures": 0, "until": 0})
        _save_state("provider.json", st)


# ------------------------------------------------------------------ un job
def _contract_artifact(contract: dict) -> dict:
    return {k: contract[k] for k in ("contract_version", "contract_digest", "json_schema",
                                     "server_rules", "instructions")}


def process_job(job: dict, workdir: Path) -> str:
    """Traite UN job loue. Rend l'issue ; laisse remonter ProviderError."""
    jid, lid, fen = job["job_id"], job["lease_id"], int(job["fencing_token"])
    doc = wj.read_job(jid, lid)
    try:
        # Meme source canonique que wiki_ingest_contract, rechargee pour CE job.
        contract = wj.contract(str(doc["contract_version"]))
    except wj.WikiJobsError as exc:
        raise ProviderError("contract", str(exc)[:200]) from exc
    if len({contract["contract_digest"], job["contract_digest"], doc["contract_digest"]}) != 1:
        wj.release_alternate(jid, lid, fen, "contrat change pendant le traitement",
                             provider=False)
        return "released-contract-changed"
    proj = wp.project(str(doc["chunk_markdown"]), job_id=jid, source=str(doc["source"]),
                      source_hash=str(doc["source_hash"]), chunk_hash=str(doc["chunk_hash"]),
                      chunk_index=int(doc["chunk_index"]), chunk_count=int(doc["chunk_count"]))
    doc_file = workdir / "document.md"
    doc_file.write_text(proj.text, encoding="utf-8")
    contract_file = workdir / "contrat.json"
    contract_file.write_text(json.dumps(_contract_artifact(contract), ensure_ascii=False,
                                        indent=1), encoding="utf-8")
    log(f"job={jid[:8]} projection={proj.sha256[:12]} octets={len(proj.text)}"
        f" caviardages={sum(proj.redactions.values())} blocs={proj.code_blocks}"
        f" essais_passes={job.get('attempts_alternate', 0)}")
    previous: list[str] = []
    while True:
        files = [doc_file, contract_file]
        message = MESSAGE_FIRST
        if previous:
            err_file = workdir / "erreurs.txt"
            err_file.write_text("\n".join(f"- {e}" for e in previous), encoding="utf-8")
            files.append(err_file)
            message = MESSAGE_RETRY
        t0 = time.monotonic()
        try:
            text = run_provider(message, files, f"wiki-{jid[:8]}", workdir)
            provider_ok()  # le provider a repondu, quel que soit le contenu
            extraction = parse_extraction(text)
            errs = evidence_errors(extraction, proj.text) + secret_errors(extraction)
            if not errs:
                chk = wj.check_extraction(jid, lid, fen, extraction)
                errs = [] if chk["ok"] else [str(e) for e in chk["errors"]]
            if errs:
                raise ModelOutputError(errs)  # noqa: TRY301
        except ModelOutputError as exc:
            r = wj.record_alternate_failure(jid, lid, fen, exc.errors)
            log(f"job={jid[:8]} essai={r['attempts_alternate']} invalide"
                f" ms={int((time.monotonic() - t0) * 1000)} : {'; '.join(exc.errors)[:200]}")
            if r["status"] == wj.ALT_QUARANTINED:
                notify_job_failed(doc, int(r["attempts_alternate"]), exc.errors[0])
                return "quarantined"
            previous = exc.errors
            continue
        try:
            res = wj.submit(jid, lid, fen, str(doc["contract_version"]), extraction,
                            job["contract_digest"], model=model_courant())
        except wj.WikiJobsError as exc:
            # Refus au submit malgre la validation a blanc (course sur un slug) :
            # submit a deja compte la tentative dans la route alternative.
            log(f"job={jid[:8]} submit refuse : {str(exc)[:200]}")
            last = {e["to_status"] for e in wj.events(jid, 1)}
            if wj.ALT_QUARANTINED in last:
                notify_job_failed(doc, wj.MAX_ALT_ATTEMPTS, str(exc))
            return "submit-refused"
        log(f"job={jid[:8]} soumis receipt={res['receipt_id']} duplicate={res['duplicate']}"
            f" model={model_courant()} ms={int((time.monotonic() - t0) * 1000)}")
        return "submitted"


# ------------------------------------------------------------------- un lot
def _request_note_merge() -> None:
    with contextlib.suppress(OSError):
        INGEST_REQUEST.write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + "\n",
                                  encoding="utf-8")


def run_batch(batch: int, max_seconds: int) -> dict[str, int]:
    stats = {"claimed": 0, "submitted": 0, "quarantined": 0, "provider": 0, "other": 0}
    st = _state("provider.json")
    if int(st.get("until", 0)) > time.time():
        log(f"provider en backoff jusqu'a {int(st['until'])} ({st.get('last_kind')}), rien a faire")
        return stats
    (STATE_DIR / "runs").mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    for _ in range(max(1, batch)):
        if time.monotonic() - t0 > max_seconds:
            break
        try:
            job = wj.claim_alternate(lease_seconds=LEASE_S)
        except wj.WikiJobsError as exc:  # contrat indisponible : rien n'est loue
            log(f"claim refuse : {str(exc)[:200]}")
            break
        if job is None:
            break
        stats["claimed"] += 1
        with tempfile.TemporaryDirectory(prefix="run-", dir=STATE_DIR / "runs") as tmp:
            try:
                outcome = process_job(job, Path(tmp))
            except wj.WikiJobsError as exc:
                # Bail perdu/expire en cours de route : le job revient seul par
                # claim_alternate ; jamais d'ecriture hors bail.
                log(f"job={job['job_id'][:8]} abandonne : {str(exc)[:200]}")
                outcome = "error"
            except ProviderError as exc:
                backoff = provider_down(exc.kind, exc.detail)
                r = wj.release_alternate(job["job_id"], job["lease_id"], job["fencing_token"],
                                         f"{exc.kind}: {exc.detail}", backoff_s=backoff,
                                         provider=True,
                                         max_provider_failures=MAX_PROVIDER_FAILURES_PER_JOB)
                log(f"job={job['job_id'][:8]} provider {exc.kind} : {exc.detail[:200]}"
                    f" -> {r['status']}, backoff global {backoff}s, lot interrompu")
                if r["status"] == wj.ALT_QUARANTINED:
                    notify_job_failed(job, int(r["provider_failures"]),
                                      f"pannes provider repetees : {exc.kind}")
                stats["provider"] += 1
                break
        if outcome == "submitted":
            stats["submitted"] += 1
        elif outcome == "quarantined":
            stats["quarantined"] += 1
        else:
            stats["other"] += 1
    if stats["submitted"]:
        res = wj.merge_pending(limit=20, max_ms=10000)
        if int(res.get("merged") or 0) > 0:
            _request_note_merge()
        log(f"merge_pending merged={res.get('merged')} failed={res.get('failed')}")
    return stats


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="wiki_alternate", description=__doc__.split("\n")[0])
    p.add_argument("--batch", type=int, default=3)
    p.add_argument("--max-seconds", type=int, default=2400)
    a = p.parse_args(argv)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / "worker.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log("un autre worker tourne, sortie")
            return 0
        stats = run_batch(a.batch, a.max_seconds)
    log("lot termine " + json.dumps(stats, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
