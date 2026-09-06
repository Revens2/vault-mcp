"""Tests de `vault_mcp.spool` (adr/0020).

Le spool est le seul moyen d ecrire dont dispose le service expose a internet.
Deux proprietes sont non negociables et testees ici : l atomicite du depot (le
pousseur ne doit jamais voir une intention partielle) et l ordre FIFO (deux
ecritures sur la meme note doivent s appliquer dans l ordre d arrivee, sans
quoi la plus ancienne gagnerait).
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from vault_mcp.spool import Spool, SpoolError


@pytest.fixture
def spool(tmp_path: Path) -> Spool:
    for sous in ("tmp", "queue", "done", "failed"):
        (tmp_path / sous).mkdir()
    return Spool(str(tmp_path))


def _intentions(spool: Spool) -> list[dict[str, Any]]:
    return [
        json.loads(f.read_text(encoding="utf-8")) for f in sorted(spool.queue.glob("*.json"))
    ]


# --- Depot -----------------------------------------------------------------
def test_depot_atterrit_dans_queue_et_pas_dans_tmp(spool: Spool) -> None:
    identifiant = spool.deposer("create", "wiki/n.md", contenu="bonjour")

    fichiers = list(spool.queue.glob("*.json"))
    assert len(fichiers) == 1
    assert identifiant in fichiers[0].name
    # `tmp/` doit etre vide : un residu signifie un depot interrompu.
    assert list(spool.tmp.iterdir()) == []


def test_le_contenu_est_restitue_a_l_identique(spool: Spool) -> None:
    contenu = "# Titre\n\nUn corps avec des accents : éàü, et un [[lien]].\n"
    spool.deposer("create", "wiki/n.md", contenu=contenu)

    (intention,) = _intentions(spool)
    assert base64.b64decode(intention["contenu_b64"]).decode("utf-8") == contenu


def test_schema_complet(spool: Spool) -> None:
    identifiant = spool.deposer(
        "move",
        "wiki/a.md",
        path_cible="archive/a.md",
        sha256_attendu="abc",
        client_id="client-42",
        lot="lot-1",
    )
    (intention,) = _intentions(spool)
    assert intention == {
        "version": 1,
        "id": identifiant,
        "op": "move",
        "path": "wiki/a.md",
        "path_cible": "archive/a.md",
        "contenu_b64": None,
        "sha256_attendu": "abc",
        "horodatage": intention["horodatage"],
        "client_id": "client-42",
        "lot": "lot-1",
    }
    assert intention["horodatage"].endswith("Z")


def test_operation_inconnue_refusee(spool: Spool) -> None:
    with pytest.raises(SpoolError):
        spool.deposer("rm -rf", "wiki/n.md", contenu="x")
    assert list(spool.queue.iterdir()) == []


def test_spool_absent_refuse_le_depot(tmp_path: Path) -> None:
    absent = Spool(str(tmp_path / "nexistepas"))
    assert absent.disponible is False
    with pytest.raises(SpoolError):
        absent.deposer("create", "wiki/n.md", contenu="x")


# --- Atomicite -------------------------------------------------------------
def test_echec_de_replace_ne_laisse_rien_dans_queue(
    spool: Spool, monkeypatch: pytest.MonkeyPatch
) -> None:
    def replace_casse(*_args: object, **_kwargs: object) -> None:
        raise OSError("disque plein")

    monkeypatch.setattr("vault_mcp.spool.os.replace", replace_casse)

    with pytest.raises(SpoolError):
        spool.deposer("create", "wiki/n.md", contenu="bonjour")

    # Rien dans la file : le pousseur ne verra jamais cette intention.
    assert list(spool.queue.iterdir()) == []
    # Le residu reste dans tmp/, que le pousseur ignore.
    assert len(list(spool.tmp.iterdir())) == 1


def test_le_message_d_erreur_ne_contient_pas_le_contenu(
    spool: Spool, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "ceci-est-un-mot-de-passe-du-vault"

    def replace_casse(*_args: object, **_kwargs: object) -> None:
        raise OSError(f"echec sur {secret}")

    monkeypatch.setattr("vault_mcp.spool.os.replace", replace_casse)

    with pytest.raises(SpoolError) as capture:
        spool.deposer("create", "wiki/n.md", contenu=secret)
    assert secret not in str(capture.value)


# --- Ordre FIFO ------------------------------------------------------------
def test_l_ordre_lexicographique_est_l_ordre_chronologique(spool: Spool) -> None:
    ids = [spool.deposer("create", f"wiki/n{i}.md", contenu=str(i)) for i in range(50)]
    lus = [json.loads(f.read_text(encoding="utf-8"))["id"] for f in sorted(spool.queue.glob("*"))]
    assert lus == ids


def test_collision_a_la_nanoseconde_ne_perd_aucune_intention(
    spool: Spool, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Horloge figee : seul l UUID distingue les deux depots. Sans lui, le second
    # ecraserait silencieusement le premier.
    monkeypatch.setattr("vault_mcp.spool.time.time_ns", lambda: 1_700_000_000_000_000_000)

    premier = spool.deposer("create", "wiki/a.md", contenu="a")
    second = spool.deposer("create", "wiki/b.md", contenu="b")

    assert premier != second
    assert len(list(spool.queue.glob("*.json"))) == 2


# --- Etat ------------------------------------------------------------------
def test_etat_en_attente_puis_applique(spool: Spool) -> None:
    identifiant = spool.deposer("create", "wiki/n.md", contenu="x")
    assert spool.etat(identifiant)["etat"] == "en_attente"

    (spool.done / f"{identifiant}.json").write_text(
        json.dumps({"resultat": "ok", "fini_a": "2026-09-05T10:00:00Z"}), encoding="utf-8"
    )
    etat = spool.etat(identifiant)
    assert etat["etat"] == "applique"
    assert etat["fini_a"] == "2026-09-05T10:00:00Z"


def test_etat_echec_porte_le_motif(spool: Spool) -> None:
    identifiant = "0123456789ab"
    (spool.failed / f"{identifiant}.json").write_text(
        json.dumps({"motif": "conflit"}), encoding="utf-8"
    )
    assert spool.etat(identifiant) == {"etat": "echec", "motif": "conflit"}


def test_etat_ne_renvoie_jamais_le_contenu(spool: Spool) -> None:
    identifiant = "0123456789ab"
    (spool.failed / f"{identifiant}.json").write_text(
        json.dumps({"motif": "conflit", "contenu_b64": "c2VjcmV0"}), encoding="utf-8"
    )
    assert "contenu_b64" not in spool.etat(identifiant)


def test_etat_d_un_id_inconnu_ne_leve_pas(spool: Spool) -> None:
    assert spool.etat("aaaaaaaaaaaa") == {"etat": "inconnu"}


@pytest.mark.parametrize("identifiant", ["../../etc/passwd", "", "ZZZZ", "0123456789abZZ"])
def test_etat_rejette_un_identifiant_non_hexadecimal(spool: Spool, identifiant: str) -> None:
    # L id vient du reseau et finit dans un nom de fichier : sans filtre,
    # `etat("../x")` sonderait l existence de fichiers hors du spool.
    assert spool.etat(identifiant)["etat"] == "inconnu"


def test_recu_illisible_ne_fait_pas_tomber_l_appel(spool: Spool) -> None:
    identifiant = "0123456789ab"
    (spool.done / f"{identifiant}.json").write_text("{ pas du json", encoding="utf-8")
    assert spool.etat(identifiant) == {"etat": "applique", "motif": "recu illisible"}


# --- Statistiques ----------------------------------------------------------
def test_statistiques(spool: Spool) -> None:
    spool.deposer("create", "wiki/a.md", contenu="a")
    spool.deposer("create", "wiki/b.md", contenu="b")
    (spool.failed / "0123456789ab.json").write_text("{}", encoding="utf-8")

    stats = spool.statistiques()
    assert stats["disponible"] is True
    assert stats["en_attente"] == 2
    assert stats["echecs"] == 1
    assert stats["appliquees"] == 0
    assert stats["plus_vieux_s"] is not None


def test_statistiques_sans_spool(tmp_path: Path) -> None:
    assert Spool(str(tmp_path / "absent")).statistiques() == {"disponible": False}


def test_statistiques_separent_historique_et_sante_courante(spool: Spool) -> None:
    """Un stock ancien de conflits ne doit pas ressembler a une panne active :
    `echecs` reste le total, `echecs_recents`/`echec_recent_s` portent la sante."""
    vieux = spool.failed / "aaaa11111111.json"
    vieux.write_text("{}", encoding="utf-8")
    ancien = time.time() - 10 * 24 * 3600  # 10 jours
    os.utime(vieux, (ancien, ancien))

    recent = spool.failed / "bbbb22222222.json"
    recent.write_text("{}", encoding="utf-8")

    stats = spool.statistiques()
    assert stats["echecs"] == 2                 # total historique conserve
    assert stats["echecs_recents"] == 1         # seul le receipt recent compte
    assert stats["echec_recent_s"] is not None
    assert stats["echec_recent_s"] < 60         # age du plus recent echec
