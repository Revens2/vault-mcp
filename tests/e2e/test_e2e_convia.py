"""ConvIA de bout en bout : serveur reel, consommateur tue aux frontieres.

Chaque test part d'une file vide et de conversations CANARY synthetiques.
"""

from __future__ import annotations

import time

import pytest

from tests.e2e import canary
from tests.e2e.conftest import kill_consumer_at
from tests.e2e.harness import Bench, RawClient, ToolRefusedError

pytestmark = pytest.mark.e2e


def _seed(bench: Bench, client: RawClient, convs: list[canary.Conv]) -> list[str]:
    paths = [bench.add_conversation(canary.SOURCE, c.name, c.body) for c in convs]
    client.call("convia_scan")
    return paths


def _counts(bench: Bench) -> dict[str, int]:
    rows = bench.sql("convia.db", "SELECT status, COUNT(*) n FROM pending_analysis GROUP BY status")
    return {r["status"]: r["n"] for r in rows}


def _analyse_one(client: RawClient, path: str, idx: int = 0) -> dict:
    read = client.call("convia_read_for_analysis", path=path)
    return client.call("convia_write_analysis", source_path=read["path"],
                       source_hash=read["source_sha256"],
                       analysis_version=read["analysis_version"],
                       markdown=canary.analysis_markdown(idx))


# ------------------------------------------------------------------ Crash A
def test_crash_apres_list_rien_n_est_perdu(bench: Bench, client: RawClient) -> None:
    _seed(bench, client, canary.tiny(5))
    before = _counts(bench)
    seen = kill_consumer_at(bench, "convia", "list")
    assert seen["list"]["count"] == 5
    assert _counts(bench) == before == {"pending": 5}
    # Le run suivant retrouve exactement le meme backlog et le traite.
    listed = client.call("convia_list_pending_analysis", limit=50)
    assert listed["pending_total"] == 5 and listed["returned"] == 5
    for i, item in enumerate(listed["items"]):
        _analyse_one(client, item["path"], i)
    assert _counts(bench) == {"done": 5}


# ------------------------------------------------------------------ Crash B
def test_crash_apres_read_la_conversation_reste_analysable(bench: Bench,
                                                          client: RawClient) -> None:
    _seed(bench, client, canary.tiny(3))
    seen = kill_consumer_at(bench, "convia", "read")
    assert _counts(bench) == {"pending": 3}
    listed = client.call("convia_list_pending_analysis", limit=50)
    assert seen["read"]["path"] in [i["path"] for i in listed["items"]]
    res = _analyse_one(client, seen["read"]["path"])
    assert res["duplicate"] is False
    assert _counts(bench) == {"pending": 2, "done": 1}


# ------------------------------------------------------------------ Crash C
def test_reponse_de_write_perdue_puis_rejeu_idempotent(bench: Bench, client: RawClient) -> None:
    (path,) = _seed(bench, client, canary.tiny(1))
    read = client.call("convia_read_for_analysis", path=path)
    args = {"source_path": read["path"], "source_hash": read["source_sha256"],
            "analysis_version": read["analysis_version"],
            "markdown": canary.analysis_markdown(0)}
    client.call_and_lose_response("convia_write_analysis", **args)
    deadline = time.monotonic() + 10
    while _counts(bench).get("done") != 1 and time.monotonic() < deadline:
        time.sleep(0.1)
    assert _counts(bench) == {"done": 1}, "le write a bien ete applique cote serveur"
    intents_before = sorted(p.name for p in (bench.root / "spool").rglob("*") if p.is_file())
    # Le consommateur ne sait pas : il rejoue.
    replay = client.call("convia_write_analysis", **args)
    assert replay["duplicate"] is True
    assert replay["path"].endswith("__analyse.md")
    intents_after = sorted(p.name for p in (bench.root / "spool").rglob("*") if p.is_file())
    assert intents_after == intents_before, "le rejeu ne depose aucune seconde intention"
    assert _counts(bench) == {"done": 1}


# ------------------------------------------------------------------ Crash D
def test_source_modifiee_entre_read_et_write(bench: Bench, client: RawClient) -> None:
    convs = canary.tiny(1)
    (path,) = _seed(bench, client, convs)
    read = client.call("convia_read_for_analysis", path=path)
    bench.add_conversation(canary.SOURCE, convs[0].name, convs[0].body + "\nsuite CANARY-E2E\n")
    with pytest.raises(ToolRefusedError, match="hash p"):
        client.call("convia_write_analysis", source_path=path,
                    source_hash=read["source_sha256"],
                    analysis_version=read["analysis_version"],
                    markdown=canary.analysis_markdown(0))
    assert _counts(bench) == {"pending": 1}, "aucune analyse appliquee a la mauvaise version"
    client.call("convia_scan")
    # Une seule entree servie : la nouvelle version. L'ancienne est retiree, pas supprimee.
    listed = client.call("convia_list_pending_analysis", limit=50)
    assert listed["returned"] == 1 and listed["pending_total"] == 1
    assert listed["items"][0]["hash"] != read["source_sha256"]
    assert _counts(bench) == {"pending": 1, "superseded": 1}
    res = _analyse_one(client, path)
    assert res["duplicate"] is False
    assert _counts(bench) == {"done": 1, "superseded": 1}


def test_versions_successives_une_seule_entree_en_tete(bench: Bench, client: RawClient) -> None:
    """Regression du 2026-09-12 : 41 des 50 premieres entrees etaient des versions mortes."""
    convs = canary.tiny(2)
    live, other = convs
    _seed(bench, client, convs)
    for k in range(4):
        bench.add_conversation(canary.SOURCE, live.name, live.body + f"\nappend {k} CANARY-E2E\n")
        client.call("convia_scan")
    listed = client.call("convia_list_pending_analysis", limit=50)
    paths = [i["path"] for i in listed["items"]]
    assert len(paths) == len(set(paths)) == 2
    assert listed["pending_total"] == 2
    for item in listed["items"]:
        res = _analyse_one(client, item["path"])
        assert res["duplicate"] is False
    after = client.call("convia_list_pending_analysis", limit=50)
    assert after["returned"] == 0 and after["pending_total"] == 0


def test_hash_modifie_sans_scan_le_compteur_bouge_quand_meme(bench: Bench,
                                                           client: RawClient) -> None:
    convs = canary.tiny(1)
    (path,) = _seed(bench, client, convs)
    bench.add_conversation(canary.SOURCE, convs[0].name, convs[0].body + "\nplus CANARY-E2E\n")
    res = _analyse_one(client, path)  # read rend le hash ACTUEL, pas celui liste
    assert res["duplicate"] is False
    listed = client.call("convia_list_pending_analysis", limit=50)
    assert listed["returned"] == 0, "l'entree listee n'est plus servie apres l'analyse"


# ------------------------------------------------------------------ Crash E
def test_pathological_une_unite_en_erreur_n_arrete_pas_les_autres(bench: Bench,
                                                                client: RawClient) -> None:
    convs = canary.pathological(20)
    _seed(bench, client, convs)
    by_name = {c.name: c for c in convs}
    listed = client.call("convia_list_pending_analysis", limit=50)
    assert listed["returned"] == 20
    ok = failed = 0
    for i, item in enumerate(listed["items"]):
        conv = by_name[item["path"].rsplit("/", 1)[1]]
        try:
            read = client.call("convia_read_for_analysis", path=item["path"])
            if conv.kind == "will_go_stale":
                bench.add_conversation(canary.SOURCE, conv.name, conv.body + "\nx CANARY-E2E\n")
            md = canary.analysis_markdown(i)
            if conv.kind == "huge":
                assert read["truncated"] is True
            client.call("convia_write_analysis", source_path=read["path"],
                        source_hash=read["source_sha256"],
                        analysis_version=read["analysis_version"], markdown=md)
            ok += 1
        except ToolRefusedError:
            failed += 1
    assert failed == 1, "seule la conversation devenue stale est refusee"
    assert ok == 19
    client.call("convia_scan")
    rest = client.call("convia_list_pending_analysis", limit=50)
    assert rest["returned"] == 1, "la version stale revient une fois, a jour"
