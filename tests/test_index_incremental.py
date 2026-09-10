"""Batch incremental, verrou writer, file durable et coherence de publication.

Ces tests couvrent la mission du 2026-09-10 : remplacer « une ecriture de note ->
un FULL rebuild » par un lot incremental, sans jamais perdre une modification ni
publier une generation incoherente.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import time
from pathlib import Path

import numpy as np
import pytest

from vault_mcp import dirty
from vault_mcp.embed import DIMENSIONS
from vault_mcp.index import (
    FICHIER_BACKLINKS,
    FICHIER_META,
    FICHIER_VECTEURS,
    IGNORER,
    Index,
    MetaFragment,
    VerrouOccupe,
    sauvegarder,
    verrou_writers,
)


def _index_initial(repertoire: Path, chemins: dict[str, str]) -> Index:
    metas = [MetaFragment(chemin=c, rang=0, titre=c, apercu=t) for c, t in chemins.items()]
    vecteurs = np.zeros((len(metas), DIMENSIONS), dtype=np.float32)
    vecteurs[:, 0] = 1.0
    sauvegarder(repertoire, vecteurs, metas, {})
    return Index(repertoire)


# ------------------------------------------------------- coherence de publication


def test_meta_json_est_le_seul_point_de_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un seul `replace()` publie la generation, et c'est celui de meta.json.

    Les vecteurs et les backlinks portent leur generation dans leur nom : ils sont
    ecrits sous leur nom definitif, personne ne les designe avant le commit.
    """
    bascules: list[str] = []
    original = Path.replace

    def espion(self: Path, cible):  # type: ignore[no-untyped-def]
        bascules.append(Path(cible).name)
        return original(self, cible)

    monkeypatch.setattr(Path, "replace", espion)
    _index_initial(tmp_path, {"a.md": "alpha"})

    assert bascules == [FICHIER_META]


def test_un_crash_avant_le_commit_laisse_la_generation_precedente_intacte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C'est LE cas que trois `replace()` successifs ne savaient pas couvrir."""
    index = _index_initial(tmp_path, {"a.md": "alpha", "b.md": "beta"})
    generation = index._courant().generation

    original = Path.replace

    def echouer(self: Path, cible):  # type: ignore[no-untyped-def]
        if Path(cible).name == FICHIER_META:
            raise OSError("crash simule juste avant le commit")
        return original(self, cible)

    monkeypatch.setattr(Path, "replace", echouer)
    with pytest.raises(OSError):
        index.reindexer_notes({"c.md": "# Gamma\ncontenu"})
    monkeypatch.undo()

    relu = Index(tmp_path)
    assert relu._courant().generation == generation
    assert {m.chemin for m in relu.metas} == {"a.md", "b.md"}


def test_les_generations_obsoletes_sont_purgees(tmp_path: Path) -> None:
    index = _index_initial(tmp_path, {"a.md": "alpha"})
    index.reindexer_notes({"a.md": "# Alpha\nnouvelle version"})
    assert len(list(tmp_path.glob("vectors.*.npy"))) == 1
    assert len(list(tmp_path.glob("backlinks.*.json"))) == 1
    assert not (tmp_path / FICHIER_VECTEURS).exists()
    assert not (tmp_path / FICHIER_BACKLINKS).exists()


def test_aucun_temporaire_ne_survit_a_la_publication(tmp_path: Path) -> None:
    _index_initial(tmp_path, {"a.md": "alpha"})
    assert not list(tmp_path.glob("*.tmp.*"))


def test_format_herite_reste_lisible(tmp_path: Path) -> None:
    """L'index deja en place ne doit pas devenir illisible au deploiement."""
    import numpy as _np

    vecteurs = _np.zeros((2, DIMENSIONS), dtype=_np.float32)
    vecteurs[:, 0] = 1.0
    _np.save(tmp_path / FICHIER_VECTEURS, vecteurs)
    (tmp_path / FICHIER_META).write_text(
        json.dumps(
            [
                {"chemin": "a.md", "rang": 0, "titre": "a", "apercu": "a"},
                {"chemin": "b.md", "rang": 0, "titre": "b", "apercu": "b"},
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / FICHIER_BACKLINKS).write_text(json.dumps({"a.md": ["b.md"]}), encoding="utf-8")

    index = Index(tmp_path)
    assert {m.chemin for m in index.metas} == {"a.md", "b.md"}
    assert index.backlinks == {"a.md": ["b.md"]}
    # Et la premiere publication bascule le format sans perdre les autres notes.
    index.reindexer_notes({"a.md": "# A\ncontenu"})
    assert {m.chemin for m in Index(tmp_path).metas} == {"a.md", "b.md"}
    assert Index(tmp_path)._courant().generation > 0


def test_le_chargement_refuse_un_index_desaligne(tmp_path: Path) -> None:
    """Index herite laisse incoherent : echouer bruyamment plutot que classer faux."""
    import numpy as _np

    vecteurs = _np.zeros((2, DIMENSIONS), dtype=_np.float32)
    _np.save(tmp_path / FICHIER_VECTEURS, vecteurs)
    (tmp_path / FICHIER_META).write_text(
        json.dumps([{"chemin": "a.md", "rang": 0, "titre": "a", "apercu": "a"}]),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="incoherent"):
        _ = Index(tmp_path).metas


# --------------------------------------------------------------- batch incremental


def test_lot_de_plusieurs_notes_publie_une_seule_generation(tmp_path: Path) -> None:
    index = _index_initial(tmp_path, {"a.md": "alpha", "b.md": "beta"})
    avant = (tmp_path / FICHIER_META).stat().st_mtime_ns

    resultat = index.reindexer_notes(
        {
            "a.md": "# Alpha\nPare-feu et reseau.",
            "c.md": "# Gamma\nRecette de tarte.",
        }
    )
    apres = (tmp_path / FICHIER_META).stat().st_mtime_ns

    assert resultat["etat"] == "applique"
    assert resultat["notes"] == 2
    assert apres != avant
    chemins = {m.chemin for m in Index(tmp_path).metas}
    assert chemins == {"a.md", "b.md", "c.md"}


def test_contenu_none_retire_la_note(tmp_path: Path) -> None:
    """Une suppression retire les lignes ; elle n'est PAS simulee par une note vide."""
    index = _index_initial(tmp_path, {"a.md": "alpha", "b.md": "beta"})
    resultat = index.reindexer_notes({"a.md": None})
    assert resultat["notes_supprimees"] == 1
    assert {m.chemin for m in Index(tmp_path).metas} == {"b.md"}


def test_renommage_par_retrait_et_ajout(tmp_path: Path) -> None:
    index = _index_initial(tmp_path, {"vieux.md": "contenu"})
    index.reindexer_notes({"vieux.md": None, "neuf.md": "# Neuf\ncontenu deplace"})
    assert {m.chemin for m in Index(tmp_path).metas} == {"neuf.md"}


def test_last_write_wins_dans_un_lot(tmp_path: Path) -> None:
    """Le dictionnaire porte la deduplication : une note n'est vectorisee qu'une fois."""
    index = _index_initial(tmp_path, {"a.md": "marqueur-v1"})
    index.reindexer_notes({"a.md": "# Version finale\ntexte de la derniere version"})
    metas = [m for m in Index(tmp_path).metas if m.chemin == "a.md"]
    assert metas
    # Aucune trace de la version precedente : les anciennes lignes sont remplacees,
    # pas empilees.
    assert all("marqueur-v1" not in m.apercu for m in metas)


def test_backlinks_du_lot_sont_coherents(tmp_path: Path) -> None:
    index = _index_initial(tmp_path, {"a.md": "x", "b.md": "y"})
    index.reindexer_notes({"a.md": "Voir [[Cible]]", "b.md": "Voir aussi [[Cible]]"})
    backlinks = Index(tmp_path).backlinks
    assert set(backlinks["Cible"]) == {"a.md", "b.md"}
    # Retirer une source la fait disparaitre des listes, sans toucher l'autre.
    index.reindexer_notes({"a.md": None})
    assert Index(tmp_path).backlinks["Cible"] == ["b.md"]


def test_les_contenus_sont_lus_sous_verrou(tmp_path: Path) -> None:
    """Le lecteur ne doit etre appele qu'une fois le verrou writer obtenu.

    Un worker qui lit avant d'attendre la fin d'un full de 2 h republierait un etat
    vieux de deux heures par-dessus le travail du full.
    """
    index = _index_initial(tmp_path, {"a.md": "alpha"})
    appels: list[bool] = []

    def lecteur(chemin: str) -> str:
        # Le verrou est detenu : une tentative concurrente doit echouer.
        with pytest.raises(VerrouOccupe):
            with verrou_writers(tmp_path):
                pass
        appels.append(True)
        return "# Alpha\ncontenu lu sous verrou"

    index.reindexer_chemins(["a.md"], lecteur)
    assert appels == [True]


def test_ignorer_laisse_la_note_en_place(tmp_path: Path) -> None:
    """Note illisible : ni retiree, ni remplacee. `IGNORER` n'est pas `None`."""
    index = _index_initial(tmp_path, {"a.md": "alpha", "b.md": "beta"})
    resultat = index.reindexer_chemins(["a.md"], lambda _: IGNORER)
    assert resultat["etat"] == "sans_objet"
    assert resultat["ignores"] == ["a.md"]
    assert {m.chemin for m in Index(tmp_path).metas} == {"a.md", "b.md"}


def test_un_chemin_illisible_est_signale_sans_bloquer_le_lot(tmp_path: Path) -> None:
    """Echec partiel : le reste du lot publie, le chemin fautif est rapporte.

    L'appelant doit pouvoir ne PAS l'acquitter : l'acquitter perdrait sa
    modification jusqu'au full quotidien.
    """
    index = _index_initial(tmp_path, {"a.md": "alpha", "b.md": "beta"})

    def lecteur(chemin: str):  # type: ignore[no-untyped-def]
        return IGNORER if chemin == "a.md" else "# B\ncontenu lisible"

    resultat = index.reindexer_chemins(["a.md", "b.md"], lecteur)
    assert resultat["etat"] == "applique"
    assert resultat["ignores"] == ["a.md"]
    assert resultat["notes"] == 1
    assert {m.chemin for m in Index(tmp_path).metas} == {"a.md", "b.md"}


def test_lot_vide_ne_publie_rien(tmp_path: Path) -> None:
    index = _index_initial(tmp_path, {"a.md": "alpha"})
    avant = (tmp_path / FICHIER_META).stat().st_mtime_ns
    assert index.reindexer_notes({})["etat"] == "sans_objet"
    assert (tmp_path / FICHIER_META).stat().st_mtime_ns == avant


# ------------------------------------------------------------------ verrou writer


def _tenir_le_verrou(repertoire: str, pret, relacher) -> None:  # pragma: no cover - sous-process
    with verrou_writers(Path(repertoire)):
        pret.set()
        relacher.wait(30)


def test_un_second_writer_est_refuse_et_ne_force_pas(tmp_path: Path) -> None:
    index = _index_initial(tmp_path, {"a.md": "alpha"})
    contexte = multiprocessing.get_context("fork")
    pret = contexte.Event()
    relacher = contexte.Event()
    autre = contexte.Process(target=_tenir_le_verrou, args=(str(tmp_path), pret, relacher))
    autre.start()
    try:
        assert pret.wait(30)
        with pytest.raises(VerrouOccupe):
            index.reindexer_notes({"a.md": "nouvelle version"})
    finally:
        relacher.set()
        autre.join(30)

    # Verrou rendu : le meme lot passe.
    assert index.reindexer_notes({"a.md": "nouvelle version"})["etat"] == "applique"


def test_lattente_du_verrou_est_bornee(tmp_path: Path) -> None:
    _index_initial(tmp_path, {"a.md": "alpha"})
    index = Index(tmp_path)
    contexte = multiprocessing.get_context("fork")
    pret = contexte.Event()
    relacher = contexte.Event()
    autre = contexte.Process(target=_tenir_le_verrou, args=(str(tmp_path), pret, relacher))
    autre.start()
    try:
        assert pret.wait(30)
        debut = time.monotonic()
        with pytest.raises(VerrouOccupe):
            index.reindexer_notes({"a.md": "x"}, attente_verrou_s=2)
        assert 1.5 <= time.monotonic() - debut < 15
    finally:
        relacher.set()
        autre.join(30)


# -------------------------------------------------------------- file durable


@pytest.fixture()
def file_sale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    racine = tmp_path / "dirty"
    monkeypatch.setenv("VAULT_MCP_DIRTY", str(racine))
    (racine / "queue").mkdir(parents=True)
    (racine / "inflight").mkdir(parents=True)
    (racine / "differe").mkdir(parents=True)
    return racine


def test_salir_est_idempotent(file_sale: Path) -> None:
    dirty.salir(["notes/a.md"] * 5)
    dirty.salir(["notes/a.md"])
    assert dirty.taille() == 1


def test_reclamer_puis_acquitter_vide_la_file(file_sale: Path) -> None:
    dirty.salir(["a.md", "b.md"])
    jeton, reclames = dirty.reclamer()
    assert set(reclames) == {"a.md", "b.md"}
    assert dirty.taille() == 0
    dirty.acquitter(jeton)
    assert not list((file_sale / "inflight").iterdir())


def test_une_ecriture_pendant_le_traitement_nest_pas_acquittee(file_sale: Path) -> None:
    """Course d'acquittement : le worker traite r1, r2 arrive, il acquitte r1.

    Si l'acquittement effacait « le chemin » plutot que « le lot reclame »,
    la revision r2 disparaitrait sans jamais etre indexee.
    """
    dirty.salir(["a.md"])
    jeton, _ = dirty.reclamer()
    dirty.salir(["a.md"])  # nouvelle ecriture pendant le traitement
    dirty.acquitter(jeton)
    assert dirty.taille() == 1


def test_un_lot_orphelin_retourne_dans_la_file(file_sale: Path) -> None:
    dirty.salir(["a.md", "b.md"])
    dirty.reclamer()  # jeton perdu = worker tue
    assert dirty.taille() == 0
    assert dirty.recuperer() == 2
    assert dirty.taille() == 2


def test_rendre_remet_le_lot(file_sale: Path) -> None:
    dirty.salir(["a.md"])
    jeton, _ = dirty.reclamer()
    assert dirty.rendre(jeton) == 1
    assert dirty.taille() == 1


def test_un_chemin_differe_nest_pas_repris_par_la_file_surveillee(file_sale: Path) -> None:
    """`differe/` n'est PAS `queue/` : le `.path` unit ne doit pas le voir.

    Une note durablement illisible remise dans `queue/` ferait boucler le worker.
    """
    dirty.differer(["illisible.md"])
    assert dirty.taille() == 0
    assert dirty.reprendre_differes() == 1
    assert dirty.taille() == 1
    assert dirty.reprendre_differes() == 0


def test_le_lot_est_borne(file_sale: Path) -> None:
    dirty.salir([f"note-{i}.md" for i in range(10)])
    jeton, reclames = dirty.reclamer(maximum=3)
    assert len(reclames) == 3
    assert dirty.taille() == 7
    dirty.acquitter(jeton)
