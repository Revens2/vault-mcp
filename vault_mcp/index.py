"""Index vectoriel et lexical du vault, et sa recherche hybride.

Format sur disque, dans `VAULT_MCP_INDEX` (defaut `/opt/vault-mcp/index`) :

  vectors.npy     matrice float32 (n_fragments, 384), lignes normalisees L2
  meta.json       metadonnees alignees sur les lignes de la matrice
  backlinks.json  { note_cible: [notes_sources] }, extrait des [[wikilinks]]

Deux fichiers plutot qu'un seul JSON : l'ancien moteur relisait et reparsait un JSON
de 37,9 Mo a chaque requete. Avec le fragmentage, le nombre de vecteurs est multiplie
par ~5 ; `np.load(mmap_mode="r")` evite de tout charger en memoire.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from vault_mcp.chunk import fragmenter
from vault_mcp.embed import DIMENSIONS, vectoriser, vectoriser_un

INDEX_DEFAUT = Path("/opt/vault-mcp/index")
FICHIER_VECTEURS = "vectors.npy"
FICHIER_META = "meta.json"
FICHIER_BACKLINKS = "backlinks.json"

# Aligne sur `scripts/reindex.py` : la longueur d apercu d un fragment.
APERCU_CARACTERES = 240

# `[[Note]]`, `[[Note|alias]]`, `[[Note#ancre]]`
_WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
_MOT = re.compile(r"\w{2,}", re.UNICODE)


def repertoire_index() -> Path:
    return Path(os.environ.get("VAULT_MCP_INDEX", str(INDEX_DEFAUT)))


@dataclass(frozen=True)
class MetaFragment:
    chemin: str
    rang: int
    titre: str
    apercu: str


@dataclass(frozen=True)
class Resultat:
    chemin: str
    titre: str
    apercu: str
    score: float
    origine: str


def extraire_wikilinks(contenu: str) -> list[str]:
    return [m.group(1).strip() for m in _WIKILINK.finditer(contenu)]


def sauvegarder(
    repertoire: Path,
    vecteurs: NDArray[np.float32],
    metas: list[MetaFragment],
    backlinks: dict[str, list[str]],
) -> None:
    """Ecrit l'index de facon atomique.

    Un index a moitie ecrit, relu par le service en cours d'execution, donnerait des
    resultats silencieusement faux : on ecrit a cote puis on renomme.
    """
    if vecteurs.shape[0] != len(metas):
        raise ValueError(
            f"index incoherent : {vecteurs.shape[0]} vecteurs pour {len(metas)} metadonnees"
        )
    repertoire.mkdir(parents=True, exist_ok=True)

    tmp_vecteurs = repertoire / "vectors.tmp.npy"
    with tmp_vecteurs.open("wb") as flux:
        np.save(flux, vecteurs)
    tmp_vecteurs.replace(repertoire / FICHIER_VECTEURS)

    _ecrire_json(repertoire / FICHIER_META, [asdict(m) for m in metas])
    _ecrire_json(repertoire / FICHIER_BACKLINKS, backlinks)


def _ecrire_json(cible: Path, donnees: object) -> None:
    tmp = cible.with_suffix(cible.suffix + ".tmp")
    tmp.write_text(json.dumps(donnees, ensure_ascii=False), encoding="utf-8")
    tmp.replace(cible)


class Index:
    """Index charge en lecture seule. `vecteurs` est mappe en memoire, pas copie."""

    def __init__(self, repertoire: Path | None = None) -> None:
        self._repertoire = repertoire or repertoire_index()
        self._vecteurs: NDArray[np.float32] | None = None
        self._metas: list[MetaFragment] | None = None
        self._backlinks: dict[str, list[str]] | None = None
        self._signature: float | None = None
        self._par_note: dict[str, list[int]] | None = None
        self._noms: dict[str, list[str]] | None = None

    @property
    def disponible(self) -> bool:
        return (self._repertoire / FICHIER_VECTEURS).exists()

    def _signature_disque(self) -> float | None:
        """mtime de `vectors.npy`, ecrit en dernier par `sauvegarder()`."""
        try:
            return (self._repertoire / FICHIER_VECTEURS).stat().st_mtime
        except OSError:
            return None

    def _invalider_si_reindexe(self) -> None:
        """Vide le cache si la reindexation a remplace l'index sous nos pieds.

        Sans cela, le service sert l'ancien index jusqu'a son prochain redemarrage.
        """
        actuelle = self._signature_disque()
        if actuelle is None or actuelle == self._signature:
            return
        if self._signature is not None:
            self._vecteurs = None
            self._metas = None
            self._backlinks = None
            self._par_note = None
            self._noms = None
        self._signature = actuelle

    @property
    def vecteurs(self) -> NDArray[np.float32]:
        self._invalider_si_reindexe()
        if self._vecteurs is None:
            charge: NDArray[np.float32] = np.load(
                self._repertoire / FICHIER_VECTEURS, mmap_mode="r"
            )
            self._vecteurs = charge
        return self._vecteurs

    @property
    def metas(self) -> list[MetaFragment]:
        self._invalider_si_reindexe()
        if self._metas is None:
            brut = json.loads((self._repertoire / FICHIER_META).read_text(encoding="utf-8"))
            self._metas = [MetaFragment(**m) for m in brut]
        return self._metas

    @property
    def backlinks(self) -> dict[str, list[str]]:
        self._invalider_si_reindexe()
        if self._backlinks is None:
            fichier = self._repertoire / FICHIER_BACKLINKS
            self._backlinks = (
                json.loads(fichier.read_text(encoding="utf-8")) if fichier.exists() else {}
            )
        return self._backlinks

    @property
    def fragments_par_note(self) -> dict[str, list[int]]:
        """Index inverse note -> lignes de la matrice. Construit une seule fois."""
        self._invalider_si_reindexe()
        if self._par_note is None:
            groupes: dict[str, list[int]] = {}
            for ligne, meta in enumerate(self.metas):
                groupes.setdefault(meta.chemin, []).append(ligne)
            self._par_note = groupes
        return self._par_note

    def recherche_vectorielle(self, requete: str, limit: int = 10) -> list[Resultat]:
        if not self.disponible or len(self.metas) == 0:
            return []
        q = vectoriser_un(requete)
        # Vecteurs normalises des deux cotes : le produit scalaire EST le cosinus.
        scores = np.asarray(self.vecteurs, dtype=np.float32) @ q

        # Le score d'une note est la *moyenne* de ses fragments, pas leur maximum.
        # Avec le maximum, une note de 14 fragments a 14 tirages pour en placer un
        # haut la ou une fiche courte en a 3 : les dumps longs gagnent mecaniquement.
        # Mesure sur le jeu de reference : max 5/20, moyenne 9/20.
        classement: list[tuple[float, str, int]] = []
        for chemin, lignes in self.fragments_par_note.items():
            valeurs = scores[lignes]
            meilleur = int(lignes[int(np.argmax(valeurs))])
            classement.append((float(valeurs.mean()), chemin, meilleur))
        classement.sort(key=lambda t: -t[0])

        return [
            Resultat(
                chemin=chemin,
                titre=self.metas[ligne].titre,
                # L'apercu vient du *meilleur* fragment, pas d'un fragment moyen :
                # c'est celui-la que le lecteur veut voir.
                apercu=self.metas[ligne].apercu,
                score=score,
                origine="vecteur",
            )
            for score, chemin, ligne in classement[:limit]
        ]

    def recherche_lexicale(self, requete: str, limit: int = 10) -> list[Resultat]:
        """Comptage de mots-cles. Complement du vectoriel sur les termes rares.

        La recherche vectorielle est mauvaise sur un identifiant exact (`ORA-01555`,
        un nom de fichier) : le modele n'a jamais vu ce token. Le lexical la rattrape.
        """
        mots = {m.lower() for m in _MOT.findall(requete)}
        if not mots:
            return []
        resultats: list[Resultat] = []
        for meta in self.metas:
            corpus = f"{meta.titre} {meta.apercu} {meta.chemin}".lower()
            touches = sum(1 for mot in mots if mot in corpus)
            if touches:
                resultats.append(
                    Resultat(
                        chemin=meta.chemin,
                        titre=meta.titre,
                        apercu=meta.apercu,
                        score=touches / len(mots),
                        origine="lexical",
                    )
                )
        resultats.sort(key=lambda r: -r.score)
        return _dedupliquer(resultats, limit)

    def recherche_hybride(self, requete: str, limit: int = 10) -> list[Resultat]:
        """Fusion par rang reciproque des deux listes."""
        return fusion_rang_reciproque(
            self.recherche_vectorielle(requete, limit * 2),
            self.recherche_lexicale(requete, limit * 2),
            limit,
        )

    @property
    def noms_vers_chemins(self) -> dict[str, list[str]]:
        """Nom de note (sans extension) -> chemins. Une liste : les noms se repetent."""
        self._invalider_si_reindexe()
        if self._noms is None:
            noms: dict[str, list[str]] = {}
            for chemin in self.fragments_par_note:
                nom = chemin.rsplit("/", 1)[-1].removesuffix(".md")
                noms.setdefault(nom, []).append(chemin)
            self._noms = noms
        return self._noms

    def reindexer_note(self, chemin: str, contenu: str) -> dict[str, object]:
        """Reindexe UNE note : remplace ses fragments par ceux du contenu actuel.

        Mise a jour INCREMENTALE de l'index en place, sans reconstruction complete.
        Meme pipeline que `scripts/reindex.py` pour la note concernee
        (`fragmenter` + `vectoriser`), puis `sauvegarder` -- ecriture atomique :
        un index a moitie ecrit, relu par une autre requete, donnerait des
        resultats silencieusement faux.

        `chemin` est relatif au vault, separateurs POSIX, sans extension imposee
        par l'appelant (une note .md). `contenu` est le contenu ACTUEL de la
        note, tel que lu sur le miroir par l'appelant.

        Levee `RuntimeError` si l'index n'existe pas encore. Les lignes des
        autres notes sont conservees telles quelles (copie transitoire d'environ
        150 Mo pour 90k fragments -- acceptable pour une operation rare et
        explicite). Le cache de l'Index se revalide tout seul au prochain acces
        (signature = mtime de vectors.npy, reecrit en dernier).
        """
        if not self.disponible:
            raise RuntimeError("index absent, lancer scripts/reindex.py")
        if not isinstance(contenu, str):
            raise RuntimeError("contenu invalide")

        anciens = list(self.metas)
        avant_fragments = len(anciens)
        avant_notes = len(self.fragments_par_note)

        # 1. Fragments du contenu actuel, strictement comme scripts/reindex.py.
        fragments = fragmenter(chemin, contenu)
        textes = [f.texte for f in fragments]
        vecteurs_note = vectoriser(textes) if textes else np.empty((0, DIMENSIONS), dtype=np.float32)
        nouvelles = [
            MetaFragment(
                chemin=f.chemin,
                rang=f.rang,
                titre=f.titre,
                apercu=f.texte[:APERCU_CARACTERES].replace("\n", " "),
            )
            for f in fragments
        ]

        # 2. Lignes des AUTRES notes, conservees a l'identique.
        garde = [i for i, meta in enumerate(anciens) if meta.chemin != chemin]
        conserves = np.asarray(self.vecteurs)[garde] if garde else np.empty((0, DIMENSIONS), dtype=np.float32)
        vecteurs = np.concatenate([conserves, vecteurs_note], axis=0)
        metas = [anciens[i] for i in garde] + nouvelles

        # 3. Backlinks : on retire la note de toutes les listes, puis on
        #    rejoue sa contribution courante. Les autres notes ne changent pas.
        backlinks: dict[str, list[str]] = {}
        for cible, sources in self.backlinks.items():
            restantes = [source for source in sources if source != chemin]
            if restantes:
                backlinks[cible] = restantes
        for cible in extraire_wikilinks(contenu):
            if chemin not in backlinks.setdefault(cible, []):
                backlinks[cible].append(chemin)

        sauvegarder(self._repertoire, vecteurs, metas, backlinks)

        return {
            "etat": "applique",
            "chemin": chemin,
            "fragments_avant": avant_fragments,
            "fragments_nouveaux": len(nouvelles),
            "notes_indexees_avant": avant_notes,
            "notes_indexees_apres": len({meta.chemin for meta in metas}),
            "cibles_backlinks": len(backlinks),
        }

    def contexte_graphe(self, chemin: str, limite: int = 50) -> dict[str, object]:
        """Backlinks, liens sortants et voisins immediats d'une note."""
        nom = chemin.rsplit("/", 1)[-1].removesuffix(".md")

        entrants = [c for c in self.backlinks.get(nom, []) if c != chemin][:limite]

        sortants: list[dict[str, object]] = []
        for cible, sources in self.backlinks.items():
            if chemin not in sources:
                continue
            resolus = self.noms_vers_chemins.get(cible, [])
            sortants.append(
                {
                    "cible": cible,
                    "chemins": resolus[:3],
                    # Un wikilink vers une note inexistante est courant dans Obsidian.
                    # Le signaler vaut mieux que le taire : c'est souvent une note a ecrire.
                    "resolu": bool(resolus),
                }
            )
            if len(sortants) >= limite:
                break

        return {
            "chemin": chemin,
            "existe": chemin in self.fragments_par_note,
            "backlinks": entrants,
            "nb_backlinks": len(self.backlinks.get(nom, [])),
            "liens_sortants": sortants,
            "nb_liens_sortants": len(sortants),
        }


def fusion_rang_reciproque(
    a: list[Resultat], b: list[Resultat], limit: int, k: int = 60
) -> list[Resultat]:
    """RRF : score = somme de 1/(k + rang). Insensible a l'echelle des scores.

    Fusionner des scores bruts serait faux : un cosinus vit dans [-1, 1], un
    comptage de mots-cles dans [0, 1] avec une toute autre distribution.
    """
    cumul: dict[str, float] = {}
    vus: dict[str, Resultat] = {}
    for liste in (a, b):
        for rang, resultat in enumerate(liste):
            cumul[resultat.chemin] = cumul.get(resultat.chemin, 0.0) + 1.0 / (k + rang + 1)
            vus.setdefault(resultat.chemin, resultat)
    ordonnes = sorted(cumul.items(), key=lambda kv: -kv[1])
    return [
        Resultat(
            chemin=chemin,
            titre=vus[chemin].titre,
            apercu=vus[chemin].apercu,
            score=score,
            origine="hybride",
        )
        for chemin, score in ordonnes[:limit]
    ]


def _dedupliquer(resultats: list[Resultat], limit: int) -> list[Resultat]:
    """Garde le meilleur fragment par note : sinon une note longue occupe tout le top."""
    vus: set[str] = set()
    sortie: list[Resultat] = []
    for r in resultats:
        if r.chemin in vus:
            continue
        vus.add(r.chemin)
        sortie.append(r)
        if len(sortie) >= limit:
            break
    return sortie


__all__ = [
    "DIMENSIONS",
    "Index",
    "MetaFragment",
    "Resultat",
    "extraire_wikilinks",
    "fusion_rang_reciproque",
    "repertoire_index",
    "sauvegarder",
]
