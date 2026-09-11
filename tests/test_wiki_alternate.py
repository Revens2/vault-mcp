"""Route alternative (worker local) et administration de la file Wiki.

Couvre : migration additive du schema, release(action="alternate"),
claim_alternate atomique, read/submit sur un bail alternatif, budget propre
(attempts_alternate), panne provider avec backoff, requeue/terminal_skip
controles et journal wiki_job_events. Aucun appel reseau, aucun LLM.
"""

from __future__ import annotations

import importlib
import json
import sqlite3
import threading
from pathlib import Path

import pytest

FACTICE = Path(__file__).parent / "fixtures" / "contrat_wiki_factice.py"

COURT = "# Court\n\nun seul chunk."
DOC = ("# Titre\n\nParagraphe de contenu deterministe. " * 40)

CLES_HISTORIQUES = (
    "contract_version", "tokenizer", "pending_docs", "pending_chunks", "leased",
    "leased_expired", "submitted_spooled", "spool_files", "merged", "failed",
    "deferred", "quarantined", "last_merge_at", "errors_compact",
)
CLES_ALTERNATIVES = (
    "alternate_pending", "alternate_leased", "alternate_completed",
    "alternate_quarantined", "terminal_skip", "last_alternate_success_at",
    "last_alternate_error",
)


def _valid_extraction(slug="doc-test", title="Doc test"):
    body = ("Ceci est un corps de fiche largement suffisant pour depasser les "
            "deux cents caracteres exiges par la validation serveur. " * 4)
    return {
        "language": "fr",
        "confidence": 0.8,
        "note": {
            "slug": slug,
            "title": title,
            "tags": ["test", "wiki"],
            "doc_date": "",
            "summary": "Resume de test.",
            "sections": [
                {"heading": "Resume", "markdown": body + " Voir {{E:ent-test}}."},
            ],
            "warnings": [],
        },
        "entities": [
            {"slug": "ent-test", "name": "Ent Test", "kind": "entity",
             "subtype": "systeme", "aliases": [], "tags": ["test"],
             "definition": "Entite de test.",
             "evidence": "mention dans le document",
             "salience": "primary"},
        ],
        "relations": [],
        "issues": [],
    }


def _invalide():
    doc = _valid_extraction()
    doc["note"]["tags"] = []  # invalide : tags hors [2-8]
    return doc


@pytest.fixture
def wj(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WIKI_JOBS_DB", str(tmp_path / "wiki_jobs.db"))
    monkeypatch.setenv("WIKI_JOBS_SPOOL", str(tmp_path / "spool"))
    monkeypatch.setenv("WIKI_JOBS_MANIFEST", str(tmp_path / "manifest.jsonl"))
    monkeypatch.setenv("WIKI_RAW_DIR", str(tmp_path / "raw"))
    monkeypatch.setenv("WIKI_EXCLUDE_DIRS",
                       str(tmp_path / "raw" / "assets" / "ConvIA").replace("\\", "/")
                       + ":/srv/vault-mirror/raw/assets/ConvIA")
    monkeypatch.setenv("WIKI_CHUNK_MIN_TOKENS", "40")
    monkeypatch.setenv("WIKI_CHUNK_MIN_FLOOR", "8")
    monkeypatch.setenv("WIKI_CONTRACT_MODULE", str(FACTICE))
    monkeypatch.setenv("WIKI_NOTES_DIR", str(tmp_path / "wiki"))
    # Jamais les vrais marqueurs : ils reveilleraient la fusion / le worker en prod.
    monkeypatch.setenv("WIKI_INGEST_REQUEST", str(tmp_path / "wiki-ingest.request"))
    monkeypatch.setenv("WIKI_ALTERNATE_REQUEST", str(tmp_path / "wiki-alternate.request"))
    (tmp_path / "wiki" / "sources").mkdir(parents=True)
    from vault_mcp import wiki_jobs
    importlib.reload(wiki_jobs)
    assert str(wiki_jobs.ALTERNATE_REQUEST).startswith(str(tmp_path))
    return wiki_jobs


# ------------------------------------------------------------------ helpers
def _row(wj, job_id):
    conn = wj.connect()
    try:
        return dict(conn.execute("SELECT * FROM wiki_jobs WHERE job_id=?", (job_id,)).fetchone())
    finally:
        conn.close()


def _nb_lignes(wj):
    conn = wj.connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM wiki_jobs").fetchone()[0]
    finally:
        conn.close()


def _sql(wj, requete, args=()):
    conn = wj.connect()
    try:
        conn.execute(requete, args)
        conn.commit()
    finally:
        conn.close()


def _expirer(wj, job_id):
    _sql(wj, "UPDATE wiki_jobs SET expires_at=1 WHERE job_id=?", (job_id,))


def _evenements(wj, job_id):
    """Journal chronologique (events() rend le plus recent d'abord)."""
    return list(reversed(wj.events(job_id)))


def _noms(wj, job_id):
    return [e["event"] for e in _evenements(wj, job_id)]


def _claim_un(wj, source="raw/a.md", sha="a" * 64, contenu=COURT):
    wj.sync_source(source, sha, contenu)
    return wj.claim(limit=1)["jobs"][0]


def _route_alternate(wj, source="raw/a.md", sha="a" * 64, contenu=COURT,
                     raison="SKIPPED_SAFETY plateforme"):
    job = _claim_un(wj, source, sha, contenu)
    wj.release(job["job_id"], job["lease_id"], "alternate", reason=raison)
    return job


def _alt(wj, **kw):
    """Job passe en route alternative puis reserve par le worker."""
    _route_alternate(wj, **kw)
    got = wj.claim_alternate()
    assert got is not None
    return got


def _soumettre(wj, job, doc=None, model="opencode/modele-test"):
    return wj.submit(job["job_id"], job["lease_id"], job["fencing_token"],
                     wj.CONTRACT_VERSION, doc or _valid_extraction(), model=model)


def _quarantaine(wj, source="raw/a.md", sha="a" * 64, raison="timeout plateforme"):
    """Quarantaine ChatGPT par le chemin reel : 3 defer consecutifs."""
    wj.sync_source(source, sha, COURT)
    jid = wj.chunk_source(source, sha, COURT)[0]["job_id"]
    for _ in range(wj.MAX_ATTEMPTS):
        job = wj.claim(limit=1)["jobs"][0]
        assert job["job_id"] == jid
        wj.release(jid, job["lease_id"], "defer", reason=raison)
    assert _row(wj, jid)["status"] == "quarantined"
    return jid


def _quarantaine_alternative(wj, source="raw/a.md", sha="a" * 64):
    job = _alt(wj, source=source, sha=sha)
    for _ in range(wj.MAX_ALT_ATTEMPTS):
        wj.record_alternate_failure(job["job_id"], job["lease_id"], job["fencing_token"],
                                    ["JSON invalide"])
    assert _row(wj, job["job_id"])["status"] == wj.ALT_QUARANTINED
    return job["job_id"]


# ------------------------------------------------------------------ migration
_ANCIEN_SCHEMA = """
CREATE TABLE wiki_jobs (
    job_id           TEXT PRIMARY KEY,
    source           TEXT NOT NULL,
    source_hash      TEXT NOT NULL,
    chunk_index      INTEGER NOT NULL,
    chunk_count      INTEGER NOT NULL,
    chunk_hash       TEXT NOT NULL,
    chunk_bytes      INTEGER NOT NULL,
    chunk_text       TEXT NOT NULL,
    contract_version TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    lease_id         TEXT,
    fencing_token    INTEGER NOT NULL DEFAULT 0,
    expires_at       INTEGER,
    attempts         INTEGER NOT NULL DEFAULT 0,
    renews           INTEGER NOT NULL DEFAULT 0,
    payload_hash     TEXT,
    receipt_id       TEXT,
    first_seen       TEXT NOT NULL,
    last_error       TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    merged_at        TEXT,
    UNIQUE (source_hash, chunk_hash, chunk_index, contract_version)
);
"""
_ANCIEN_JOB = "0123456789abcdef01234567"


def _creer_ancienne_base(wj):
    wj.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(wj.DB_PATH)
    try:
        conn.executescript(_ANCIEN_SCHEMA)
        conn.execute(
            "INSERT INTO wiki_jobs (job_id, source, source_hash, chunk_index, chunk_count,"
            " chunk_hash, chunk_bytes, chunk_text, contract_version, status, attempts,"
            " last_error, first_seen, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_ANCIEN_JOB, "raw/ancien.md", "c" * 64, 0, 1, "d" * 64, len(COURT), COURT,
             wj.CONTRACT_VERSION, "pending", 1, "invalid: ancienne erreur",
             "2026-09-01T00:00:00Z", "2026-09-01T00:00:00Z", "2026-09-01T00:00:00Z"))
        conn.commit()
    finally:
        conn.close()


def _colonnes(conn):
    return [r[1] for r in conn.execute("PRAGMA table_info(wiki_jobs)")]


def test_migration_ancienne_base_colonnes_ajoutees_ligne_preservee(wj):
    _creer_ancienne_base(wj)
    conn = wj.connect()
    try:
        cols = _colonnes(conn)
        for nom, _ in wj._ADDED_COLUMNS:
            assert nom in cols, nom
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "wiki_job_events" in tables
        row = dict(conn.execute("SELECT * FROM wiki_jobs WHERE job_id=?",
                                (_ANCIEN_JOB,)).fetchone())
    finally:
        conn.close()
    # Ligne d'origine intacte, nouvelles colonnes a leur valeur par defaut.
    assert row["source"] == "raw/ancien.md" and row["chunk_text"] == COURT
    assert row["status"] == "pending" and row["attempts"] == 1
    assert row["last_error"] == "invalid: ancienne erreur"
    assert row["attempts_alternate"] == 0 and row["provider_failures"] == 0
    assert row["route"] is None and row["alt_next_at"] is None and row["model"] is None


def test_migration_idempotente_deuxieme_connect(wj):
    _creer_ancienne_base(wj)
    wj.connect().close()
    conn = wj.connect()  # 2e passage : aucune colonne en double, aucune erreur
    try:
        cols = _colonnes(conn)
        assert len(cols) == len(set(cols))
        n = conn.execute("SELECT COUNT(*) FROM wiki_jobs").fetchone()[0]
    finally:
        conn.close()
    assert n == 1


def test_migration_concurrente_colonne_deja_ajoutee_sans_erreur(wj):
    # Deux connexions migrent en meme temps : la seconde voit un PRAGMA perime et
    # tente d'ajouter une colonne deja creee -> "duplicate column" doit etre absorbe.
    _creer_ancienne_base(wj)
    wj.connect().close()
    brute = sqlite3.connect(wj.DB_PATH)

    class _VuePerimee:
        def execute(self, sql, *args):
            if sql.startswith("PRAGMA table_info"):
                return [(0, "job_id"), (1, "source")]
            return brute.execute(sql, *args)

        def commit(self):
            brute.commit()

    try:
        wj._migrate(_VuePerimee())
    finally:
        brute.close()


def test_ancienne_ligne_migree_passe_par_la_route_alternative(wj):
    _creer_ancienne_base(wj)
    job = wj.claim(limit=1)["jobs"][0]
    assert job["job_id"] == _ANCIEN_JOB
    wj.release(job["job_id"], job["lease_id"], "alternate", reason="blocage")
    alt = wj.claim_alternate()
    assert alt is not None and alt["job_id"] == _ANCIEN_JOB


# ------------------------------------------------------- release(alternate)
def test_release_alternate_bascule_sans_consommer_de_tentative(wj):
    assert not wj.ALTERNATE_REQUEST.exists()
    job = _claim_un(wj)
    res = wj.release(job["job_id"], job["lease_id"], "alternate",
                     reason="SKIPPED_SAFETY plateforme")
    assert res["status"] == wj.ALT_PENDING and res["attempts"] == 0
    row = _row(wj, job["job_id"])
    assert row["status"] == wj.ALT_PENDING
    assert row["attempts"] == 0  # budget ChatGPT intact
    assert row["lease_id"] is None and row["expires_at"] is None  # bail ferme
    assert row["route"] == "alternate"
    assert row["alt_reason"] == "SKIPPED_SAFETY plateforme"
    assert wj.ALTERNATE_REQUEST.exists()  # marqueur de reveil depose
    ev = _evenements(wj, job["job_id"])
    assert [e["event"] for e in ev] == ["route-alternate"]
    assert ev[0]["from_status"] == "leased" and ev[0]["to_status"] == wj.ALT_PENDING
    assert ev[0]["detail"] == "SKIPPED_SAFETY plateforme"


def test_job_alternate_invisible_pour_claim(wj):
    job = _route_alternate(wj)
    assert wj.claim(limit=10)["leased"] == 0
    # Meme bail alternatif expire : claim() ne le voit jamais.
    alt = wj.claim_alternate()
    _expirer(wj, alt["job_id"])
    assert wj.claim(limit=10)["leased"] == 0
    assert _row(wj, job["job_id"])["status"] == wj.ALT_LEASED


def test_release_classique_refuse_sur_un_bail_alternatif(wj):
    alt = _alt(wj)
    for action in ("release", "defer", "renew", "alternate"):
        with pytest.raises(wj.WikiJobsError, match="non loue"):
            wj.release(alt["job_id"], alt["lease_id"], action)
    assert _row(wj, alt["job_id"])["status"] == wj.ALT_LEASED


# ------------------------------------------------------------ claim_alternate
def test_claim_alternate_vide_rend_none(wj):
    assert wj.claim_alternate() is None
    wj.sync_source("raw/a.md", "a" * 64, COURT)  # job pending ChatGPT : pas eligible
    assert wj.claim_alternate() is None


def test_claim_alternate_champs_et_fencing(wj):
    job = _route_alternate(wj)
    alt = wj.claim_alternate(actor="opencode-test")
    assert alt["job_id"] == job["job_id"]
    assert alt["fencing_token"] == job["fencing_token"] + 1
    assert alt["lease_id"] and alt["lease_id"] != job["lease_id"]
    assert alt["contract_version"] == wj.CONTRACT_VERSION
    assert alt["contract_digest"] == wj.contract(wj.CONTRACT_VERSION)["contract_digest"]
    assert alt["attempts_alternate"] == 0
    assert alt["alt_reason"] == "SKIPPED_SAFETY plateforme"
    assert _row(wj, job["job_id"])["status"] == wj.ALT_LEASED
    assert wj.claim_alternate() is None  # deja loue, bail valide


def test_claim_alternate_concurrent_un_seul_gagnant(wj):
    _route_alternate(wj)
    barriere = threading.Barrier(2)
    resultats: list[object] = []
    erreurs: list[BaseException] = []

    def worker():
        barriere.wait()
        try:
            resultats.append(wj.claim_alternate())
        except Exception as exc:  # remonte dans l'assertion
            erreurs.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not erreurs
    gagnants = [r for r in resultats if r is not None]
    assert len(gagnants) == 1 and resultats.count(None) == 1


def test_claim_alternate_concurrent_plusieurs_jobs_sans_chevauchement(wj):
    for i, src in enumerate(("raw/a.md", "raw/b.md", "raw/c.md")):
        _route_alternate(wj, source=src, sha=str(i) * 64)
    barriere = threading.Barrier(4)
    obtenus: list[str] = []
    verrou = threading.Lock()

    def worker():
        barriere.wait()
        while True:
            got = wj.claim_alternate()
            if got is None:
                return
            with verrou:
                obtenus.append(got["job_id"])

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(obtenus) == 3 and len(set(obtenus)) == 3  # aucun job attribue deux fois


# ------------------------------------------------------------------ read/submit
def test_read_job_sur_bail_alternatif(wj):
    alt = _alt(wj)
    vue = wj.read_job(alt["job_id"], alt["lease_id"])
    assert vue["chunk_markdown"] == COURT
    assert vue["fencing_token"] == alt["fencing_token"]
    assert "DONNEE" in vue["data_notice"]


def test_submit_valide_depuis_alternate_leased(wj):
    alt = _alt(wj)
    recu = _soumettre(wj, alt, model="opencode/qwen-local")
    assert recu["receipt_id"] and recu["duplicate"] is False
    row = _row(wj, alt["job_id"])
    assert row["status"] == "submitted" and row["model"] == "opencode/qwen-local"
    assert row["attempts"] == 0 and row["attempts_alternate"] == 0
    env = json.loads(wj._spool_path(alt["source_hash"], alt["chunk_index"])
                     .read_text(encoding="utf-8"))
    assert env["extraction"]["model"] == "opencode/qwen-local"
    assert env["note"]["slug"] == "doc-test"
    assert _noms(wj, alt["job_id"]) == ["route-alternate", "alternate-submitted"]
    # Rejeu identique (reponse perdue) : meme recu, aucun nouvel effet.
    r2 = _soumettre(wj, alt, model="opencode/qwen-local")
    assert r2["duplicate"] is True and r2["receipt_id"] == recu["receipt_id"]
    assert wj.status()["spool_files"] == 1
    assert _noms(wj, alt["job_id"]).count("alternate-submitted") == 1


def test_submit_route_chatgpt_trace_le_modele_par_defaut(wj):
    job = _claim_un(wj)
    wj.submit(job["job_id"], job["lease_id"], job["fencing_token"],
              wj.CONTRACT_VERSION, _valid_extraction())
    assert _row(wj, job["job_id"])["model"] == "chatgpt"
    env = json.loads(wj._spool_path(job["source_hash"], 0).read_text(encoding="utf-8"))
    assert env["extraction"]["model"] == "chatgpt"
    assert "alternate-submitted" not in _noms(wj, job["job_id"])


def test_ancien_fencing_refuse_apres_reclaim_d_un_bail_alternatif_expire(wj):
    vieux = _alt(wj)
    _expirer(wj, vieux["job_id"])
    neuf = wj.claim_alternate()
    assert neuf["job_id"] == vieux["job_id"]
    assert neuf["fencing_token"] == vieux["fencing_token"] + 1
    assert "alternate-lease-expired" in _noms(wj, vieux["job_id"])
    with pytest.raises(wj.WikiJobsError, match="lease-invalid"):
        _soumettre(wj, vieux)
    with pytest.raises(wj.WikiJobsError, match="fencing"):
        wj.submit(neuf["job_id"], neuf["lease_id"], vieux["fencing_token"],
                  wj.CONTRACT_VERSION, _valid_extraction(), model="x")
    with pytest.raises(wj.WikiJobsError, match="fencing"):
        wj.check_extraction(neuf["job_id"], neuf["lease_id"], vieux["fencing_token"],
                            _valid_extraction())
    with pytest.raises(wj.WikiJobsError, match="lease-invalid"):
        wj.record_alternate_failure(vieux["job_id"], vieux["lease_id"],
                                    vieux["fencing_token"], ["x"])
    assert _row(wj, neuf["job_id"])["attempts_alternate"] == 0
    assert _soumettre(wj, neuf)["duplicate"] is False


def test_read_bail_alternatif_expire_revient_en_alternate_pending(wj):
    alt = _alt(wj)
    _expirer(wj, alt["job_id"])
    with pytest.raises(wj.WikiJobsError, match="lease-expired"):
        wj.read_job(alt["job_id"], alt["lease_id"])
    row = _row(wj, alt["job_id"])
    assert row["status"] == wj.ALT_PENDING  # jamais `pending` (file ChatGPT)
    assert row["lease_id"] is None
    assert wj.claim(limit=10)["leased"] == 0
    assert wj.claim_alternate()["job_id"] == alt["job_id"]


def test_submit_bail_alternatif_expire_revient_en_alternate_pending(wj):
    alt = _alt(wj)
    _expirer(wj, alt["job_id"])
    with pytest.raises(wj.WikiJobsError, match="lease-expired"):
        _soumettre(wj, alt)
    row = _row(wj, alt["job_id"])
    assert row["status"] == wj.ALT_PENDING
    assert row["attempts"] == 0 and row["attempts_alternate"] == 0
    assert wj.claim(limit=10)["leased"] == 0
    assert wj.status()["spool_files"] == 0


def test_submit_invalide_en_route_alternative_budget_propre(wj):
    alt = _alt(wj)
    with pytest.raises(wj.WikiJobsError, match="validation refusee"):
        _soumettre(wj, alt, doc=_invalide())
    row = _row(wj, alt["job_id"])
    assert row["status"] == wj.ALT_PENDING
    assert row["attempts_alternate"] == 1
    assert row["attempts"] == 0  # budget ChatGPT jamais consomme
    assert row["lease_id"] is None and row["expires_at"] is None
    assert row["last_error"].startswith("alternate:")
    assert "alternate-invalid" in _noms(wj, alt["job_id"])
    assert wj.claim(limit=10)["leased"] == 0  # jamais renvoye dans la file ChatGPT
    assert wj.status()["spool_files"] == 0


def test_trois_submit_invalides_alternate_quarantined(wj):
    job = _route_alternate(wj)
    for i in range(wj.MAX_ALT_ATTEMPTS):
        alt = wj.claim_alternate()
        assert alt is not None and alt["attempts_alternate"] == i
        with pytest.raises(wj.WikiJobsError, match="validation refusee"):
            _soumettre(wj, alt, doc=_invalide())
    row = _row(wj, job["job_id"])
    assert row["status"] == wj.ALT_QUARANTINED
    assert row["attempts_alternate"] == wj.MAX_ALT_ATTEMPTS and row["attempts"] == 0
    assert wj.claim_alternate() is None
    assert wj.claim(limit=10)["leased"] == 0
    st = wj.status()
    assert st["alternate_quarantined"] == 1
    assert "alternate-invalid" in st["last_alternate_error"]


# ------------------------------------------------------------ check_extraction
def test_check_extraction_valide_et_invalide_sans_effet(wj):
    alt = _alt(wj)
    avant = _row(wj, alt["job_id"])
    ev_avant = wj.events(alt["job_id"])
    ok = wj.check_extraction(alt["job_id"], alt["lease_id"], alt["fencing_token"],
                             _valid_extraction())
    assert ok["ok"] is True and ok["errors"] == []
    ko = wj.check_extraction(alt["job_id"], alt["lease_id"], alt["fencing_token"], _invalide())
    assert ko["ok"] is False and any("note.tags" in e for e in ko["errors"])
    brut = wj.check_extraction(alt["job_id"], alt["lease_id"], alt["fencing_token"], "pas un dict")
    assert brut["ok"] is False
    assert _row(wj, alt["job_id"]) == avant  # aucune colonne touchee
    assert wj.events(alt["job_id"]) == ev_avant
    assert wj.status()["spool_files"] == 0


def test_check_extraction_signale_une_collision_de_slug(wj):
    a = _claim_un(wj, "raw/a.md", "a" * 64)
    wj.submit(a["job_id"], a["lease_id"], a["fencing_token"], wj.CONTRACT_VERSION,
              _valid_extraction(slug="meme-slug"))
    alt = _alt(wj, source="raw/b.md", sha="b" * 64)
    res = wj.check_extraction(alt["job_id"], alt["lease_id"], alt["fencing_token"],
                              _valid_extraction(slug="meme-slug"))
    assert res["ok"] is False and "deja porte" in res["errors"][0]
    assert _row(wj, alt["job_id"])["attempts_alternate"] == 0


def test_check_extraction_refuse_un_bail_chatgpt(wj):
    job = _claim_un(wj)
    with pytest.raises(wj.WikiJobsError, match="lease-invalid"):
        wj.check_extraction(job["job_id"], job["lease_id"], job["fencing_token"],
                            _valid_extraction())


# ---------------------------------------------------- record_alternate_failure
def test_record_alternate_failure_persiste_puis_quarantaine(wj):
    alt = _alt(wj)
    args = (alt["job_id"], alt["lease_id"], alt["fencing_token"])
    r1 = wj.record_alternate_failure(*args, ["JSON invalide"])
    assert r1 == {"status": wj.ALT_LEASED, "attempts_alternate": 1, "job_id": alt["job_id"]}
    row = _row(wj, alt["job_id"])
    assert row["status"] == wj.ALT_LEASED and row["attempts_alternate"] == 1  # persiste
    assert row["lease_id"] == alt["lease_id"] and row["attempts"] == 0
    assert wj.read_job(alt["job_id"], alt["lease_id"])["chunk_markdown"]  # bail toujours valide
    wj.record_alternate_failure(*args, ["contrat refuse"])
    r3 = wj.record_alternate_failure(*args, ["toujours invalide"])
    assert r3["status"] == wj.ALT_QUARANTINED and r3["attempts_alternate"] == 3
    row = _row(wj, alt["job_id"])
    assert row["status"] == wj.ALT_QUARANTINED
    assert row["lease_id"] is None and row["expires_at"] is None  # bail ferme
    assert row["attempts"] == 0
    assert _noms(wj, alt["job_id"]).count("alternate-invalid") == 2
    assert _noms(wj, alt["job_id"])[-1] == "alternate-quarantined"
    with pytest.raises(wj.WikiJobsError, match="lease-invalid"):
        wj.record_alternate_failure(*args, ["encore"])
    assert wj.claim_alternate() is None


# ------------------------------------------------------------ release_alternate
def test_release_alternate_provider_backoff(wj, monkeypatch):
    alt = _alt(wj)
    wj.record_alternate_failure(alt["job_id"], alt["lease_id"], alt["fencing_token"], ["x"])
    base = wj._now_ts()
    monkeypatch.setattr(wj, "_now_ts", lambda: base)
    res = wj.release_alternate(alt["job_id"], alt["lease_id"], alt["fencing_token"],
                               "HTTP 503 provider", backoff_s=600, provider=True)
    assert res["status"] == wj.ALT_PENDING and res["alt_next_at"] == base + 600
    row = _row(wj, alt["job_id"])
    assert row["status"] == wj.ALT_PENDING
    assert row["provider_failures"] == 1
    assert row["attempts_alternate"] == 1  # panne provider : rien de consomme
    assert row["attempts"] == 0
    assert row["lease_id"] is None and row["alt_next_at"] == base + 600
    assert _noms(wj, alt["job_id"])[-1] == "alternate-provider"
    assert wj.claim_alternate() is None  # backoff non echu
    monkeypatch.setattr(wj, "_now_ts", lambda: base + 599)
    assert wj.claim_alternate() is None
    monkeypatch.setattr(wj, "_now_ts", lambda: base + 600)
    neuf = wj.claim_alternate()
    assert neuf is not None and neuf["job_id"] == alt["job_id"]
    assert "HTTP 503" in wj.status()["last_alternate_error"]


def test_release_alternate_arret_propre_sans_compter_de_panne(wj):
    alt = _alt(wj)
    res = wj.release_alternate(alt["job_id"], alt["lease_id"], alt["fencing_token"],
                               "arret du worker", provider=False)
    assert res["alt_next_at"] is None
    row = _row(wj, alt["job_id"])
    assert row["status"] == wj.ALT_PENDING and row["provider_failures"] == 0
    assert _noms(wj, alt["job_id"])[-1] == "alternate-release"
    assert wj.claim_alternate()["job_id"] == alt["job_id"]  # reeligible sans delai


# ------------------------------------------------------------------ merge
def test_merge_pending_fusionne_un_job_de_la_route_alternative(wj):
    alt = _alt(wj)
    _soumettre(wj, alt)
    res = wj.merge_pending()
    assert res["merged"] == 1
    assert _row(wj, alt["job_id"])["status"] == "merged"
    assert wj.status()["alternate_completed"] == 1
    assert wj.merge_pending()["merged"] == 0


def test_merge_pending_groupe_mixte_chatgpt_et_alternatif(wj):
    wj.sync_source("raw/a.md", "a" * 64, DOC)
    jobs = wj.claim(limit=10)["jobs"]
    assert len(jobs) > 1
    premier, reste = jobs[0], jobs[1:]
    wj.release(premier["job_id"], premier["lease_id"], "alternate", reason="blocage")
    for j in reste:
        wj.submit(j["job_id"], j["lease_id"], j["fencing_token"], wj.CONTRACT_VERSION,
                  _valid_extraction())
    assert wj.merge_pending()["merged"] == 0  # groupe incomplet : attente
    alt = wj.claim_alternate()
    _soumettre(wj, alt)
    assert wj.merge_pending()["merged"] == 1
    for j in jobs:
        assert _row(wj, j["job_id"])["status"] == "merged"


# ------------------------------------------------------------------ status
def test_status_cles_historiques_et_nouvelles(wj):
    st = wj.status()
    for cle in CLES_HISTORIQUES + CLES_ALTERNATIVES:
        assert cle in st, cle
    assert st["last_alternate_success_at"] is None and st["last_alternate_error"] is None
    _route_alternate(wj, "raw/a.md", "a" * 64)
    alt = _alt(wj, source="raw/b.md", sha="b" * 64)
    wj.sync_source("raw/c.md", "c" * 64, COURT)
    st = wj.status()
    assert st["alternate_pending"] == 1 and st["alternate_leased"] == 1
    assert st["pending_chunks"] == 1  # les jobs alternatifs ne gonflent pas la file ChatGPT
    assert st["leased"] == 0
    _soumettre(wj, alt)
    st = wj.status()
    assert st["alternate_completed"] == 1 and st["submitted_spooled"] == 1
    assert st["last_alternate_success_at"]
    assert st["terminal_skip"] == 0 and st["alternate_quarantined"] == 0


# ------------------------------------------------------------------ sync CAS
def test_sync_nouveau_hash_marque_stale_un_job_alternate_pending(wj):
    job = _route_alternate(wj)
    assert wj.sync_source("raw/a.md", "a" * 64, COURT)["stale"] == 0  # meme hash : rien
    assert _row(wj, job["job_id"])["status"] == wj.ALT_PENDING
    r = wj.sync_source("raw/a.md", "b" * 64, COURT + "\n\nversion 2")
    assert r["stale"] == 1
    row = _row(wj, job["job_id"])
    assert row["status"] == "deferred" and row["last_error"].startswith("stale")
    assert wj.claim_alternate() is None


def test_sync_nouveau_hash_invalide_le_bail_alternatif_en_cours(wj):
    alt = _alt(wj)
    assert wj.sync_source("raw/a.md", "b" * 64, COURT + "\n\nversion 2")["stale"] == 1
    with pytest.raises(wj.WikiJobsError, match="stale|lease-invalid"):
        _soumettre(wj, alt)
    assert _row(wj, alt["job_id"])["status"] != "submitted"
    assert wj.status()["spool_files"] == 0


# --------------------------------------------------------- bugs suspectes
# Regression : read_job verifiait l'expiration AVANT le statut.
def test_read_apres_submit_alternatif_bail_expire_ne_remet_pas_en_file(wj):
    alt = _alt(wj)
    _soumettre(wj, alt)
    _expirer(wj, alt["job_id"])
    with pytest.raises(wj.WikiJobsError):
        wj.read_job(alt["job_id"], alt["lease_id"])  # rejeu tardif d'un read
    assert _row(wj, alt["job_id"])["status"] == "submitted"


# Regression : route alternative epuisee puis requeue pending -> jamais de job fantome.
def test_retour_en_route_alternative_epuisee_quarantaine_visible(wj):
    jid = _quarantaine_alternative(wj)
    wj.requeue(job_ids=[jid], dry_run=False, cause="provider repare")
    job = wj.claim(limit=1)["jobs"][0]
    assert job["job_id"] == jid
    res = wj.release(jid, job["lease_id"], "alternate", reason="nouveau blocage plateforme")
    row = _row(wj, jid)
    # Budget alternatif deja epuise : quarantaine visible, pas de famine silencieuse.
    assert res["status"] == row["status"] == wj.ALT_QUARANTINED
    assert "epuisee" in row["last_error"]
    assert wj.claim_alternate() is None and wj.claim(limit=10)["leased"] == 0
    assert wj.status()["alternate_quarantined"] == 1


# ------------------------------------------------------------------ requeue
def test_requeue_filtre_obligatoire(wj):
    _quarantaine(wj)
    for kw in ({}, {"job_ids": []}, {"job_ids": None, "reason_like": ""}):
        with pytest.raises(wj.WikiJobsError, match="filtre obligatoire"):
            wj.requeue(**kw)
    with pytest.raises(wj.WikiJobsError, match="target inconnue"):
        wj.requeue(reason_like="timeout", target="ailleurs")


def test_requeue_dry_run_sans_effet(wj):
    jid = _quarantaine(wj)
    avant = _row(wj, jid)
    ev_avant = wj.events(jid)
    res = wj.requeue(reason_like="timeout")
    assert res["dry_run"] is True and res["count"] == 1
    assert res["jobs"][0]["job_id"] == jid and res["jobs"][0]["attempts"] == 3
    assert _row(wj, jid) == avant
    assert wj.events(jid) == ev_avant
    assert wj.claim(limit=10)["leased"] == 0


def test_requeue_filtre_par_raison(wj):
    a = _quarantaine(wj, "raw/a.md", "a" * 64, raison="timeout plateforme")
    b = _quarantaine(wj, "raw/b.md", "b" * 64, raison="contrat non expose")
    res = wj.requeue(reason_like="non expose")
    assert [j["job_id"] for j in res["jobs"]] == [b]
    res = wj.requeue(reason_like="timeout")
    assert [j["job_id"] for j in res["jobs"]] == [a]


def test_requeue_job_id_explicite_et_prefixe(wj):
    a = _quarantaine(wj, "raw/a.md", "a" * 64)
    _quarantaine(wj, "raw/b.md", "b" * 64)
    assert [j["job_id"] for j in wj.requeue(job_ids=[a])["jobs"]] == [a]
    assert [j["job_id"] for j in wj.requeue(job_ids=[a[:8]])["jobs"]] == [a]


@pytest.mark.parametrize("prefixe", ["abc", "0123456", "zzzzzzzz", "ABCDEF12",
                                     "0000000%", "01234567_", "' OR 1=1 --"])
def test_requeue_prefixe_invalide_refuse(wj, prefixe):
    _quarantaine(wj)
    with pytest.raises(wj.WikiJobsError, match="job_id invalide"):
        wj.requeue(job_ids=[prefixe])


def test_requeue_cause_obligatoire_hors_dry_run(wj):
    jid = _quarantaine(wj)
    for cause in ("", "   "):
        with pytest.raises(wj.WikiJobsError, match="cause obligatoire"):
            wj.requeue(job_ids=[jid], dry_run=False, cause=cause)
    assert _row(wj, jid)["status"] == "quarantined"


def test_requeue_remet_a_zero_et_journalise(wj):
    jid = _quarantaine(wj, raison="timeout plateforme")
    res = wj.requeue(job_ids=[jid], dry_run=False, cause="contrat expose le 2026-09-10")
    assert res["count"] == 1 and res["target"] == "pending"
    row = _row(wj, jid)
    assert row["status"] == "pending" and row["attempts"] == 0
    assert row["lease_id"] is None
    assert row["last_error"] == "requeue: contrat expose le 2026-09-10"
    ev = _evenements(wj, jid)[-1]
    assert ev["event"] == "requeue-pending" and ev["actor"] == "admin"
    assert ev["from_status"] == "quarantined" and ev["to_status"] == "pending"
    assert ev["attempts_before"] == 3  # ancien budget conserve dans le journal
    assert ev["last_error"] == "timeout plateforme"  # ancienne raison conservee
    assert ev["detail"] == "contrat expose le 2026-09-10"
    # Idempotence : plus quarantined, donc plus eligible.
    again = wj.requeue(job_ids=[jid], dry_run=False, cause="contrat expose le 2026-09-10")
    assert again["count"] == 0
    assert _noms(wj, jid).count("requeue-pending") == 1
    # Reclamable par la file ChatGPT.
    job = wj.claim(limit=10)["jobs"][0]
    assert job["job_id"] == jid


def test_requeue_ignore_les_jobs_non_quarantined(wj):
    job = _claim_un(wj)
    wj.sync_source("raw/b.md", "b" * 64, COURT)
    res = wj.requeue(job_ids=[job["job_id"]], dry_run=False, cause="x")
    assert res["count"] == 0
    assert _row(wj, job["job_id"])["status"] == "leased"
    assert wj.requeue(reason_like="%", dry_run=True)["count"] == 0  # rien en quarantaine


def test_requeue_limite_respectee(wj):
    ids = [_quarantaine(wj, f"raw/{c}.md", c * 64) for c in "abc"]
    res = wj.requeue(reason_like="timeout", limit=2, dry_run=False, cause="lot borne")
    assert res["count"] == 2
    statuts = sorted(_row(wj, j)["status"] for j in ids)
    assert statuts == ["pending", "pending", "quarantined"]


def test_requeue_source_modifiee_depuis_skipped(wj):
    jid = _quarantaine(wj, "raw/a.md", "a" * 64)
    wj.sync_source("raw/a.md", "b" * 64, COURT + "\n\nversion 2")
    for dry in (True, False):
        res = wj.requeue(job_ids=[jid], dry_run=dry, cause="repare")
        assert res["count"] == 0
        assert res["skipped"][0]["job_id"] == jid
        assert res["skipped"][0]["skip"] == "source modifiee depuis"
    assert _row(wj, jid)["status"] == "quarantined"
    assert "requeue-pending" not in _noms(wj, jid)


def test_requeue_target_alternate(wj):
    jid = _quarantaine(wj, raison="SKIPPED_SAFETY")
    assert not wj.ALTERNATE_REQUEST.exists()
    wj.requeue(job_ids=[jid], target="alternate")  # dry-run : pas de marqueur
    assert not wj.ALTERNATE_REQUEST.exists()
    res = wj.requeue(job_ids=[jid], target="alternate", dry_run=False, cause="route locale")
    assert res["count"] == 1 and res["target"] == wj.ALT_PENDING
    row = _row(wj, jid)
    assert row["status"] == wj.ALT_PENDING and row["route"] == "alternate"
    assert row["attempts_alternate"] == 0 and row["alt_reason"] == "SKIPPED_SAFETY"
    assert wj.ALTERNATE_REQUEST.exists()
    assert _evenements(wj, jid)[-1]["event"] == "requeue-alternate"
    assert wj.claim(limit=10)["leased"] == 0
    assert wj.claim_alternate()["job_id"] == jid


def test_requeue_alternate_quarantined_vers_la_route_alternative(wj):
    jid = _quarantaine_alternative(wj)
    assert wj.claim_alternate() is None
    res = wj.requeue(job_ids=[jid], target="alternate", dry_run=False, cause="modele change")
    assert res["count"] == 1
    assert _row(wj, jid)["attempts_alternate"] == 0
    assert wj.claim_alternate()["job_id"] == jid


def test_requeue_aucun_doublon_de_job_id(wj):
    jid = _quarantaine(wj)
    n = _nb_lignes(wj)
    res = wj.requeue(job_ids=[jid, jid[:8], jid[:12]], dry_run=False, cause="x")
    assert res["count"] == 1 and [j["job_id"] for j in res["jobs"]] == [jid]
    assert _nb_lignes(wj) == n
    assert [j["job_id"] for j in wj.claim(limit=10)["jobs"]] == [jid]


# ------------------------------------------------------------ terminal_skip
def test_terminal_skip_dry_run_sans_effet(wj):
    jid = _quarantaine(wj)
    avant = _row(wj, jid)
    res = wj.terminal_skip([jid], cause="source vide")
    assert res["dry_run"] is True and res["count"] == 1
    assert res["jobs"][0]["to"] == wj.TERMINAL_SKIP
    assert _row(wj, jid) == avant
    assert "terminal-skip" not in _noms(wj, jid)


def test_terminal_skip_garde_fous(wj):
    jid = _quarantaine(wj)
    with pytest.raises(wj.WikiJobsError, match="obligatoire"):
        wj.terminal_skip([], cause="x", dry_run=False)
    with pytest.raises(wj.WikiJobsError, match="cause obligatoire"):
        wj.terminal_skip([jid], cause=" ", dry_run=False)
    with pytest.raises(wj.WikiJobsError, match="job_id invalide"):
        wj.terminal_skip(["abc"], cause="x", dry_run=False)
    assert _row(wj, jid)["status"] == "quarantined"


def test_terminal_skip_effet_jamais_reclame(wj):
    a = _quarantaine(wj, "raw/a.md", "a" * 64)
    b = _quarantaine_alternative(wj, "raw/b.md", "b" * 64)
    res = wj.terminal_skip([a, b], cause="source vide", dry_run=False)
    assert res["count"] == 2
    for jid in (a, b):
        row = _row(wj, jid)
        assert row["status"] == wj.TERMINAL_SKIP
        assert row["last_error"] == "terminal_skip: source vide"
        ev = _evenements(wj, jid)[-1]
        assert ev["event"] == "terminal-skip" and ev["to_status"] == wj.TERMINAL_SKIP
        assert ev["detail"] == "source vide"
    assert wj.status()["terminal_skip"] == 2
    # Meme budgets remis a zero, les listes blanches de statuts l'excluent.
    _sql(wj, "UPDATE wiki_jobs SET attempts=0, attempts_alternate=0, expires_at=NULL,"
             " alt_next_at=NULL")
    assert wj.claim(limit=10)["leased"] == 0
    assert wj.claim_alternate() is None
    # Et l'administration ne le ressuscite pas.
    assert wj.requeue(job_ids=[a, b], dry_run=False, cause="x")["count"] == 0


# ------------------------------------------------------------------ events
def test_events_ordre_recent_d_abord_et_limite(wj):
    alt = _alt(wj)
    wj.record_alternate_failure(alt["job_id"], alt["lease_id"], alt["fencing_token"], ["x"])
    ev = wj.events(alt["job_id"])
    assert [e["event"] for e in ev] == ["alternate-invalid", "route-alternate"]
    assert len(wj.events(alt["job_id"][:8], limit=1)) == 1
    for e in ev:
        assert COURT not in (e["detail"] or "")  # jamais de contenu documentaire


# ------------------------------------------------------- surface MCP (wrappers)
def test_release_alternate_via_le_wrapper_mcp(wj):
    # Chemin exact du consommateur ChatGPT : wiki_ingest_release -> convia_mcp.wiki_release.
    from vault_mcp import convia_mcp
    importlib.reload(convia_mcp)
    wj.sync_source("raw/a.md", "a" * 64, COURT)
    job = convia_mcp.wiki_claim(limit=1)["jobs"][0]
    res = convia_mcp.wiki_release(job["job_id"], job["lease_id"], "alternate",
                                  "SKIPPED_SAFETY: lecture documentaire bloquee par la plateforme")
    assert res["status"] == wj.ALT_PENDING and res["attempts"] == 0
    row = _row(wj, job["job_id"])
    assert row["status"] == wj.ALT_PENDING and row["attempts"] == 0 and row["lease_id"] is None
    assert row["alt_reason"].startswith("SKIPPED_SAFETY")
    assert wj.ALTERNATE_REQUEST.exists()
    assert convia_mcp.wiki_claim(limit=10)["jobs"] == []
    with pytest.raises(convia_mcp.ConviaError, match="action inconnue"):
        convia_mcp.wiki_release(job["job_id"], "x", "teleporter", "")
