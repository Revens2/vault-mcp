"""Index vectoriel et lexical du vault, et sa recherche hybride.

Format sur disque, dans `VAULT_MCP_INDEX` (defaut `/opt/vault-mcp/index`) :

  meta.json              manifeste + metadonnees alignees sur les lignes de la matrice.
                         { generation, vecteurs, backlinks, fragments: [...] }
  vectors.<gen>.npy      matrice float32 (n_fragments, 384), lignes normalisees L2
  backlinks.<gen>.json   { note_cible: [notes_sources] }, extrait des [[wikilinks]]
  .writers.lock          verrou EXCLUSIF de tout writer, sur read -> modify -> publish
  .publish.lock          verrou de la bascule : EXCLUSIF writer, PARTAGE lecteur

Plusieurs fichiers plutot qu'un seul JSON : l'ancien moteur relisait et reparsait un
JSON de 37,9 Mo a chaque requete. Avec le fragmentage, le nombre de vecteurs est
multiplie par ~5 ; `np.load(mmap_mode="r")` evite de tout charger en memoire.

COHERENCE DE LA GENERATION (mission 2026-09-10)
-----------------------------------------------
Avant : trois fichiers a noms fixes, publies par trois `replace()` successifs, et
`vectors.npy` -- ECRIT EN PREMIER -- servait de marqueur de generation aux lecteurs.
Deux defauts distincts en decoulaient :
  - un lecteur pouvait charger des vecteurs N+1 avec des metadonnees N, et
    `recherche_vectorielle` indexait alors `scores[lignes]` avec des lignes d'une
    autre generation : resultats faux ou `IndexError` ;
  - un crash entre le premier et le troisieme `replace()` laissait un triplet
    mi-N mi-N+1 que rien ne detectait quand le nombre de lignes coincidait.

`replace()` est atomique fichier par fichier, jamais pour un groupe. La coherence
vient donc de trois controles, chacun couvrant un cas que les autres ne couvrent pas :
  1. UN SEUL point de commit : les vecteurs et les backlinks portent leur generation
     dans leur nom, et `meta.json` -- qui les designe -- bascule en dernier et seul.
     Un crash avant sa bascule laisse la generation precedente complete ; un crash
     apres ne laisse que des orphelins, purges a la publication suivante ;
  2. `.publish.lock` : le writer tient l'exclusif le temps de la bascule et de la
     purge, le lecteur tient le partage pendant tout son chargement. Sans lui, la
     purge pourrait retirer un fichier qu'un lecteur froid s'apprete a ouvrir ;
  3. `Instantane` : une recherche epingle une generation pour toute sa duree, au
     lieu de re-tester le disque a chaque propriete touchee.

Le format herite (meta.json = simple liste, `vectors.npy` et `backlinks.json` a noms
fixes) reste lisible : la bascule se fait a la premiere publication du nouveau code.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from vault_mcp.autorite import historique, rang_autorite
from vault_mcp.chunk import fragmenter
from vault_mcp.embed import DIMENSIONS, vectoriser, vectoriser_un
from vault_mcp.lexical import IndexBM25
from vault_mcp.selection import indexable, notes, repertoire_vault

INDEX_DEFAUT = Path("/opt/vault-mcp/index")
FICHIER_VECTEURS = "vectors.npy"
FICHIER_META = "meta.json"
FICHIER_BACKLINKS = "backlinks.json"
FICHIER_VERROU_WRITERS = ".writers.lock"
FICHIER_VERROU_PUBLICATION = ".publish.lock"


def _nom_vecteurs(generation: int) -> str:
    return f"vectors.{generation}.npy"


def _nom_backlinks(generation: int) -> str:
    return f"backlinks.{generation}.json"


# Aligne sur `scripts/reindex.py` : la longueur d apercu d un fragment.
APERCU_CARACTERES = 240

# `[[Note]]`, `[[Note|alias]]`, `[[Note#ancre]]`
_WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
_MOT = re.compile(r"\w{2,}", re.UNICODE)

# Nombre de rechargements consecutifs toleres avant de declarer l'index incoherent.
_ESSAIS_CHARGEMENT = 4

# Sentinelle rendue par un lecteur de contenu : « je ne sais pas lire cette note,
# laisse-la telle quelle dans l'index ». Distinct de `None`, qui veut dire
# « retire-la » -- confondre les deux effacerait une note valide sur une erreur
# d'entree/sortie transitoire.
IGNORER: object = object()


class VerrouOccupe(RuntimeError):
    """Un autre writer detient le verrou. L'appelant doit differer, pas forcer."""


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


# --------------------------------------------------------------------- verrous


def _ouvrir_verrou(repertoire: Path, nom: str) -> int:
    """Ouvre (et cree) le fichier de verrou. Jamais supprime, jamais tronque :
    remplacer l'inode ferait travailler deux processus sur deux verrous distincts."""
    repertoire.mkdir(parents=True, exist_ok=True)
    return os.open(repertoire / nom, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o660)


def _prendre(descripteur: int, operation: int, attente_s: float | None) -> bool:
    """`flock` non bloquant, eventuellement re-essaye jusqu'a `attente_s`.

    Pas de `flock` bloquant : un writer bloque 2 h sur le full quotidien
    immobiliserait la boucle asyncio du MCP. On sonde, on rend la main.
    """
    limite = None if attente_s is None else time.monotonic() + attente_s
    while True:
        try:
            fcntl.flock(descripteur, operation | fcntl.LOCK_NB)
            return True
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                raise
            if limite is None or time.monotonic() >= limite:
                return False
            time.sleep(0.5)


@contextlib.contextmanager
def verrou_writers(repertoire: Path | None = None, attente_s: float | None = None) -> Iterator[None]:
    """Verrou EXCLUSIF de tout writer de l'index, sur read -> modify -> publish.

    Il rend impossible le scenario de perte silencieuse :
      le full lit la generation N, l'incremental publie N+1, le full publie N.

    `attente_s=None` : echec immediat (`VerrouOccupe`). C'est le mode des writers
    interactifs et du worker -- ils n'attendent pas derriere un full de 2 h, ils
    laissent leurs chemins dans la file durable et repassent plus tard.
    """
    repertoire = repertoire or repertoire_index()
    descripteur = _ouvrir_verrou(repertoire, FICHIER_VERROU_WRITERS)
    try:
        if not _prendre(descripteur, fcntl.LOCK_EX, attente_s):
            raise VerrouOccupe("un autre writer de l'index est en cours")
        try:
            yield
        finally:
            fcntl.flock(descripteur, fcntl.LOCK_UN)
    finally:
        os.close(descripteur)


@contextlib.contextmanager
def _verrou_publication(repertoire: Path, operation: int) -> Iterator[None]:
    descripteur = _ouvrir_verrou(repertoire, FICHIER_VERROU_PUBLICATION)
    try:
        # Attente bornee genereusement : cote writer on attend au pire la duree du
        # chargement d'un lecteur (~1 s), cote lecteur au pire trois `replace()`.
        if not _prendre(descripteur, operation, 60.0):
            raise RuntimeError("verrou de publication de l'index non obtenu")
        try:
            yield
        finally:
            fcntl.flock(descripteur, fcntl.LOCK_UN)
    finally:
        os.close(descripteur)


# ---------------------------------------------------------------- publication


def _fsync_repertoire(repertoire: Path) -> None:
    descripteur = os.open(repertoire, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(descripteur)
    finally:
        os.close(descripteur)


# L'index contient le vault en clair. Les droits sont poses EXPLICITEMENT et non
# laisses au umask du writer : le service, le full et le worker ont trois unites
# systemd distinctes, donc trois umask possibles, et un fichier publie en 0664 par
# l'un rendrait tout le vault lisible par n'importe quel compte de la machine.
MODE_INDEX = 0o600


def _ecrire_npy(cible: Path, vecteurs: NDArray[np.float32]) -> None:
    with cible.open("wb") as flux:
        np.save(flux, vecteurs)
        flux.flush()
        os.fsync(flux.fileno())
    os.chmod(cible, MODE_INDEX)


def _ecrire_json(cible: Path, donnees: object) -> None:
    with cible.open("w", encoding="utf-8") as flux:
        flux.write(json.dumps(donnees, ensure_ascii=False))
        flux.flush()
        os.fsync(flux.fileno())
    os.chmod(cible, MODE_INDEX)


def _generation_courante(repertoire: Path) -> int:
    """Generation publiee, ou 0 si l'index est absent ou au format herite."""
    try:
        with (repertoire / FICHIER_META).open("rb") as flux:
            debut = flux.read(200).decode("utf-8", "ignore")
    except OSError:
        return 0
    marque = '"generation":'
    position = debut.find(marque)
    if position < 0:
        return 0
    reste = debut[position + len(marque) :].strip()
    chiffres = ""
    for caractere in reste:
        if caractere.isdigit():
            chiffres += caractere
        else:
            break
    return int(chiffres) if chiffres else 0


def _purger_generations(repertoire: Path, generation: int) -> None:
    """Supprime les fichiers des generations qui ne sont plus referencees.

    Appelee VERROU DE PUBLICATION EXCLUSIF TENU : aucun lecteur ne peut etre en
    train de resoudre un nom de fichier a cet instant. Un lecteur qui a deja
    ouvert l'ancien `vectors.<gen>.npy` garde son mapping valide -- l'inode
    survit a la disparition de son nom.
    """
    garder = {_nom_vecteurs(generation), _nom_backlinks(generation)}
    for motif in ("vectors.*.npy", "backlinks.*.json", FICHIER_VECTEURS, FICHIER_BACKLINKS):
        for fichier in repertoire.glob(motif):
            if fichier.name in garder or ".tmp." in fichier.name:
                continue
            with contextlib.suppress(OSError):
                fichier.unlink()


def sauvegarder(
    repertoire: Path,
    vecteurs: NDArray[np.float32],
    metas: list[MetaFragment],
    backlinks: dict[str, list[str]],
) -> None:
    """Publie une generation complete de l'index, en UN SEUL point de bascule.

    POURQUOI PAS TROIS `replace()`
    ------------------------------
    `replace()` est atomique fichier par fichier, jamais pour les trois ensemble.
    Un crash entre le premier et le troisieme laissait un triplet mi-N mi-N+1 que
    rien ne detectait quand le nombre de lignes coincidait -- index silencieusement
    faux, jusqu'au full suivant.

    Les vecteurs et les backlinks portent donc le numero de generation dans leur
    NOM, et `meta.json` -- qui les designe -- est le seul point de commit. Un crash
    avant sa bascule laisse la generation precedente intacte et complete ; un crash
    apres ne laisse que des fichiers orphelins, purges a la publication suivante.

    Chaque writer ecrit dans des temporaires qui lui sont propres (pid + uuid) :
    un nom fixe partage laisserait deux writers ecrire dans le meme inode, dont
    l'un vient d'etre publie par l'autre.
    """
    if vecteurs.shape[0] != len(metas):
        raise ValueError(
            f"index incoherent : {vecteurs.shape[0]} vecteurs pour {len(metas)} metadonnees"
        )
    repertoire.mkdir(parents=True, exist_ok=True)

    # Identifiant strictement croissant : `time.time_ns()` seul reculerait si
    # l'horloge recule, et deux publications d'une meme nanoseconde sont
    # theoriquement possibles.
    generation = max(time.time_ns(), _generation_courante(repertoire) + 1)
    jeton = f"{os.getpid()}.{uuid.uuid4().hex[:8]}"
    fichier_vecteurs = repertoire / _nom_vecteurs(generation)
    fichier_backlinks = repertoire / _nom_backlinks(generation)
    tmp_meta = repertoire / f"meta.tmp.{jeton}.json"
    try:
        _ecrire_npy(fichier_vecteurs, vecteurs)
        _ecrire_json(fichier_backlinks, backlinks)
        # Les DONNEES des deux fichiers sont durables (fsync ci-dessus), mais pas
        # encore leurs entrees de repertoire. Sans ce fsync, une coupure juste
        # apres la bascule du manifeste pourrait laisser un manifeste durable qui
        # designe des fichiers introuvables au redemarrage.
        _fsync_repertoire(repertoire)
        _ecrire_json(
            tmp_meta,
            {
                "generation": generation,
                "vecteurs": fichier_vecteurs.name,
                "backlinks": fichier_backlinks.name,
                "fragments": [asdict(m) for m in metas],
            },
        )
        with _verrou_publication(repertoire, fcntl.LOCK_EX):
            tmp_meta.replace(repertoire / FICHIER_META)  # COMMIT
            _fsync_repertoire(repertoire)
            _purger_generations(repertoire, generation)
            _fsync_repertoire(repertoire)
    except BaseException:
        # Rien n'a ete commite : les fichiers de cette generation ne sont
        # references par personne, on ne laisse pas de dechets derriere soi.
        for orphelin in (fichier_vecteurs, fichier_backlinks):
            with contextlib.suppress(OSError):
                orphelin.unlink()
        raise
    finally:
        with contextlib.suppress(OSError):
            tmp_meta.unlink()


# ----------------------------------------------------------------- instantane


@dataclass(frozen=True)
class Instantane:
    """Triplet coherent, epingle pour toute la duree d'une operation de lecture."""

    signature: int
    generation: int
    vecteurs: NDArray[np.float32]
    metas: list[MetaFragment]
    backlinks: dict[str, list[str]]
    _par_note: dict[str, list[int]] = field(default_factory=dict, repr=False)
    _noms: dict[str, list[str]] = field(default_factory=dict, repr=False)

    @property
    def fragments_par_note(self) -> dict[str, list[int]]:
        if not self._par_note:
            for ligne, meta in enumerate(self.metas):
                self._par_note.setdefault(meta.chemin, []).append(ligne)
        return self._par_note

    @property
    def noms_vers_chemins(self) -> dict[str, list[str]]:
        if not self._noms:
            for chemin in self.fragments_par_note:
                nom = chemin.rsplit("/", 1)[-1].removesuffix(".md")
                self._noms.setdefault(nom, []).append(chemin)
        return self._noms


# Reconstruction BM25 au plus une fois par intervalle : le worker incremental publie
# souvent, et chaque construction coute ~80 s de CPU sur l'index reel.
BM25_INTERVALLE_S = float(os.environ.get("VAULT_MCP_BM25_INTERVALLE_S", "900"))
BM25_SYNCHRONE_MAX = 5000


def poids_autorite() -> float:
    """Poids du prior d'autorite dans la fusion (0 = desactive). Voir `autorite.py`."""
    return float(os.environ.get("VAULT_MCP_POIDS_AUTORITE", "2.0"))


@dataclass(frozen=True)
class _Bm25:
    generation: int
    construit_a: float
    metas: list[MetaFragment]
    index: IndexBM25


def textes_complets(metas: Sequence[MetaFragment], racine: Path) -> list[str]:
    """Texte complet de chaque fragment, re-fragmente depuis le miroir.

    `fragmenter` est deterministe : (chemin, rang) designe le meme fragment tant que
    la note n'a pas change. Note absente, illisible ou modifiee depuis l'indexation
    (debut de texte different de l'apercu) : on garde titre + apercu, soit exactement
    ce que voyait l'ancien lexical.
    """
    voulues = {m.chemin for m in metas}
    par_note: dict[str, dict[int, str]] = {}
    for fichier in notes(racine) if racine.is_dir() else []:
        relatif = fichier.relative_to(racine).as_posix()
        if relatif not in voulues:
            continue
        try:
            contenu = fichier.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        par_note[relatif] = {f.rang: f.texte for f in fragmenter(relatif, contenu)}
    sortie: list[str] = []
    for meta in metas:
        texte = par_note.get(meta.chemin, {}).get(meta.rang)
        debut = meta.apercu[:200]
        if texte is None or not texte[:200].replace("\n", " ").startswith(debut):
            texte = f"{meta.titre} {meta.apercu}"
        sortie.append(texte)
    return sortie


class Index:
    """Index charge en lecture seule. `vecteurs` est mappe en memoire, pas copie."""

    def __init__(self, repertoire: Path | None = None) -> None:
        self._repertoire = repertoire or repertoire_index()
        self._instantane: Instantane | None = None
        self._bm25: _Bm25 | None = None
        self._bm25_verrou = threading.Lock()

    @property
    def repertoire(self) -> Path:
        return self._repertoire

    @property
    def disponible(self) -> bool:
        return (self._repertoire / FICHIER_META).exists()

    def _signature_disque(self) -> int | None:
        """mtime_ns de `meta.json`, le SEUL point de bascule de `sauvegarder()`.

        `_ns` : sur un full de 2 h suivi d'un batch incremental, deux publications
        peuvent tomber dans la meme seconde. La resolution seconde ferait servir
        l'ancienne generation jusqu'a la suivante.
        """
        try:
            return (self._repertoire / FICHIER_META).stat().st_mtime_ns
        except OSError:
            return None

    def _charger(self) -> Instantane:
        """Charge une generation coherente, verrou de publication PARTAGE tenu.

        `meta.json` designe ses propres fichiers de vecteurs et de backlinks :
        c'est ce qui rend le triplet indissociable, meme apres un crash en pleine
        publication. Le format herite (une simple liste de fragments, avec
        `vectors.npy` et `backlinks.json` a noms fixes) reste lisible : la bascule
        de format se fait a la premiere publication du nouveau code, et l'index
        deja en place ne doit pas devenir illisible entre-temps.

        La verification de forme reste utile face a un index herite laisse
        incoherent par l'ancien ordre de publication : mieux vaut echouer
        bruyamment que servir des resultats silencieusement faux.
        """
        dernier = ""
        for _ in range(_ESSAIS_CHARGEMENT):
            with _verrou_publication(self._repertoire, fcntl.LOCK_SH):
                signature = self._signature_disque()
                if signature is None:
                    raise RuntimeError("index absent, lancer scripts/reindex.py")
                document = json.loads(
                    (self._repertoire / FICHIER_META).read_text(encoding="utf-8")
                )
                if isinstance(document, list):
                    generation, brut = 0, document
                    nom_vecteurs, nom_backlinks = FICHIER_VECTEURS, FICHIER_BACKLINKS
                else:
                    generation = int(document.get("generation", 0))
                    brut = document["fragments"]
                    nom_vecteurs = document.get("vecteurs", FICHIER_VECTEURS)
                    nom_backlinks = document.get("backlinks", FICHIER_BACKLINKS)
                vecteurs: NDArray[np.float32] = np.load(
                    self._repertoire / nom_vecteurs, mmap_mode="r"
                )
                fichier_backlinks = self._repertoire / nom_backlinks
                backlinks = (
                    json.loads(fichier_backlinks.read_text(encoding="utf-8"))
                    if fichier_backlinks.exists()
                    else {}
                )
            metas = [MetaFragment(**m) for m in brut]
            if vecteurs.shape[0] == len(metas):
                return Instantane(
                    signature=signature,
                    generation=generation,
                    vecteurs=vecteurs,
                    metas=metas,
                    backlinks=backlinks,
                )
            dernier = f"{vecteurs.shape[0]} vecteurs pour {len(metas)} metadonnees"
        raise RuntimeError(f"index incoherent sur disque : {dernier}")

    def _courant(self) -> Instantane:
        """Instantane a jour. Recharge uniquement si la generation a change."""
        actuelle = self._signature_disque()
        if actuelle is None:
            # Index jamais construit : inoffensif, pas une erreur. Les outils de
            # lecture doivent rendre « rien » et non planter.
            self._instantane = None
            return Instantane(
                signature=-1,
                generation=0,
                vecteurs=np.empty((0, DIMENSIONS), dtype=np.float32),
                metas=[],
                backlinks={},
            )
        if self._instantane is None or actuelle != self._instantane.signature:
            self._instantane = self._charger()
        return self._instantane

    def invalider(self) -> None:
        """Force la relecture au prochain acces (a appeler apres avoir pris le verrou)."""
        self._instantane = None

    # ------------------------------------------------------------- proprietes

    @property
    def vecteurs(self) -> NDArray[np.float32]:
        return self._courant().vecteurs

    @property
    def metas(self) -> list[MetaFragment]:
        return self._courant().metas

    @property
    def backlinks(self) -> dict[str, list[str]]:
        return self._courant().backlinks

    @property
    def fragments_par_note(self) -> dict[str, list[int]]:
        return self._courant().fragments_par_note

    @property
    def noms_vers_chemins(self) -> dict[str, list[str]]:
        return self._courant().noms_vers_chemins

    # --------------------------------------------------------------- recherche

    def recherche_vectorielle(self, requete: str, limit: int = 10) -> list[Resultat]:
        if not self.disponible:
            return []
        instantane = self._courant()
        if not instantane.metas:
            return []
        q = vectoriser_un(requete)
        # Vecteurs normalises des deux cotes : le produit scalaire EST le cosinus.
        scores = np.asarray(instantane.vecteurs, dtype=np.float32) @ q

        # Le score d'une note est la *moyenne* de ses fragments, pas leur maximum.
        # Avec le maximum, une note de 14 fragments a 14 tirages pour en placer un
        # haut la ou une fiche courte en a 3 : les dumps longs gagnent mecaniquement.
        # Mesure sur le jeu de reference : max 5/20, moyenne 9/20.
        classement: list[tuple[float, str, int]] = []
        for chemin, lignes in instantane.fragments_par_note.items():
            valeurs = scores[lignes]
            meilleur = int(lignes[int(np.argmax(valeurs))])
            classement.append((float(valeurs.mean()), chemin, meilleur))
        classement.sort(key=lambda t: -t[0])

        return [
            Resultat(
                chemin=chemin,
                titre=instantane.metas[ligne].titre,
                # L'apercu vient du *meilleur* fragment, pas d'un fragment moyen :
                # c'est celui-la que le lecteur veut voir.
                apercu=instantane.metas[ligne].apercu,
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
        # Voie fraiche en tete : une note pas encore indexee n'a pas d'autre chance.
        indexees = self._courant().fragments_par_note
        resultats: list[Resultat] = [r for r in _frais(requete, limit) if r.chemin not in indexees]
        for meta in self._courant().metas:
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
        """Fusion par rang reciproque vecteur (mix) + BM25 plein texte, prior d'autorite.

        Mesure 2026-09-14 (scripts/eval_retrieval.py, 40 requetes, index reel) : voir
        docs/eval-retrieval.md. Retombe sur l'ancien lexical tant que le BM25 n'est pas pret.
        """
        instantane = self._courant()
        pool = max(limit * 5, 20)
        # Voie fraiche (vault_mcp.frais) : notes du miroir que l'index publie ne
        # reflete pas encore -- ConvIA arrivees par rclone, lots en attente d'embedding.
        frais = _frais(requete, pool)
        if not instantane.metas:
            return frais[:limit]
        bm25 = self._bm25_pret(instantane)
        if bm25 is None:
            return fusion_rang_reciproque(
                self.recherche_vectorielle(requete, limit * 2),
                self.recherche_lexicale(requete, limit * 2),
                limit,
            )
        q = vectoriser_un(requete)
        scores = np.asarray(instantane.vecteurs, dtype=np.float32) @ q
        vecteur: list[tuple[float, str, int]] = []
        for chemin, lignes in instantane.fragments_par_note.items():
            # Un index publie avant une nouvelle exclusion garde ces notes jusqu'au full.
            if not indexable(chemin):
                continue
            valeurs = scores[lignes]
            meilleur = int(np.argmax(valeurs))
            # mix : la moyenne seule noie une note courte et precise, le max seul
            # favorise les dumps longs. Mesure : MRR 0,468 contre 0,249 (moyenne).
            note = 0.5 * float(valeurs[meilleur]) + 0.5 * float(valeurs.mean())
            vecteur.append((note, chemin, int(lignes[meilleur])))
        vecteur.sort(key=lambda t: -t[0])
        lexical = self._classement_bm25(bm25, requete, instantane, pool)

        cumul: dict[str, float] = {}
        ligne_de: dict[str, int] = {}
        for rang, (_, chemin, ligne) in enumerate(vecteur[:pool]):
            cumul[chemin] = cumul.get(chemin, 0.0) + 1.0 / (61 + rang)
            ligne_de[chemin] = ligne
        for rang, (chemin, ligne) in enumerate(lexical):
            cumul[chemin] = cumul.get(chemin, 0.0) + 1.0 / (61 + rang)
            ligne_de.setdefault(chemin, ligne)
        # La voie fraiche remplace a elle seule vecteur ET BM25 pour une note ABSENTE
        # de l'index : son poids compense l'absence de seconde liste. Une note deja
        # indexee garde son classement d'index (garde aussi cote worker, cf. frais.py) :
        # le lui cumuler faisait regresser le banc -- mesure en prod 2026-09-14.
        poids_frais = _poids_frais()
        recents: dict[str, Resultat] = {}
        for rang, resultat in enumerate(
            r for r in frais if r.chemin not in instantane.fragments_par_note
        ):
            cumul[resultat.chemin] = cumul.get(resultat.chemin, 0.0) + poids_frais / (61 + rang)
            recents.setdefault(resultat.chemin, resultat)
        poids = poids_autorite()
        if poids and not historique(requete):
            for chemin in cumul:
                cumul[chemin] += poids * (4 - rang_autorite(chemin)) / 4 / 61
        ordonnes = sorted(cumul.items(), key=lambda kv: -kv[1])[:limit]
        sortie: list[Resultat] = []
        for chemin, score in ordonnes:
            if chemin in recents:
                # Le contenu frais est plus recent que l'apercu indexe.
                r = recents[chemin]
                sortie.append(Resultat(chemin, r.titre, r.apercu, score, "frais"))
            else:
                meta = instantane.metas[ligne_de[chemin]]
                sortie.append(Resultat(chemin, meta.titre, meta.apercu, score, "hybride"))
        return sortie

    # ----------------------------------------------------------- BM25 plein texte

    def _bm25_pret(self, instantane: Instantane) -> _Bm25 | None:
        """BM25 disponible, eventuellement d'une generation anterieure.

        Le BM25 est reconstruit depuis le miroir (le texte complet n'est pas dans
        l'index) : ~80 s sur 244k fragments. Il est donc construit en arriere-plan et
        une generation anterieure reste servie en attendant -- son classement est au
        niveau NOTE, filtre sur les notes encore presentes, donc toujours valide ; seul
        le texte d'une note modifiee entre-temps est en retard de quelques minutes.
        Petit index (tests, vault minuscule) : construction synchrone.
        """
        courant = self._bm25
        perime = courant is None or (
            courant.generation != instantane.signature
            and time.monotonic() - courant.construit_a >= BM25_INTERVALLE_S
        )
        if perime and not self._bm25_verrou.locked():
            if len(instantane.metas) <= BM25_SYNCHRONE_MAX:
                self._construire_bm25(instantane)
            else:
                threading.Thread(
                    target=self._construire_bm25, args=(instantane,), daemon=True
                ).start()
        return self._bm25

    def _construire_bm25(self, instantane: Instantane) -> None:
        if not self._bm25_verrou.acquire(blocking=False):
            return
        try:
            textes = textes_complets(instantane.metas, repertoire_vault())
            bm25 = IndexBM25.construire(
                [f"{m.chemin} {m.titre} {t}" for m, t in zip(instantane.metas, textes, strict=True)]
            )
            self._bm25 = _Bm25(instantane.signature, time.monotonic(), instantane.metas, bm25)
        except Exception:  # noqa: BLE001 -- le lexical historique reste servi
            logging.getLogger(__name__).exception("construction BM25 echouee")
        finally:
            self._bm25_verrou.release()

    @staticmethod
    def _classement_bm25(
        bm25: _Bm25, requete: str, instantane: Instantane, limit: int
    ) -> list[tuple[str, int]]:
        scores = bm25.index.scores(requete)
        touches = np.nonzero(scores)[0]
        meilleur: dict[str, tuple[float, int]] = {}
        for i in touches[np.argsort(-scores[touches], kind="stable")]:
            chemin = bm25.metas[int(i)].chemin
            if chemin in meilleur:
                continue
            lignes = instantane.fragments_par_note.get(chemin)
            if lignes is None or not indexable(chemin):
                continue
            rang = bm25.metas[int(i)].rang
            ligne = next((j for j in lignes if instantane.metas[j].rang == rang), lignes[0])
            meilleur[chemin] = (float(scores[i]), ligne)
            if len(meilleur) >= limit:
                break
        return [(chemin, ligne) for chemin, (_, ligne) in meilleur.items()]

    # ------------------------------------------------------------ reindexation

    def reindexer_note(self, chemin: str, contenu: str) -> dict[str, object]:
        """Reindexe UNE note. Conserve pour compatibilite : delegue au batch."""
        return self.reindexer_notes({chemin: contenu})

    def reindexer_notes(
        self,
        changements: Mapping[str, str | None],
        *,
        attente_verrou_s: float | None = None,
    ) -> dict[str, object]:
        """Reindexe N notes en UNE seule publication, contenus deja lus.

        `changements` : chemin (relatif au vault, separateurs POSIX) -> contenu
        ACTUEL, ou `None` pour retirer la note de l'index (suppression, ou source
        disparue du miroir apres un renommage). `None` n'est PAS simule par une
        note vide : une note vide produirait un fragment vide indexable, une
        suppression doit retirer les lignes.
        """
        return self.reindexer_chemins(
            list(changements), lambda chemin: changements[chemin],
            attente_verrou_s=attente_verrou_s,
        )

    def reindexer_chemins(
        self,
        chemins: Sequence[str],
        lecteur: Callable[[str], str | None | object],
        *,
        attente_verrou_s: float | None = None,
    ) -> dict[str, object]:
        """Reindexe N chemins en UNE seule publication, contenus lus SOUS verrou.

        `lecteur(chemin)` rend le contenu courant, `None` pour retirer la note, ou
        `IGNORER` pour laisser la note telle quelle (source illisible).

        La lecture DOIT se faire sous verrou. Un worker qui attend la fin d'un full
        de 2 h avec des contenus lus avant l'attente republierait par-dessus le full
        un etat vieux de deux heures -- exactement la perte que le verrou est cense
        empecher.

        Le dictionnaire construit porte le last-write-wins : une note modifiee dix
        fois pendant la fenetre de coalescence n'est vectorisee qu'une fois.

        Leve `VerrouOccupe` si un autre writer travaille et `attente_verrou_s` est
        ecoule -- l'appelant doit alors laisser ses chemins dans la file durable,
        surtout pas forcer.
        """
        if not self.disponible:
            raise RuntimeError("index absent, lancer scripts/reindex.py")
        if not chemins:
            return {"etat": "sans_objet", "notes": 0}

        with verrou_writers(self._repertoire, attente_verrou_s):
            changements: dict[str, str | None] = {}
            ignores: list[str] = []
            for chemin in dict.fromkeys(chemins):
                contenu = lecteur(chemin)
                if contenu is IGNORER:
                    # Le chemin n'a pas ete traite : l'appelant ne doit pas
                    # l'acquitter, sinon la modification disparait de la file et
                    # n'est plus rattrapee que par le full quotidien.
                    ignores.append(chemin)
                    continue
                if contenu is not None and not isinstance(contenu, str):
                    raise RuntimeError(f"contenu invalide pour {chemin}")
                changements[chemin] = contenu
            if not changements:
                return {"etat": "sans_objet", "notes": 0, "ignores": ignores}

            self.invalider()
            instantane = self._courant()
            anciens = instantane.metas
            avant_fragments = len(anciens)
            avant_notes = len(instantane.fragments_par_note)
            cibles = set(changements)

            # 1. Fragments des contenus actuels, strictement comme scripts/reindex.py.
            textes: list[str] = []
            nouvelles: list[MetaFragment] = []
            for chemin, contenu in changements.items():
                if contenu is None:
                    continue
                for fragment in fragmenter(chemin, contenu):
                    textes.append(fragment.texte)
                    nouvelles.append(
                        MetaFragment(
                            chemin=fragment.chemin,
                            rang=fragment.rang,
                            titre=fragment.titre,
                            apercu=fragment.texte[:APERCU_CARACTERES].replace("\n", " "),
                        )
                    )
            vecteurs_nouveaux = (
                vectoriser(textes) if textes else np.empty((0, DIMENSIONS), dtype=np.float32)
            )

            # 2. Lignes des AUTRES notes, conservees a l'identique.
            garde = [i for i, meta in enumerate(anciens) if meta.chemin not in cibles]
            conserves = (
                np.asarray(instantane.vecteurs)[garde]
                if garde
                else np.empty((0, DIMENSIONS), dtype=np.float32)
            )
            vecteurs = np.concatenate([conserves, vecteurs_nouveaux], axis=0)
            metas = [anciens[i] for i in garde] + nouvelles

            # 3. Backlinks : on retire TOUTES les notes du lot des listes de sources,
            #    puis on rejoue la contribution courante de celles qui existent encore.
            backlinks: dict[str, list[str]] = {}
            for cible, sources in instantane.backlinks.items():
                restantes = [source for source in sources if source not in cibles]
                if restantes:
                    backlinks[cible] = restantes
            for chemin, contenu in changements.items():
                if contenu is None:
                    continue
                for cible in extraire_wikilinks(contenu):
                    if chemin not in backlinks.setdefault(cible, []):
                        backlinks[cible].append(chemin)

            sauvegarder(self._repertoire, vecteurs, metas, backlinks)
            self.invalider()

        supprimees = sum(1 for contenu in changements.values() if contenu is None)
        return {
            "etat": "applique",
            "ignores": ignores,
            "notes": len(changements),
            "notes_supprimees": supprimees,
            "chemins": sorted(changements)[:20],
            "fragments_avant": avant_fragments,
            "fragments_nouveaux": len(nouvelles),
            "fragments_apres": len(metas),
            "notes_indexees_avant": avant_notes,
            "notes_indexees_apres": len({meta.chemin for meta in metas}),
            "cibles_backlinks": len(backlinks),
        }

    # ---------------------------------------------------------------- graphe

    def contexte_graphe(self, chemin: str, limite: int = 50) -> dict[str, object]:
        """Backlinks, liens sortants et voisins immediats d'une note."""
        instantane = self._courant()
        nom = chemin.rsplit("/", 1)[-1].removesuffix(".md")

        entrants = [c for c in instantane.backlinks.get(nom, []) if c != chemin][:limite]

        sortants: list[dict[str, object]] = []
        for cible, sources in instantane.backlinks.items():
            if chemin not in sources:
                continue
            resolus = instantane.noms_vers_chemins.get(cible, [])
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
            "existe": chemin in instantane.fragments_par_note,
            "backlinks": entrants,
            "nb_backlinks": len(instantane.backlinks.get(nom, [])),
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


def _poids_frais() -> float:
    return float(os.environ.get("VAULT_MCP_POIDS_FRAIS", "2.0"))


def _frais(requete: str, limit: int) -> list[Resultat]:
    """Classement de la voie fraiche, vide si desactivee ou absente."""
    if not _poids_frais():
        return []
    # Import tardif : `frais` importe `Resultat` depuis ce module.
    from vault_mcp import frais

    return frais.rechercher(requete, limit)


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
    "IGNORER",
    "Instantane",
    "MetaFragment",
    "Resultat",
    "VerrouOccupe",
    "extraire_wikilinks",
    "fusion_rang_reciproque",
    "repertoire_index",
    "sauvegarder",
    "verrou_writers",
]
