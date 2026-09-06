"""File d intentions d ecriture (adr/0020).

POURQUOI CE MODULE EXISTE
-------------------------
Le service MCP est le seul composant du VPS joignable depuis internet. Son unite
systemd lui interdit toute sortie reseau (`IPAddressDeny=any`) et toute ecriture
hors de deux repertoires (`ProtectSystem=strict`, `ReadWritePaths`). Il ne peut
donc ecrire ni sur Drive -- la source de verite du vault -- ni dans le miroir
`/srv/vault-mirror`, protege par un `ReadOnlyPaths` dedie.

Ce module est le seul moyen d ecrire dont dispose le MCP : il depose un fichier
JSON decrivant l intention. Un processus separe (`vault-spool-push.service`,
uid `juliann`) la consomme et l applique. La compromission totale du MCP donne
donc au mieux la capacite de deposer des intentions valides -- exactement le
privilege accorde, et rien de plus.

CE QUE CE MODULE NE FAIT PAS
----------------------------
Aucun appel reseau, aucun `subprocess`, aucune connaissance de rclone ou de
Drive. Il ne raisonne pas sur le markdown : `contenu_b64` porte toujours l etat
FINAL complet de la note, jamais un delta. `append` et `patch` sont resolus en
amont par `vault_mcp.ecriture`, qui sait lire le miroir. Le pousseur reste bete,
ce qui supprime toute divergence de semantique entre les deux processus et rend
chaque intention naturellement idempotente.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

SPOOL_DEFAUT = "/srv/vault-spool"

# Version du schema d intention. Le pousseur refuse ce qu il ne sait pas lire :
# une montee de version cote MCP sans montee cote pousseur doit echouer bruyamment
# en `failed/`, pas etre appliquee de travers.
VERSION_SCHEMA = 1

OPS = (
    "create",
    "update",
    "delete",
    "move",
    # Dossier vide cree sur Drive par le pousseur (outil `create_folder`).
    # Ni note ni contenu : pas de reindexation a armer cote pousseur.
    "mkdir",
    "admin/reindex",
    "admin/sync",
)


class SpoolError(RuntimeError):
    """Echec de depot. Le message ne contient jamais le contenu de la note."""


class Spool:
    """Depot atomique d intentions et lecture de leur etat."""

    def __init__(self, racine: str = SPOOL_DEFAUT) -> None:
        self._racine = Path(racine)
        self.tmp = self._racine / "tmp"
        self.queue = self._racine / "queue"
        self.done = self._racine / "done"
        self.failed = self._racine / "failed"

    @classmethod
    def depuis_env(cls) -> Spool:
        return cls(os.environ.get("VAULT_MCP_SPOOL", SPOOL_DEFAUT))

    @property
    def disponible(self) -> bool:
        """Le spool est-il utilisable ? Verifie a chaque appel, pas au demarrage.

        Le service doit demarrer meme si le spool n a pas encore ete cree : la
        lecture ne doit jamais dependre de l ecriture.
        """
        return self.queue.is_dir() and self.tmp.is_dir()

    # ---------------------------------------------------------------- depot
    def deposer(
        self,
        op: str,
        path: str,
        *,
        contenu: str | None = None,
        path_cible: str | None = None,
        sha256_attendu: str | None = None,
        md5_attendu: str | None = None,
        client_id: str = "",
        lot: str | None = None,
    ) -> str:
        """Depose une intention et rend son identifiant.

        L ecriture est atomique : `tmp/` puis `os.replace` vers `queue/`. Le
        pousseur ne regarde jamais `tmp/`, il ne peut donc pas lire une
        intention partiellement ecrite.
        """
        if op not in OPS:
            raise SpoolError(f"operation inconnue : {op}")
        if not self.disponible:
            raise SpoolError("spool indisponible")

        identifiant = uuid.uuid4().hex[:12]
        intention: dict[str, Any] = {
            "version": VERSION_SCHEMA,
            "id": identifiant,
            "op": op,
            "path": path,
            "path_cible": path_cible,
            "contenu_b64": (
                base64.b64encode(contenu.encode("utf-8")).decode("ascii")
                if contenu is not None
                else None
            ),
            "sha256_attendu": sha256_attendu,
            "horodatage": _maintenant_iso(),
            "client_id": client_id,
            "lot": lot,
        }
        # Version-token interne Drive (adr/0023) : calcule par le SERVEUR sur
        # les octets lus, compare par le POUSSEUR au md5 natif Drive
        # (`lsjson --hash`) au moment de l execution. VRAIMENT optionnel : on
        # n'ecrit la cle que lorsqu'un jeton CAS existe, afin que le schema
        # d'une intention sans CAS reste byte-identique au schema v1 (le test
        # historique test_spool.py::test_schema_complet compare le dict exact).
        # Une intention ancienne (v1, sans ce champ) reste traitable par le
        # repli legacy du pousseur. Jamais expose par l'API MCP : uniquement
        # transporte en interne serveur -> spool -> worker.
        if md5_attendu is not None:
            intention["md5_attendu"] = md5_attendu

        # `time_ns` en largeur fixe (19 chiffres) rend le tri lexicographique du
        # glob egal au tri chronologique jusqu en 2286. L UUID casse la collision
        # entre deux requetes tombant dans la meme nanoseconde.
        nom = f"{time.time_ns():019d}-{identifiant}.json"
        provisoire = self.tmp / nom
        definitif = self.queue / nom

        charge = json.dumps(intention, ensure_ascii=False, sort_keys=True)
        try:
            with provisoire.open("w", encoding="utf-8") as fichier:
                fichier.write(charge)
                fichier.flush()
                os.fsync(fichier.fileno())
                # 0660 EXPLICITE, avant le replace. L unite systemd impose
                # `UMask=0077` : sans ce chmod l intention est creee en 0600 et
                # le pousseur, qui tourne sous `juliann` et n a que le groupe
                # `vault-spool`, ne peut pas la lire. Le defaut ne se voit pas
                # en test (umask 022 en session) : il n apparait que sous
                # systemd. Constate le 2026-09-05 au test bout-en-bout.
                os.fchmod(fichier.fileno(), 0o660)  # nosemgrep: insecure-file-permissions
            # `os.replace` et non `Path.replace` (PTH105) : c est la primitive
            # POSIX documentee comme atomique, et c est le point exact que le test
            # d atomicite monkeypatche pour simuler un disque plein.
            os.replace(provisoire, definitif)  # noqa: PTH105
            _fsync_repertoire(self.queue)
        except OSError as exc:
            # Jamais l exception brute ni le contenu : l une porte un chemin
            # systeme absolu, l autre peut porter un secret du vault.
            raise SpoolError(f"depot impossible ({exc.__class__.__name__})") from None

        # Journal (journald via stdout). Jamais le contenu -- `secrets.masquer`
        # n est pas concu pour du log, et une note peut porter un identifiant.
        print(
            f"ecriture op={op} path={path} id={identifiant} client={client_id or '-'}",
            file=sys.stdout,
            flush=True,
        )
        return identifiant

    # ----------------------------------------------------------------- etat
    def etat(self, identifiant: str) -> dict[str, Any]:
        """Etat d une intention : `en_attente`, `applique`, `echec`, `inconnu`.

        Lecture seule. `done/` et `failed/` sont lisibles par le groupe
        `vault-spool` sans que `ReadWritePaths` n en depende.
        """
        if not _identifiant_plausible(identifiant):
            return {"etat": "inconnu", "motif": "identifiant invalide"}

        recu = self.done / f"{identifiant}.json"
        if recu.is_file():
            return {"etat": "applique", **_lire_json(recu)}

        echec = self.failed / f"{identifiant}.json"
        if echec.is_file():
            return {"etat": "echec", **_lire_json(echec)}

        # La file est testee en dernier : une intention traitee pendant l appel
        # doit etre rapportee comme appliquee, pas comme en attente.
        if any(self.queue.glob(f"*-{identifiant}.json")):
            return {"etat": "en_attente"}

        return {"etat": "inconnu"}

    def statistiques(self) -> dict[str, Any]:
        """Profondeur de file, ages, volume des echecs.

        `echecs` reste le TOTAL historique (compat). La sante COURANTE se lit
        dans `echecs_recents` (fenetre 1 h — une panne active produit de
        nouveaux receipts chaque cycle) et `echec_recent_s` (age du dernier
        echec) : un stock ancien de conflits CAS ne doit pas ressembler a une
        panne active.
        """
        if not self.disponible:
            return {"disponible": False}

        en_file = sorted(self.queue.glob("*.json"))
        plus_vieux_s: float | None = None
        if en_file:
            try:
                plus_vieux_s = round(time.time() - en_file[0].stat().st_mtime, 1)
            except OSError:
                plus_vieux_s = None

        maintenant = time.time()
        ages_echecs: list[float] = []
        for p in self.failed.glob("*.json"):
            try:
                ages_echecs.append(maintenant - p.stat().st_mtime)
            except OSError:
                pass
        echecs_recents = sum(1 for a in ages_echecs if a < 3600)
        echec_recent_s = round(min(ages_echecs), 1) if ages_echecs else None

        return {
            "disponible": True,
            "en_attente": len(en_file),
            "plus_vieux_s": plus_vieux_s,
            "echecs": len(list(self.failed.glob("*.json"))),
            "echecs_recents": echecs_recents,
            "echec_recent_s": echec_recent_s,
            "appliquees": len(list(self.done.glob("*.json"))),
        }


# --------------------------------------------------------------- utilitaires
def _maintenant_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def _identifiant_plausible(identifiant: str) -> bool:
    """Un id vient du reseau : il finit dans un nom de fichier, donc on le filtre.

    12 caracteres hexadecimaux, rien d autre. Sans ce controle, `etat("../x")`
    ferait tester l existence d un fichier hors du spool.
    """
    return len(identifiant) == 12 and all(c in "0123456789abcdef" for c in identifiant)


def _lire_json(chemin: Path) -> dict[str, Any]:
    try:
        charge = json.loads(chemin.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"motif": "recu illisible"}
    if not isinstance(charge, dict):
        return {"motif": "recu malforme"}
    # `contenu_b64` peut peser 1 Mio et n a aucun interet dans un retour d etat.
    return {c: v for c, v in charge.items() if c != "contenu_b64"}


def _fsync_repertoire(chemin: Path) -> None:
    """Rend l entree de repertoire durable ; sans cela un `replace` peut se perdre."""
    descripteur = os.open(chemin, os.O_RDONLY)
    try:
        os.fsync(descripteur)
    finally:
        os.close(descripteur)


__all__ = ["OPS", "SPOOL_DEFAUT", "VERSION_SCHEMA", "Spool", "SpoolError"]
