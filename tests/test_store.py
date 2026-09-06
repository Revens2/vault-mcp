"""Tests de `vault_mcp.store`, sans CouchDB : httpx.MockTransport tient le role.

L'enjeu principal teste ici n'est pas le bonheur du chemin nominal mais la
**non-divulgation** : `COUCH_URL` contient les identifiants, et httpx recopie l'URL
complete dans ses exceptions. Un message d'erreur qui fuit est une vraie fuite.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest

from vault_mcp.safety import CheminInvalideError
from vault_mcp.store import CouchStore, StoreError, _borner

URL_AVEC_SECRET = "http://nexususer:MotDePasseTresSecret@192.0.2.9:5984"
MOTS_INTERDITS = ("MotDePasseTresSecret", "nexususer", "192.0.2.9", "5984")


def _store(gestionnaire: object) -> CouchStore:
    transport = httpx.MockTransport(gestionnaire)  # type: ignore[arg-type]
    return CouchStore(URL_AVEC_SECRET, "vault_rag", httpx.Client(transport=transport))


def _reponse(charge: object, code: int = 200) -> httpx.Response:
    return httpx.Response(code, content=json.dumps(charge).encode())


def test_lire_note_renvoie_le_contenu() -> None:
    store = _store(lambda r: _reponse({"content": "# Titre\ncorps"}))
    assert store.lire_note("wiki/n.md") == "# Titre\ncorps"


def test_lire_note_absente_renvoie_none() -> None:
    store = _store(lambda r: _reponse({"error": "not_found"}, 404))
    assert store.lire_note("wiki/absente.md") is None


def test_lire_note_encode_les_slash_du_id() -> None:
    vues: list[str] = []

    def gestionnaire(requete: httpx.Request) -> httpx.Response:
        vues.append(str(requete.url))
        return _reponse({"content": "ok"})

    _store(gestionnaire).lire_note("wiki/concepts/Portage.md")
    # Sans encodage, CouchDB lirait `wiki/concepts/Portage.md` comme une hierarchie
    # d'URL et renverrait 404 sur une base qui contient pourtant le document.
    assert "wiki%2Fconcepts%2FPortage.md" in vues[0]


def test_lire_note_valide_le_chemin_avant_tout_appel() -> None:
    def gestionnaire(requete: httpx.Request) -> httpx.Response:
        raise AssertionError("aucun appel reseau ne doit partir pour un chemin invalide")

    with pytest.raises(CheminInvalideError):
        _store(gestionnaire).lire_note("../../etc/passwd.md")


def test_lister_ecarte_les_ids_reserves() -> None:
    store = _store(
        lambda r: _reponse({"rows": [{"id": "_design/v"}, {"id": "wiki/a.md"}, {"id": "b.md"}]})
    )
    assert store.lister_chemins() == ["wiki/a.md", "b.md"]


def test_lister_filtre_par_prefixe() -> None:
    store = _store(lambda r: _reponse({"rows": [{"id": "wiki/a.md"}, {"id": "raw/b.md"}]}))
    assert store.lister_chemins(prefix="wiki") == ["wiki/a.md"]


def test_limite_superieure_au_corpus_rend_tout() -> None:
    lignes = [{"id": f"n{i}.md"} for i in range(10)]
    store = _store(lambda r: _reponse({"rows": lignes}))
    assert len(store.lister_chemins(limit=1_000_000)) == 10


# Le contrat a CHANGE deliberement (adr/0020) : `limit=0` valait une erreur, il
# vaut desormais "sans limite". L ancien test n est pas contourne, il est retire.
@pytest.mark.parametrize("limit", [0, -1])
def test_limite_nulle_ou_negative_signifie_sans_limite(limit: int) -> None:
    import sys

    assert _borner(limit) == sys.maxsize


def test_limite_positive_est_rendue_telle_quelle() -> None:
    assert _borner(7) == 7


def test_limite_zero_rend_tout_le_corpus() -> None:
    lignes = [{"id": f"n{i}.md"} for i in range(10)]
    store = _store(lambda r: _reponse({"rows": lignes}))
    assert len(store.lister_chemins(limit=0)) == 10


def test_recherche_renvoie_path_et_snippet() -> None:
    doc = {"path": "wiki/a.md", "content": "avant " * 20 + "AIGUILLE" + " apres" * 20}
    store = _store(lambda r: _reponse({"docs": [doc]}))
    resultats = store.rechercher("aiguille")
    assert resultats[0].path == "wiki/a.md"
    assert "AIGUILLE" in resultats[0].snippet
    assert "\n" not in resultats[0].snippet


def test_recherche_echappe_les_metacaracteres() -> None:
    vues: list[dict[str, object]] = []

    def gestionnaire(requete: httpx.Request) -> httpx.Response:
        vues.append(json.loads(requete.content))
        return _reponse({"docs": []})

    _store(gestionnaire).rechercher("a.*b(")
    # Sans echappement, `(` casse la regex CouchDB et `.*` transforme une recherche
    # litterale en balayage integral. On lit la valeur brute : `str()` sur le dict
    # redoublerait les antislashs et le test comparerait des repr, pas des regex.
    selecteur = vues[0]["selector"]
    assert isinstance(selecteur, dict)
    envoye = selecteur["content"]["$regex"]
    assert envoye == "(?i)" + re.escape("a.*b(")


def test_recherche_vide_refusee() -> None:
    store = _store(lambda r: _reponse({"docs": []}))
    with pytest.raises(StoreError):
        store.rechercher("   ")


def test_erreur_reseau_ne_divulgue_ni_url_ni_identifiants() -> None:
    def gestionnaire(requete: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connexion refusee vers " + URL_AVEC_SECRET)

    with pytest.raises(StoreError) as capture:
        _store(gestionnaire).lire_note("wiki/n.md")

    message = str(capture.value)
    for interdit in MOTS_INTERDITS:
        assert interdit not in message


def test_erreur_http_ne_divulgue_pas_le_stockage() -> None:
    store = _store(lambda r: _reponse({"error": "unauthorized"}, 401))
    with pytest.raises(StoreError) as capture:
        store.lire_note("wiki/n.md")
    message = str(capture.value)
    for interdit in MOTS_INTERDITS:
        assert interdit not in message
    assert "401" in message
