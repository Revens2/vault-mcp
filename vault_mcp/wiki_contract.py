"""Contrat d'extraction Wiki lu a sa SOURCE CANONIQUE, jamais recopie.

La source de verite du contrat `wiki-extract-v*` est `llm_wiki_extract.py`
(depot vps-etude-infra, deploye dans /usr/local/bin) : `RESPONSE_SCHEMA`,
`SYSTEM_PREFIX`, `validate()`. Ce module le charge tel quel, dans un espace
de noms prive, et n'en garde aucune copie sur disque :

* le schema expose au consommateur MCP est `RESPONSE_SCHEMA` lui-meme, plus
  sa traduction mecanique en JSON Schema (aucune regle ajoutee ici) ;
* `validate()` canonique est rejoue par le serveur a chaque submit.

Fail closed : module absent, illisible, qui leve a l'import, qui ne porte pas
les symboles attendus ou dont le dialecte de schema est inconnu ->
`ContractUnavailableError`, jamais un repli sur un schema anterieur. Le cache suit
l'empreinte du fichier : un redeploiement du contrat change le digest sans
redemarrer le serveur.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.abc
import importlib.util
import json
import os
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_MODULE = "/usr/local/bin/llm_wiki_extract.py"
JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
SOURCE_DIALECT = ("llm_wiki_extract.RESPONSE_SCHEMA (sous-ensemble OpenAPI :"
                  " OBJECT/STRING/nullable/propertyOrdering)")

_TYPES = {"OBJECT": "object", "ARRAY": "array", "STRING": "string",
          "NUMBER": "number", "INTEGER": "integer", "BOOLEAN": "boolean"}
# Cles du dialecte source. Toute autre cle = dialecte inconnu = refus.
_KNOWN_KEYS = {"type", "description", "enum", "format", "nullable",
               "properties", "required", "items", "propertyOrdering"}


class ContractUnavailableError(RuntimeError):
    """Contrat canonique introuvable ou inexploitable (fail closed)."""


@dataclass(frozen=True)
class Canonical:
    path: str
    sha256: str
    version: str
    response_schema: dict
    instructions: str
    validate: Callable[..., Any]


_lock = threading.Lock()
_cache: dict[str, tuple[tuple[int, int, int], Canonical]] = {}


def module_path() -> str:
    return os.environ.get("WIKI_CONTRACT_MODULE", DEFAULT_MODULE)


def canonical_json(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(obj: object) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


class _OctetsLoader(importlib.abc.SourceLoader):
    """Charge EXACTEMENT les octets deja lus (et haches) : pas de relecture du
    fichier entre l'empreinte et l'execution, pas de .pyc ecrit."""

    def __init__(self, path: str, data: bytes) -> None:
        self._path = path
        self._data = data

    def get_filename(self, fullname: str) -> str:
        return self._path

    def get_data(self, path: str) -> bytes:
        return self._data


def _exec_module(path: str, data: bytes) -> object:
    """Charge le module canonique dans un espace de noms prive.

    `llm_wiki_extract` fait `sys.path.insert(0, "/usr/local/bin")` a l'import :
    sys.path est restaure apres coup pour ne jamais laisser un repertoire de
    scripts masquer un module du serveur. Le module n'est pas inscrit dans
    sys.modules.
    """
    spec = importlib.util.spec_from_loader(
        "_wiki_contrat_canonique", _OctetsLoader(path, data), origin=path)
    if spec is None or spec.loader is None:
        raise ContractUnavailableError(f"module canonique non chargeable : {path}")
    mod = importlib.util.module_from_spec(spec)
    saved = list(sys.path)
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path[:] = saved
    return mod


def load_canonical() -> Canonical:
    path = module_path()
    try:
        st = Path(path).stat()
    except OSError as exc:
        raise ContractUnavailableError(
            f"module canonique absent : {path} ({exc.strerror})") from exc
    key = (st.st_mtime_ns, st.st_size, st.st_ino)
    with _lock:
        hit = _cache.get(path)
        if hit and hit[0] == key:
            return hit[1]
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            raise ContractUnavailableError(
                f"module canonique illisible : {path} ({exc.strerror})") from exc
        try:
            mod = _exec_module(path, data)
        except ContractUnavailableError:
            raise
        except Exception as exc:  # import du contrat en echec : jamais de repli
            raise ContractUnavailableError(
                f"module canonique en erreur a l'import : {type(exc).__name__}: {exc}") from exc
        version = getattr(mod, "CONTRACT_VERSION", None)
        schema = getattr(mod, "RESPONSE_SCHEMA", None)
        prefix = getattr(mod, "SYSTEM_PREFIX", None)
        validate = getattr(mod, "validate", None)
        if not isinstance(version, str) or not version:
            raise ContractUnavailableError("module canonique sans CONTRACT_VERSION")
        if not isinstance(schema, dict):
            raise ContractUnavailableError("module canonique sans RESPONSE_SCHEMA")
        if not isinstance(prefix, str) or not callable(validate):
            raise ContractUnavailableError("module canonique sans SYSTEM_PREFIX/validate")
        canon = Canonical(path=path, sha256=hashlib.sha256(data).hexdigest(),
                          version=version, response_schema=copy.deepcopy(schema),
                          instructions=prefix, validate=validate)
        _cache[path] = (key, canon)
        return canon


def to_json_schema(node: dict) -> dict:
    """Traduction MECANIQUE du dialecte source vers JSON Schema 2020-12.

    Aucune contrainte n'est inventee : types en minuscules, `nullable` ->
    union avec null, `propertyOrdering` et `format: enum` retires (non
    JSON Schema). Une cle inconnue leve ValueError : un dialecte qui a bouge
    doit casser bruyamment, pas produire un schema faux.
    """
    if not isinstance(node, dict):
        raise TypeError(f"noeud de schema non-objet : {node!r}")
    unknown = set(node) - _KNOWN_KEYS
    if unknown:
        raise ValueError(f"cle(s) de schema inconnue(s) : {sorted(unknown)}")
    out: dict[str, Any] = {}
    t = node.get("type")
    if t not in _TYPES:
        raise ValueError(f"type de schema inconnu : {t!r}")
    jt: Any = _TYPES[t]
    if node.get("nullable"):
        jt = [jt, "null"]
    out["type"] = jt
    if "description" in node:
        out["description"] = node["description"]
    if "enum" in node:
        out["enum"] = list(node["enum"])
    fmt = node.get("format")
    if fmt not in (None, "enum"):
        raise ValueError(f"format de schema inconnu : {fmt!r}")
    if "properties" in node:
        out["properties"] = {k: to_json_schema(v) for k, v in node["properties"].items()}
    if "required" in node:
        out["required"] = list(node["required"])
    if "items" in node:
        out["items"] = to_json_schema(node["items"])
    return out
