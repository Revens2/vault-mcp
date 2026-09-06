"""Acces en lecture a la base CouchDB `vault_rag`.

Regle non negociable de ce module : **aucun message d'erreur ne doit contenir l'URL
CouchDB ni d'identifiant**. `COUCH_URL` est de la forme `http://user:motdepasse@hote`,
et httpx recopie volontiers l'URL complete dans ses exceptions. Toute erreur reseau est
donc reemballee en `StoreError` avec un message construit a la main.
"""

from __future__ import annotations

import os
import re
import sys
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx

from vault_mcp.safety import normalize_path

# Le serveur v1 acceptait un `limit` arbitraire et le passait tel quel a CouchDB :
# `limit=10**9` faisait materialiser la base entiere en memoire. Borne dure ici.
DELAI_LECTURE_S = 20.0
DELAI_RECHERCHE_S = 30.0


class StoreError(RuntimeError):
    """Erreur d'acces au stockage, deja expurgee de tout secret."""


@dataclass(frozen=True)
class ResultatRecherche:
    path: str
    snippet: str


def _borner(limit: int) -> int:
    """`limit <= 0` signifie SANS LIMITE ; sinon la valeur demandee, telle quelle.

    Le plafond serveur `LIMITE_MAX` a ete retire (adr/0020) : plus aucune borne
    n est imposee sur le NOMBRE de resultats. Les gardes qui subsistent
    (MAX_LECTURE_OCTETS, MAX_FICHIERS_BALAYES) portent sur la consommation de
    ressources du processus, ce qui est une autre categorie.

    `sys.maxsize` plutot qu une valeur sentinelle : les comparaisons
    `len(sortie) >= borne` du code appelant restent correctes sans modification.
    """
    if limit <= 0:
        return sys.maxsize
    return limit


class CouchStore:
    """Lecture seule sur `vault_rag`. Construit depuis l'environnement par `depuis_env`."""

    def __init__(self, base_url: str, db: str, client: httpx.Client | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._db = db
        # Un client injectable est ce qui rend ce module testable sans CouchDB :
        # les tests passent un httpx.MockTransport.
        self._client = client or httpx.Client(timeout=DELAI_LECTURE_S)

    @classmethod
    def depuis_env(cls) -> CouchStore:
        try:
            base = os.environ["COUCH_URL"]
        except KeyError as exc:
            raise StoreError("COUCH_URL absent de l'environnement") from exc
        return cls(base, os.environ.get("COUCH_DB", "vault_rag"))

    def _url(self, suffixe: str) -> str:
        return f"{self._base}/{self._db}{suffixe}"

    def _appel(self, methode: str, suffixe: str, **kw: Any) -> httpx.Response:
        try:
            return self._client.request(methode, self._url(suffixe), **kw)
        except httpx.HTTPError as exc:
            # str(exc) contient l'URL, donc les identifiants. On ne garde que le type.
            raise StoreError(f"stockage injoignable ({type(exc).__name__})") from None

    def lire_note(self, path: str) -> str | None:
        """Contenu markdown d'une note, ou None si absente."""
        chemin = normalize_path(path)
        # Le `_id` CouchDB est le chemin relatif : il contient des `/` qui doivent
        # etre encodes, sans quoi CouchDB les lit comme une hierarchie d'URL.
        reponse = self._appel("GET", "/" + urllib.parse.quote(chemin.relatif, safe=""))
        if reponse.status_code == 404:
            return None
        if reponse.status_code >= 400:
            raise StoreError(f"lecture refusee par le stockage (HTTP {reponse.status_code})")
        contenu = reponse.json().get("content")
        return contenu if isinstance(contenu, str) else ""

    def lister_chemins(self, prefix: str = "", limit: int = 200) -> list[str]:
        """Chemins de notes, filtres par prefixe. Les `_id` reserves sont ecartes."""
        borne = _borner(limit)
        reponse = self._appel("GET", "/_all_docs")
        if reponse.status_code >= 400:
            raise StoreError(f"listage refuse par le stockage (HTTP {reponse.status_code})")
        ids = [
            ligne["id"]
            for ligne in reponse.json().get("rows", [])
            if not ligne["id"].startswith("_")
        ]
        if prefix:
            racine = normalize_path(prefix, exiger_md=False).relatif
            ids = [i for i in ids if i.startswith(racine)]
        return ids[:borne]

    def rechercher(self, query: str, limit: int = 20) -> list[ResultatRecherche]:
        """Recherche plein-texte insensible a la casse. Contrat identique a la v1."""
        if not query.strip():
            raise StoreError("requete vide")
        borne = _borner(limit)
        selecteur = {
            "selector": {"content": {"$regex": "(?i)" + _echapper_regex(query)}},
            "fields": ["path", "content"],
            "limit": borne,
        }
        reponse = self._appel(
            "POST",
            "/_find",
            json=selecteur,
            headers={"Content-Type": "application/json"},
            timeout=DELAI_RECHERCHE_S,
        )
        if reponse.status_code >= 400:
            raise StoreError(f"recherche refusee par le stockage (HTTP {reponse.status_code})")
        return [_extraire(doc, query) for doc in reponse.json().get("docs", [])]


def _echapper_regex(motif: str) -> str:
    return re.escape(motif)


def _extraire(doc: dict[str, Any], query: str) -> ResultatRecherche:
    contenu = doc.get("content", "")
    trouve = re.search("(?i)" + re.escape(query), contenu)
    debut = max(0, trouve.start() - 60) if trouve else 0
    return ResultatRecherche(
        path=doc.get("path", ""),
        snippet=contenu[debut : debut + 160].replace("\n", " "),
    )
