"""Worker alternatif `wiki_alternate` (OpenCode + Muse Spark) de bout en bout.

Le vrai binaire OpenCode est remplace par un faux executable genere dans tmp_path :
il rejoue un scenario JSON (une reponse par appel, dans l'ordre) et journalise
chaque appel (argv, fichiers `-f`, environnement recu). Couvre : chemin nominal
jusqu'a merge_pending, tentatives de contenu et erreurs rendues au modele,
quarantaine + notification unique, pannes provider (backoff global, lot interrompu,
alerte globale unique), garde de securite, confinement de l'environnement et de
la ligne de commande, verrou du worker. Aucun appel reseau, aucun LLM.
"""

from __future__ import annotations

import importlib
import json
import sys
import time
from pathlib import Path

import pytest

fcntl = pytest.importorskip("fcntl")  # worker POSIX (flock), comme en prod

FACTICE = Path(__file__).parent / "fixtures" / "contrat_wiki_factice.py"
MODELE = "opencode/muse-spark-1.3-contributor-free"
SOURCE = "raw/notes/passerelle-locale.md"
CITATION = "La passerelle locale relaie les requetes du vault vers le modele"
DOC_SOURCE = f"# Passerelle locale\n\n{CITATION} heberge sur le VPS.\n"
PHRASE_MDP = "Le compte de service utilise mdp : Azerty1234 pour la console."
DOC_MDP = f"# Acces console\n\n{PHRASE_MDP}\n\n{CITATION} heberge sur le VPS.\n"
CITATION_MDP = "Le compte de service utilise mdp : <REDACTED_PASSWORD> pour la console"
ERREUR_429 = "Error: 429 Too Many Requests\n"


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


def _extraction(slug="passerelle-locale", evidence=CITATION):
    """Extraction valide dont l'evidence est une citation litterale du job."""
    doc = _valid_extraction(slug=slug, title="Passerelle locale")
    doc["entities"][0]["evidence"] = evidence
    return doc


def _reponse(extraction=None, **kw):
    """Reponse du faux OpenCode : texte = l'objet JSON `extraction`."""
    doc = _extraction() if extraction is None else extraction
    return {"texte": json.dumps(doc, ensure_ascii=False), **kw}


INVALIDE = {"texte": "ceci n'est pas du JSON"}
PANNE_429 = {"rc": 1, "stderr": ERREUR_429}


# ------------------------------------------------------------ faux OpenCode
_SCRIPT = r'''#!@PYTHON@
"""Faux binaire opencode : rejoue un scenario JSON, journalise chaque appel."""
import json
import os
import sys
import time

SCENARIO = @SCENARIO@
COMPTEUR = @COMPTEUR@
JOURNAL = @JOURNAL@


def env_initial():
    # Environnement tel que recu a l'exec (avant toute coercition de locale par Python).
    try:
        with open("/proc/self/environ", "rb") as fh:
            brut = fh.read()
    except OSError:
        return dict(os.environ)
    env = {}
    for ligne in brut.split(b"\0"):
        if ligne:
            k, _, v = ligne.partition(b"=")
            env[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
    return env


args = sys.argv[1:]
fichiers = [args[i + 1] for i, a in enumerate(args[:-1]) if a == "-f"]
try:
    with open(COMPTEUR, encoding="utf-8") as fh:
        n = int(fh.read() or 0)
except OSError:
    n = 0
with open(COMPTEUR, "w", encoding="utf-8") as fh:
    fh.write(str(n + 1))
env = env_initial()
contenus = {}
for f in fichiers:
    try:
        with open(f, encoding="utf-8") as fh:
            contenus[os.path.basename(f)] = fh.read()
    except OSError as exc:
        contenus[os.path.basename(f)] = "ILLISIBLE " + str(exc)
xdg = env.get("XDG_DATA_HOME", "")
entree = {
    "n": n, "argv": args, "fichiers": fichiers, "contenus": contenus,
    "erreurs": any(os.path.basename(f) == "erreurs.txt" for f in fichiers),
    "env_keys": sorted(env), "env": env, "xdg_data_home": xdg,
    "xdg_existe": bool(xdg) and os.path.isdir(xdg), "cwd": os.getcwd(),
}
with open(JOURNAL, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(entree) + "\n")
with open(SCENARIO, encoding="utf-8") as fh:
    reponses = json.load(fh)
if n >= len(reponses):
    sys.stderr.write("FAUX-OPENCODE appel non prevu par le scenario\n")
    sys.exit(97)
r = reponses[n]
if r.get("sleep"):
    time.sleep(float(r["sleep"]))
evenements = r.get("evenements")
if evenements is None:
    evenements = [{"type": "step_start"}] + list(r.get("avant", []))
    if "texte" in r:
        evenements.append({"type": "text", "part": {"text": r["texte"]}})
    evenements.append({"type": "step_finish", "part": {"reason": r.get("fin", "stop")}})
sys.stdout.write(r.get("bruit", "") + "".join(json.dumps(e) + "\n" for e in evenements))
sys.stdout.flush()
sys.stderr.write(r.get("stderr", ""))
sys.stderr.flush()
sys.exit(int(r.get("rc", 0)))
'''


class FauxOpencode:
    def __init__(self, racine: Path) -> None:
        racine.mkdir(parents=True)
        self.scenario_path = racine / "scenario.json"
        self.compteur = racine / "compteur"
        self.journal = racine / "appels.jsonl"
        self.binaire = racine / "opencode"
        script = (_SCRIPT.replace("@PYTHON@", sys.executable)
                  .replace("@SCENARIO@", repr(str(self.scenario_path)))
                  .replace("@COMPTEUR@", repr(str(self.compteur)))
                  .replace("@JOURNAL@", repr(str(self.journal))))
        self.binaire.write_text(script, encoding="utf-8")
        self.binaire.chmod(0o755)
        self.scenario([])

    def scenario(self, reponses: list[dict]) -> None:
        """Nouveau scenario : les appels suivants le consomment depuis le debut."""
        self.scenario_path.write_text(json.dumps(reponses), encoding="utf-8")
        self.compteur.write_text("0", encoding="utf-8")

    def appels(self) -> list[dict]:
        if not self.journal.exists():
            return []
        return [json.loads(ln) for ln in
                self.journal.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ------------------------------------------------------------------ fixtures
@pytest.fixture
def wj(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WIKI_JOBS_DB", str(tmp_path / "wiki_jobs.db"))
    monkeypatch.setenv("WIKI_JOBS_SPOOL", str(tmp_path / "spool"))
    monkeypatch.setenv("WIKI_JOBS_MANIFEST", str(tmp_path / "manifest.jsonl"))
    monkeypatch.setenv("WIKI_RAW_DIR", str(tmp_path / "raw"))
    monkeypatch.setenv("WIKI_EXCLUDE_DIRS",
                       str(tmp_path / "raw" / "assets" / "ConvIA").replace("\\", "/")
                       + ":/srv/vault-mirror/raw/assets/ConvIA")
    # Seuil haut : chaque document de test tient en UN chunk (tokenizer ou repli octets).
    monkeypatch.setenv("WIKI_CHUNK_MIN_TOKENS", "5000")
    monkeypatch.setenv("WIKI_CHUNK_MIN_FLOOR", "8")
    monkeypatch.setenv("WIKI_CONTRACT_MODULE", str(FACTICE))
    monkeypatch.setenv("WIKI_NOTES_DIR", str(tmp_path / "wiki"))
    # Jamais les vrais marqueurs : ils reveilleraient la fusion / le worker en prod.
    monkeypatch.setenv("WIKI_INGEST_REQUEST", str(tmp_path / "wiki-ingest.request"))
    monkeypatch.setenv("WIKI_ALTERNATE_REQUEST", str(tmp_path / "wiki-alternate.request"))
    (tmp_path / "wiki" / "sources").mkdir(parents=True)
    from vault_mcp import wiki_jobs
    importlib.reload(wiki_jobs)
    return wiki_jobs


@pytest.fixture
def faux(tmp_path: Path) -> FauxOpencode:
    return FauxOpencode(tmp_path / "faux-opencode")


@pytest.fixture
def wa(wj, faux, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Le worker lit sa config A L'IMPORT : fixer l'env puis recharger wiki_jobs PUIS
    # wiki_alternate (qui lie wiki_jobs a l'import).
    monkeypatch.setenv("WIKI_ALT_MODEL", MODELE)
    monkeypatch.setenv("WIKI_ALT_OPENCODE", str(faux.binaire))
    monkeypatch.setenv("WIKI_ALT_STATE", str(tmp_path / "alt-state"))
    monkeypatch.setenv("WIKI_ALT_NOTIFY_SPOOL", str(tmp_path / "notify-spool"))
    monkeypatch.setenv("WIKI_ALT_TIMEOUT", "3")
    monkeypatch.delenv("WIKI_ALT_AGENT", raising=False)
    monkeypatch.delenv("WIKI_ALT_LEASE", raising=False)
    importlib.reload(wj)
    from vault_mcp import wiki_alternate
    importlib.reload(wiki_alternate)
    for chemin in (wiki_alternate.STATE_DIR, wiki_alternate.NOTIFY_SPOOL,
                   wiki_alternate.INGEST_REQUEST, wj.ALTERNATE_REQUEST, wj.DB_PATH):
        assert str(chemin).startswith(str(tmp_path)), chemin
    assert wiki_alternate.MODEL_ID == MODELE
    assert wiki_alternate.CALL_TIMEOUT_S == 3
    return wiki_alternate


# ------------------------------------------------------------------ helpers
def _row(wj, job_id):
    conn = wj.connect()
    try:
        return dict(conn.execute("SELECT * FROM wiki_jobs WHERE job_id=?", (job_id,)).fetchone())
    finally:
        conn.close()


def _sql(wj, requete, args=()):
    conn = wj.connect()
    try:
        conn.execute(requete, args)
        conn.commit()
    finally:
        conn.close()


def _route_alternate(wj, source=SOURCE, sha="a" * 64, contenu=DOC_SOURCE):
    """Job reserve par ChatGPT puis route vers le worker alternatif (chemin reel)."""
    wj.sync_source(source, sha, contenu)
    job = wj.claim(limit=1)["jobs"][0]
    assert job["source"] == source and job["chunk_count"] == 1
    r = wj.release(job["job_id"], job["lease_id"], "alternate",
                   reason="SKIPPED_SAFETY plateforme")
    assert r["status"] == wj.ALT_PENDING
    return job["job_id"]


def _notifs(wa):
    if not wa.NOTIFY_SPOOL.is_dir():
        return []
    return [json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(wa.NOTIFY_SPOOL.iterdir())
            if p.suffix == ".json" and not p.name.startswith(".")]


def _provider(wa):
    return json.loads((wa.STATE_DIR / "provider.json").read_text(encoding="utf-8"))


def _lever_backoff(wa):
    """Simule l'ecoulement du backoff global (until remis a 0, compteurs conserves)."""
    st = _provider(wa)
    st["until"] = 0
    (wa.STATE_DIR / "provider.json").write_text(json.dumps(st), encoding="utf-8")


def _message(appel):
    """Consigne passee en argument : le positionnel juste avant le premier `-f`."""
    argv = appel["argv"]
    return argv[argv.index("-f") - 1]


def _lot(wa, batch=3):
    return wa.run_batch(batch, 60)


def _assert_panne_sans_tentative(wa, wj, jid, kind):
    row = _row(wj, jid)
    assert row["status"] == wj.ALT_PENDING
    assert row["attempts_alternate"] == 0
    assert row["provider_failures"] == 1
    assert row["lease_id"] is None
    assert int(row["alt_next_at"] or 0) > time.time()
    st = _provider(wa)
    assert st["last_kind"] == kind and st["failures"] == 1
    assert st["until"] > time.time()
    assert wj.status()["spool_files"] == 0
    assert "alternate-provider" in {e["event"] for e in wj.events(jid)}


# ------------------------------------------------------------ 1. nominal
def test_nominal_route_alternate_soumis_puis_fusionne(wa, wj, faux, monkeypatch):
    jid = _route_alternate(wj)
    assert wj.ALTERNATE_REQUEST.exists()  # marqueur de reveil du worker
    faux.scenario([_reponse()])
    vu_au_merge = []
    merge_reel = wj.merge_pending

    def espion(**kw):
        vu_au_merge.append(_row(wj, jid)["status"])
        return merge_reel(**kw)

    monkeypatch.setattr(wj, "merge_pending", espion)
    stats = _lot(wa)
    assert stats == {"claimed": 1, "submitted": 1, "quarantined": 0, "provider": 0, "other": 0}
    assert vu_au_merge == ["submitted"]  # soumis PUIS merge_pending appele
    row = _row(wj, jid)
    assert row["status"] == "merged" and row["model"] == MODELE
    assert row["attempts_alternate"] == 0 and row["attempts"] == 0
    env = json.loads(wj._spool_path("a" * 64, 0).read_text(encoding="utf-8"))
    assert env["extraction"]["model"] == MODELE
    assert env["note"]["slug"] == "passerelle-locale"
    assert wa.INGEST_REQUEST.exists()  # fusion des fiches demandee
    evs = wj.events(jid)
    assert any(e["event"] == "alternate-submitted" and e["to_status"] == "submitted"
               for e in evs)
    st = wj.status()
    assert st["alternate_completed"] == 1
    assert st["last_alternate_success_at"] is not None
    appels = faux.appels()
    assert len(appels) == 1
    assert _message(appels[0]) == wa.MESSAGE_FIRST
    assert appels[0]["argv"][appels[0]["argv"].index("-m") + 1] == MODELE
    assert appels[0]["erreurs"] is False
    assert _notifs(wa) == []


# ------------------------------------------------------------ 2. retry
def test_json_invalide_puis_valide_erreurs_exactes_rendues(wa, wj, faux):
    jid = _route_alternate(wj)
    faux.scenario([INVALIDE, _reponse()])
    with pytest.raises(wa.ModelOutputError) as attendu:
        wa.parse_extraction(INVALIDE["texte"])
    stats = _lot(wa)
    assert stats["submitted"] == 1 and stats["claimed"] == 1
    row = _row(wj, jid)
    assert row["attempts_alternate"] == 1
    assert row["status"] in ("submitted", "merged")
    assert "alternate-submitted" in {e["event"] for e in wj.events(jid)}
    premier, second = faux.appels()
    assert premier["erreurs"] is False and second["erreurs"] is True
    assert second["contenus"]["erreurs.txt"] == "- " + attendu.value.errors[0]
    assert "JSON invalide" in second["contenus"]["erreurs.txt"]
    assert _message(premier) == wa.MESSAGE_FIRST
    assert _message(second) == wa.MESSAGE_RETRY
    assert wa.MESSAGE_RETRY != wa.MESSAGE_FIRST
    assert [Path(f).name for f in second["fichiers"]] == [
        "document.md", "contrat.json", "erreurs.txt"]


# ------------------------------------------------------------ 3. quarantaine
def test_trois_reponses_invalides_quarantaine_et_une_notification(wa, wj, faux):
    jid = _route_alternate(wj)
    faux.scenario([INVALIDE, INVALIDE, INVALIDE])
    stats = _lot(wa)
    assert stats["quarantined"] == 1 and stats["submitted"] == 0
    row = _row(wj, jid)
    assert row["status"] == wj.ALT_QUARANTINED and row["attempts_alternate"] == 3
    assert wj.status()["alternate_quarantined"] == 1
    notifs = _notifs(wa)
    assert len(notifs) == 1
    n = notifs[0]
    assert n["emoji"] == "FAIL"
    assert jid[:8] in n["text"]
    assert "passerelle-locale.md" in n["text"] and "raw/notes" not in n["text"]
    assert MODELE in n["text"]
    assert "attempts: 3" in n["text"]
    assert CITATION not in n["text"] and "heberge" not in n["text"]
    # Deduplication persistante : un second signalement du meme job ne cree rien.
    assert wa.notify_job_failed({"job_id": jid, "source": SOURCE}, 3, "encore") is False
    assert len(_notifs(wa)) == 1
    # Pas de boucle : ni ce lot, ni un run suivant ne rappellent le modele.
    assert len(faux.appels()) == 3
    assert wa.main(["--batch", "3"]) == 0
    assert _lot(wa)["claimed"] == 0
    assert len(faux.appels()) == 3
    assert len(_notifs(wa)) == 1


# ------------------------------------------------------------ 4. panne provider
def test_panne_429_rend_le_job_sans_tentative_et_arrete_le_lot(wa, wj, faux):
    j1 = _route_alternate(wj, "raw/notes/a.md", "a" * 64)
    j2 = _route_alternate(wj, "raw/notes/b.md", "b" * 64)
    faux.scenario([PANNE_429, _reponse(), _reponse()])
    stats = _lot(wa, batch=3)
    assert stats == {"claimed": 1, "submitted": 0, "quarantined": 0, "provider": 1, "other": 0}
    assert len(faux.appels()) == 1  # le 2e job n'est PAS reclame dans ce run
    rows = {j: _row(wj, j) for j in (j1, j2)}
    touche = [j for j, r in rows.items() if r["provider_failures"] == 1]
    assert len(touche) == 1
    autre = j2 if touche[0] == j1 else j1
    _assert_panne_sans_tentative(wa, wj, touche[0], "rate_limit")
    assert rows[autre]["status"] == wj.ALT_PENDING
    assert rows[autre]["provider_failures"] == 0 and rows[autre]["fencing_token"] == 1
    assert "429" in _provider(wa)["last_detail"]
    # Run suivant immediat : backoff global, rien n'est reclame.
    assert _lot(wa) == {"claimed": 0, "submitted": 0, "quarantined": 0, "provider": 0,
                        "other": 0}
    assert len(faux.appels()) == 1
    assert _notifs(wa) == []  # une seule panne : pas encore d'alerte globale


# ------------------------------------------------------------ 5. modele indisponible
def test_modele_indisponible_classe_provider(wa, wj, faux):
    jid = _route_alternate(wj)
    faux.scenario([{
        "rc": 1,
        "evenements": [{"type": "step_start"},
                       {"type": "error", "error": {"name": "ProviderModelNotFoundError",
                                                   "data": {"message": "Model not found"}}}],
        "stderr": "ProviderModelNotFoundError: Model not found\n",
    }])
    assert _lot(wa)["provider"] == 1
    _assert_panne_sans_tentative(wa, wj, jid, "model_unavailable")


# ------------------------------------------------------------ 6. timeout
def test_timeout_classe_provider(wa, wj, faux):
    jid = _route_alternate(wj)
    faux.scenario([_reponse(sleep=wa.CALL_TIMEOUT_S + 3)])
    t0 = time.monotonic()
    assert _lot(wa)["provider"] == 1
    assert time.monotonic() - t0 < wa.CALL_TIMEOUT_S + 2.5  # tue au delai, pas attendu
    _assert_panne_sans_tentative(wa, wj, jid, "timeout")
    appel = faux.appels()[0]
    assert not Path(appel["xdg_data_home"]).exists()  # donnees ephemeres detruites


# ------------------------------------------------------------ 7. alerte globale
def test_pannes_consecutives_une_seule_alerte_globale(wa, wj, faux):
    jid = _route_alternate(wj)
    faux.scenario([PANNE_429] * 5)
    for run in range(1, 6):
        if run > 1:
            _lever_backoff(wa)
            _sql(wj, "UPDATE wiki_jobs SET alt_next_at=NULL WHERE job_id=?", (jid,))
        assert _lot(wa)["provider"] == 1, run
        assert _provider(wa)["failures"] == run
        attendu = 0 if run < wa.GLOBAL_ALERT_AFTER else 1
        assert len(_notifs(wa)) == attendu, run  # cooldown : jamais une 2e alerte
    assert len(faux.appels()) == 5
    (alerte,) = _notifs(wa)
    assert alerte["emoji"] == "WARN"
    assert MODELE in alerte["text"] and "rate_limit" in alerte["text"]
    assert "consecutifs: 3" in alerte["text"]
    row = _row(wj, jid)
    assert row["attempts_alternate"] == 0 and row["provider_failures"] == 5
    assert row["status"] == wj.ALT_PENDING


# ------------------------------------------------------------ 8. securite
def test_evenement_outil_arret_securite_rien_soumis(wa, wj, faux):
    jid = _route_alternate(wj)
    faux.scenario([_reponse(avant=[{"type": "tool_use",
                                    "part": {"tool": "bash", "state": {"input": "ls"}}}])])
    assert _lot(wa)["provider"] == 1
    _assert_panne_sans_tentative(wa, wj, jid, "securite")
    assert wj.status()["submitted_spooled"] == 0


def test_parse_events_outil_leve_securite(wa):
    ev = (json.dumps({"type": "step_start"}) + "\n"
          + json.dumps({"type": "tool_use", "part": {"tool": "read"}}) + "\n"
          + json.dumps({"type": "text", "part": {"text": "{}"}}) + "\n"
          + json.dumps({"type": "step_finish", "part": {"reason": "stop"}}) + "\n")
    with pytest.raises(wa.ProviderError) as exc:
        wa.parse_events(0, ev.encode(), b"")
    assert exc.value.kind == "securite" and "tool_use" in exc.value.detail


# ------------------------------------------------------------ 9. reponse tronquee
def test_fin_length_consomme_une_tentative(wa, wj, faux):
    jid = _route_alternate(wj)
    faux.scenario([_reponse(fin="length"), _reponse()])
    assert _lot(wa)["submitted"] == 1
    assert _row(wj, jid)["attempts_alternate"] == 1
    second = faux.appels()[1]
    assert "reponse incomplete (fin=length)" in second["contenus"]["erreurs.txt"]


# ------------------------------------------------------------ 10. habillages
@pytest.mark.parametrize("habillage", [
    pytest.param(lambda t: "```json\n" + t + "\n```", id="cloture-json"),
    pytest.param(lambda t: "```\n" + t + "\n```", id="cloture-nue"),
    pytest.param(lambda t: json.dumps({"extraction": json.loads(t)}), id="enveloppe"),
    pytest.param(lambda t: "﻿  " + t + "\n\n", id="bom-espaces"),
])
def test_habillage_syntaxique_accepte(wa, wj, faux, habillage):
    jid = _route_alternate(wj)
    faux.scenario([{"texte": habillage(_reponse()["texte"])}])
    assert _lot(wa)["submitted"] == 1
    assert _row(wj, jid)["attempts_alternate"] == 0


def test_texte_avant_le_json_refuse(wa, wj, faux):
    jid = _route_alternate(wj)
    faux.scenario([{"texte": "Voici l'extraction : " + _reponse()["texte"]}, _reponse()])
    assert _lot(wa)["submitted"] == 1
    assert _row(wj, jid)["attempts_alternate"] == 1
    assert "JSON invalide" in faux.appels()[1]["contenus"]["erreurs.txt"]


# ------------------------------------------------------------ 11. contenu refuse
def test_evidence_non_litterale_consomme_une_tentative(wa, wj, faux):
    jid = _route_alternate(wj)
    faux.scenario([_reponse(_extraction(evidence="une phrase inventee absente du texte")),
                   _reponse()])
    assert _lot(wa)["submitted"] == 1
    assert _row(wj, jid)["attempts_alternate"] == 1
    assert "citation litterale" in faux.appels()[1]["contenus"]["erreurs.txt"]


def test_valeur_secrete_dans_la_reponse_refusee(wa, wj, faux):
    jid = _route_alternate(wj)
    fuite = _extraction()
    fuite["note"]["summary"] = "Acces console mdp: Azerty1234"
    faux.scenario([_reponse(fuite), _reponse()])
    assert _lot(wa)["submitted"] == 1
    assert _row(wj, jid)["attempts_alternate"] == 1
    erreurs = faux.appels()[1]["contenus"]["erreurs.txt"]
    assert "ressemblant a un secret (mdp)" in erreurs
    assert "Azerty1234" not in erreurs  # la famille, jamais la valeur
    env = json.loads(wj._spool_path("a" * 64, 0).read_text(encoding="utf-8"))
    assert "Azerty1234" not in json.dumps(env)


def test_marqueur_redacted_recopie_non_signale(wa, wj, faux):
    jid = _route_alternate(wj, contenu=DOC_MDP)
    faux.scenario([_reponse(_extraction(evidence=CITATION_MDP))])
    assert _lot(wa)["submitted"] == 1
    assert _row(wj, jid)["attempts_alternate"] == 0  # marqueur != secret
    doc = faux.appels()[0]["contenus"]["document.md"]
    assert "Azerty1234" not in doc and CITATION_MDP in doc  # le modele n'a vu que le marqueur


# ------------------------------------------------------------ 12. confinement
def test_environnement_et_ligne_de_commande_confines(wa, wj, faux, monkeypatch):
    monkeypatch.setenv("OPENCODE_FAUX_REGLAGE", "1")
    monkeypatch.setenv("WIKI_JETON_FACTICE", "ne-doit-pas-fuir")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "factice-ne-doit-pas-fuir")
    monkeypatch.setenv("LANG", "C.UTF-8")
    _route_alternate(wj)
    faux.scenario([_reponse()])
    assert _lot(wa)["submitted"] == 1
    (appel,) = faux.appels()
    autorisees = {"PATH", "HOME", "LANG", "LC_ALL", "TZ"}
    hors = [k for k in appel["env_keys"]
            if k not in autorisees and not k.startswith(("XDG_", "OPENCODE_"))]
    assert hors == []
    assert not any(k.startswith("WIKI_") for k in appel["env_keys"])
    assert "ne-doit-pas-fuir" not in json.dumps(appel["env"])
    assert appel["env"]["OPENCODE_FAUX_REGLAGE"] == "1"
    assert appel["env"]["HOME"] == str(wa.STATE_DIR)
    assert appel["env"]["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    # Donnees de session (donc le document) ephemeres : presentes pendant, detruites apres.
    assert appel["xdg_existe"] is True
    assert appel["xdg_data_home"].startswith(str(wa.STATE_DIR / "runs"))
    assert not Path(appel["xdg_data_home"]).exists()
    assert list((wa.STATE_DIR / "runs").iterdir()) == []
    # Ligne de commande : message statique PUIS uniquement des paires `-f chemin`.
    argv = appel["argv"]
    i = argv.index("-f")
    assert argv[i - 1] == wa.MESSAGE_FIRST
    suite = argv[i:]
    assert len(suite) % 2 == 0 and suite[0::2] == ["-f"] * (len(suite) // 2)
    assert [Path(p).name for p in suite[1::2]] == ["document.md", "contrat.json"]
    tout = "\n".join(argv)
    for fragment in (CITATION, "heberge sur le VPS", "Passerelle locale", "passerelle-locale"):
        assert fragment not in tout, fragment
    assert CITATION in appel["contenus"]["document.md"]  # le document passe par fichier


# ------------------------------------------------------------ 13. verrou
def test_verrou_tenu_main_sort_sans_rien_reclamer(wa, wj, faux):
    jid = _route_alternate(wj)
    faux.scenario([_reponse()])
    wa.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (wa.STATE_DIR / "worker.lock").open("w") as verrou:
        fcntl.flock(verrou, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert wa.main(["--batch", "3"]) == 0
        assert faux.appels() == []
        row = _row(wj, jid)
        assert row["status"] == wj.ALT_PENDING and row["fencing_token"] == 1
    assert wa.main(["--batch", "3"]) == 0  # verrou libere : le lot passe
    assert len(faux.appels()) == 1
    assert _row(wj, jid)["status"] == "merged"


# ------------------------------------------------------------ 14. garde-fou provider
def test_garde_fou_pannes_provider_repetees_quarantaine(wa, wj, faux):
    jid = _route_alternate(wj)
    _sql(wj, "UPDATE wiki_jobs SET provider_failures=9 WHERE job_id=?", (jid,))
    faux.scenario([PANNE_429])
    assert _lot(wa)["provider"] == 1
    row = _row(wj, jid)
    assert row["status"] == wj.ALT_QUARANTINED
    assert row["provider_failures"] == wa.MAX_PROVIDER_FAILURES_PER_JOB
    assert row["attempts_alternate"] == 0
    (n,) = _notifs(wa)
    assert n["emoji"] == "FAIL" and jid[:8] in n["text"]
    assert "pannes provider repetees : rate_limit" in n["text"]
    _lever_backoff(wa)
    assert _lot(wa)["claimed"] == 0  # plus jamais reclame
    assert len(faux.appels()) == 1


# ------------------------------------------------------------ complements
def test_compteur_de_tentatives_survit_a_une_panne_provider(wa, wj, faux):
    jid = _route_alternate(wj)
    faux.scenario([INVALIDE, PANNE_429])
    assert _lot(wa)["provider"] == 1
    row = _row(wj, jid)
    assert row["status"] == wj.ALT_PENDING and row["attempts_alternate"] == 1
    _lever_backoff(wa)
    _sql(wj, "UPDATE wiki_jobs SET alt_next_at=NULL WHERE job_id=?", (jid,))
    faux.scenario([INVALIDE, INVALIDE, _reponse()])
    assert _lot(wa)["quarantined"] == 1
    row = _row(wj, jid)
    assert row["status"] == wj.ALT_QUARANTINED and row["attempts_alternate"] == 3
    assert len(faux.appels()) == 4  # 2 + 2 : la 3e reponse du 2e scenario jamais demandee
    assert [n["emoji"] for n in _notifs(wa)] == ["FAIL"]


def test_refus_au_submit_compte_puis_quarantaine_notifiee(wa, wj, faux, monkeypatch):
    # Course simulee : la validation a blanc accepte, submit refuse (budget alternatif).
    jid = _route_alternate(wj)
    monkeypatch.setattr(wj, "check_extraction",
                        lambda *a, **k: {"ok": True, "errors": [], "warnings": []})
    refuse = _extraction()
    refuse["note"]["tags"] = []
    faux.scenario([_reponse(refuse)] * 3)
    stats = _lot(wa, batch=5)
    assert stats["other"] == 3 and stats["submitted"] == 0
    row = _row(wj, jid)
    assert row["status"] == wj.ALT_QUARANTINED and row["attempts_alternate"] == 3
    (n,) = _notifs(wa)
    assert n["emoji"] == "FAIL" and "attempts: 3" in n["text"]
    assert len(faux.appels()) == 3


# ------------------------------------------------------------ bugs suspectes
# Regression : requeue(target="alternate") remet aussi provider_failures a zero.
def test_requeue_apres_garde_fou_provider_rend_un_budget_neuf(wa, wj, faux):
    jid = _route_alternate(wj)
    _sql(wj, "UPDATE wiki_jobs SET provider_failures=9 WHERE job_id=?", (jid,))
    faux.scenario([PANNE_429])
    _lot(wa)
    assert _row(wj, jid)["status"] == wj.ALT_QUARANTINED
    wj.requeue(job_ids=[jid], dry_run=False, cause="provider repare", target="alternate")
    assert _row(wj, jid)["status"] == wj.ALT_PENDING
    _lever_backoff(wa)
    faux.scenario([PANNE_429])
    assert _lot(wa)["provider"] == 1
    assert _row(wj, jid)["status"] == wj.ALT_PENDING  # obtenu : alternate_quarantined


# Regression : la deduplication porte sur la quarantaine, pas sur le job pour toujours.
def test_nouvel_echec_apres_requeue_notifie(wa, wj, faux):
    jid = _route_alternate(wj)
    faux.scenario([INVALIDE] * 3)
    _lot(wa)
    assert len(_notifs(wa)) == 1
    wj.requeue(job_ids=[jid], dry_run=False, cause="contrat corrige", target="alternate")
    faux.scenario([INVALIDE] * 3)
    assert _lot(wa)["quarantined"] == 1
    assert len(_notifs(wa)) == 2  # obtenu : 1


# ---------------------------------------------- confirmation du modele (journal INFO)
def test_journal_confirme_le_modele_attendu(wa, wj, faux):
    jid = _route_alternate(wj)
    faux.scenario([_reponse(stderr="INFO  2026-09-11T10:00:00 +12ms service=llm"
                                   " providerID=opencode modelID=muse-spark-1.3-contributor-free"
                                   " sessionID=ses_factice stream\n")])
    assert _lot(wa)["submitted"] == 1
    assert _row(wj, jid)["model"] == MODELE


def test_journal_autre_modele_panne_config_sans_tentative(wa, wj, faux):
    jid = _route_alternate(wj)
    faux.scenario([_reponse(stderr="INFO  service=llm providerID=opencode modelID=big-pickle"
                                   " stream\n")])
    assert _lot(wa)["provider"] == 1
    _assert_panne_sans_tentative(wa, wj, jid, "config")
    assert "big-pickle" in _provider(wa)["last_detail"]


def test_check_model_unitaire(wa):
    ok = f"providerID=opencode modelID={MODELE.split('/', 1)[1]}\n".encode()
    wa.check_model(ok)
    wa.check_model(ok * 2)
    wa.check_model(b"")  # aucune ligne : simple journal, pas d'echec
    for mauvais in (b"providerID=autre modelID=muse-spark-1.3-contributor-free\n",
                    ok + b"providerID=opencode modelID=big-pickle\n"):
        with pytest.raises(wa.ProviderError) as exc:
            wa.check_model(mauvais)
        assert exc.value.kind == "config"


# ------------------------------------------------------------ 15. unitaires
def _ndjson(*evs):
    return "".join(json.dumps(e) + "\n" for e in evs).encode()


DEBUT = {"type": "step_start"}
FIN_STOP = {"type": "step_finish", "part": {"reason": "stop"}}


def test_parse_events_concatene_et_ignore_le_bruit(wa):
    out = (b"INFO demarrage\n{pas du json\n"
           + _ndjson(DEBUT, {"type": "text", "part": {"text": "ab"}},
                     {"type": "text", "part": {"text": "cd"}}, FIN_STOP))
    assert wa.parse_events(0, out, b"INFO bavard 503 unavailable\n") == "abcd"


def test_parse_events_sans_fin_est_une_reponse_incomplete(wa):
    with pytest.raises(wa.ModelOutputError, match="fin=None"):
        wa.parse_events(0, _ndjson(DEBUT, {"type": "text", "part": {"text": "{}"}}), b"")


def test_parse_events_erreur_meme_avec_rc_zero(wa):
    out = _ndjson(DEBUT, {"type": "error", "error": {"name": "APIError",
                                                      "data": {"message": "fetch failed"}}})
    with pytest.raises(wa.ProviderError) as exc:
        wa.parse_events(0, out, b"")
    assert exc.value.kind == "network"


def test_parse_events_refus_de_contenu_consomme(wa):
    with pytest.raises(wa.ModelOutputError, match="refus de contenu"):
        wa.parse_events(1, b"", b"Error: blocked by content policy\n")
    # ... sauf s'il s'agit en fait d'une limite de debit (provider).
    with pytest.raises(wa.ProviderError) as exc:
        wa.parse_events(1, b"", b"Error: 429 moderation quota exceeded\n")
    assert exc.value.kind == "rate_limit"


def test_parse_events_classe_sur_les_seules_lignes_d_erreur(wa):
    # Journal INFO bavard : un mot de refus hors ligne d'erreur ne transforme pas une
    # panne reseau en faute du document.
    err = (b"INFO service=session message=safety checks loaded\n"
           b"INFO service=provider message=moderation module ready\n"
           b"ERROR service=llm error=ECONNRESET socket closed\n")
    with pytest.raises(wa.ProviderError) as exc:
        wa.parse_events(1, b"", err)
    assert exc.value.kind == "network"


def test_parse_events_detail_caviarde(wa):
    cle = "sk-" + "abcdefghijklmnopqrstuvwx"
    with pytest.raises(wa.ProviderError) as exc:
        wa.parse_events(1, b"", f"Error: 401 invalid api key {cle}\n".encode())
    assert exc.value.kind == "auth"
    assert cle not in exc.value.detail and "<REDACTED_API_KEY>" in exc.value.detail


@pytest.mark.parametrize("blob, attendu", [
    ("Error: 429 Too Many Requests", "rate_limit"),
    ("FreeUsageLimitError: limite gratuite atteinte", "rate_limit"),
    ("ProviderModelNotFoundError: Model not found", "model_unavailable"),
    ("401 Unauthorized", "auth"),
    ("connect ECONNREFUSED 127.0.0.1:443", "network"),
    ("503 Service Unavailable", "server"),
    ("rien de reconnaissable", "unknown"),
])
def test_classify_failure(wa, blob, attendu):
    assert wa.classify_failure(blob) == attendu


def test_parse_extraction_corrections_syntaxiques_seulement(wa):
    doc = _extraction()
    t = json.dumps(doc)
    assert wa.parse_extraction("```json\n" + t + "\n```") == doc
    assert wa.parse_extraction(json.dumps({"extraction": doc})) == doc
    assert wa.parse_extraction("﻿" + t) == doc
    # Enveloppe avec une cle en plus : pas deballee (jamais de reecriture du contenu).
    assert set(wa.parse_extraction(json.dumps({"extraction": doc, "x": 1}))) == {"extraction",
                                                                                 "x"}
    for texte, motif in (("", "reponse vide"), ("```json\n\n```", "reponse vide"),
                         ("Voici : " + t, "JSON invalide"),
                         ("```json\n" + t + "\n```\nfin", "JSON invalide"),
                         ("[1, 2]", "racine"), ('"texte"', "racine")):
        with pytest.raises(wa.ModelOutputError, match=motif):
            wa.parse_extraction(texte)


def test_evidence_et_secret_unitaires(wa):
    proj = f"entete\n<<<DOC\n{CITATION.upper()}\n  heberge   sur le VPS.\nDOC>>>\n"
    # Casse et espaces normalises ; le reste doit etre litteral.
    assert wa.evidence_errors(_extraction(evidence=CITATION + " heberge sur le VPS"), proj) == []
    errs = wa.evidence_errors(_extraction(evidence="absente"), proj)
    assert len(errs) == 1 and "ent-test" in errs[0] and "citation litterale" in errs[0]
    assert wa.secret_errors(_extraction(evidence=CITATION_MDP)) == []
    fuite = _extraction()
    fuite["note"]["summary"] = "mdp: Azerty1234"
    (err,) = wa.secret_errors(fuite)
    assert "(mdp)" in err and "Azerty1234" not in err
