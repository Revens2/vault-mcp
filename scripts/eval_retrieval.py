#!/usr/bin/env python3
"""Banc d'evaluation offline du retrieval, sur l'index et le miroir REELS, en lecture seule.

Rien n'est publie : on charge l'index existant (vecteurs mmap), on re-fragmente le miroir
pour retrouver le texte complet de chaque fragment (fragmenter est deterministe), et on
compare des variantes de classement sur un jeu etiquete.

Usage :
    eval_retrieval.py --golden tests/eval/golden.jsonl [--index DIR] [--vault DIR] [--out f.json]

Golden (JSONL) : {"id", "famille", "q", "attendus": [chemin ou prefixe/], "perimes": [...]}
  - hit si un chemin du top-k commence par un des `attendus` ;
  - `perimes` : notes historiques qui ne doivent PAS passer devant la premiere attendue.
"""

# ruff: noqa: E501, ARG005

from __future__ import annotations

import argparse
import json
import math
import resource
import statistics
import time
from collections import Counter
from pathlib import Path

import numpy as np

from vault_mcp.chunk import fragmenter
from vault_mcp.embed import vectoriser_un
from vault_mcp.index import Index, Resultat, fusion_rang_reciproque
from vault_mcp.lexical import IndexBM25
from vault_mcp.selection import notes


def charger_golden(chemin: Path) -> list[dict]:
    return [json.loads(ligne) for ligne in chemin.read_text(encoding="utf-8").splitlines() if ligne.strip()]


def textes_alignes(index: Index, racine: Path) -> list[str]:
    """Texte complet de chaque ligne de l'index ; aperçu si la note a change depuis."""
    par_note: dict[str, dict[int, str]] = {}
    voulues = set(index.fragments_par_note)
    for fichier in notes(racine):
        rel = fichier.relative_to(racine).as_posix()
        if rel not in voulues:
            continue
        try:
            contenu = fichier.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        par_note[rel] = {f.rang: f.texte for f in fragmenter(rel, contenu)}
    sortie, derives = [], 0
    for m in index.metas:
        t = par_note.get(m.chemin, {}).get(m.rang)
        if t is None or not t[:200].replace("\n", " ").startswith(m.apercu[:200]):
            derives += 1
            t = f"{m.titre} {m.apercu}"
        sortie.append(t)
    print(f"textes alignes : {len(sortie)} fragments, {derives} derives (aperçu)")
    return sortie


def agreger(scores: np.ndarray, par_note: dict[str, list[int]], mode: str) -> list[tuple[float, str]]:
    out = []
    for chemin, lignes in par_note.items():
        v = scores[lignes]
        if mode == "mean":
            s = float(v.mean())
        elif mode == "max":
            s = float(v.max())
        elif mode == "mix":
            s = 0.5 * float(v.max()) + 0.5 * float(v.mean())
        else:  # top2 : moyenne des deux meilleurs
            s = float(np.sort(v)[-2:].mean())
        out.append((s, chemin))
    out.sort(key=lambda t: -t[0])
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--golden", type=Path, required=True)
    p.add_argument("--index", type=Path, default=None)
    p.add_argument("--vault", type=Path, default=Path("/srv/vault-mirror"))
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--k", type=int, default=10)
    p.add_argument("--seulement", nargs="*", default=None, help="prefixes de variantes")
    a = p.parse_args()

    index = Index(a.index)
    inst = index._courant()
    par_note = inst.fragments_par_note
    golden = charger_golden(a.golden)
    t0 = time.perf_counter()
    textes = textes_alignes(index, a.vault)
    bm25 = IndexBM25.construire([f"{m.chemin} {m.titre} {t}" for m, t in zip(inst.metas, textes, strict=False)])
    print(f"BM25 construit en {time.perf_counter() - t0:.1f}s, rss={resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024} Mo")
    vect = np.asarray(inst.vecteurs, dtype=np.float32)

    def note_bm25(q: str, lim: int) -> list[str]:
        s = bm25.scores(q)
        meilleur: dict[str, float] = {}
        for i in np.nonzero(s)[0]:
            c = inst.metas[i].chemin
            if s[i] > meilleur.get(c, 0.0):
                meilleur[c] = float(s[i])
        return [c for c, _ in sorted(meilleur.items(), key=lambda kv: -kv[1])[:lim]]

    def en_resultats(chemins: list[str]) -> list[Resultat]:
        return [Resultat(c, "", "", 0.0, "") for c in chemins]

    variantes = {
        "base_vecteur": lambda q, qv, k: [r.chemin for r in index.recherche_vectorielle(q, k)],
        "base_lexical": lambda q, qv, k: [r.chemin for r in index.recherche_lexicale(q, k)],
        "base_hybride": lambda q, qv, k: [r.chemin for r in index.recherche_hybride(q, k)],
        "bm25": lambda q, qv, k: note_bm25(q, k),
    }
    for mode in ("max", "mix", "top2"):
        variantes[f"vec_{mode}"] = (
            lambda q, qv, k, mode=mode: [c for _, c in agreger(vect @ qv, par_note, mode)[:k]]
        )
    for mode in ("mean", "mix"):
        variantes[f"hyb_bm25_{mode}"] = lambda q, qv, k, mode=mode: [
            r.chemin
            for r in fusion_rang_reciproque(
                en_resultats([c for _, c in agreger(vect @ qv, par_note, mode)[: 2 * k]]), en_resultats(note_bm25(q, 2 * k)), k
            )
        ]

    from vault_mcp.autorite import historique, rang_autorite
    from vault_mcp.selection import indexable

    def hyb_prior(q: str, qv: np.ndarray, k: int, poids: float, routeur: bool) -> list[str]:
        pool = 5 * k
        vec = [c for _, c in agreger(vect @ qv, par_note, "mix") if indexable(c)][:pool]
        lex = [c for c in note_bm25(q, 2 * pool) if indexable(c)][:pool]
        cumul: dict[str, float] = {}
        for liste in (vec, lex):
            for r, c in enumerate(liste):
                cumul[c] = cumul.get(c, 0.0) + 1.0 / (61 + r)
        if not (routeur and historique(q)):
            for c in cumul:
                cumul[c] += poids * (4 - rang_autorite(c)) / 4 / 61
        return [c for c, _ in sorted(cumul.items(), key=lambda kv: -kv[1])[:k]]

    variantes["hyb_mix_notrash"] = lambda q, qv, k: hyb_prior(q, qv, k, 0.0, False)
    for poids in (0.5, 1.0, 2.0):
        variantes[f"hyb_prior{poids}"] = lambda q, qv, k, p=poids: hyb_prior(q, qv, k, p, False)
        variantes[f"hyb_prior{poids}_route"] = lambda q, qv, k, p=poids: hyb_prior(q, qv, k, p, True)
    # Implementation servie par le MCP, BM25 construit de facon synchrone pour le banc.
    import vault_mcp.index as module_index

    module_index.BM25_SYNCHRONE_MAX = 10**9
    variantes["prod_hybride"] = lambda q, qv, k: [r.chemin for r in index.recherche_hybride(q, k)]
    if a.seulement:
        variantes = {n: f for n, f in variantes.items() if n.startswith(tuple(a.seulement))}

    rapport: dict[str, dict] = {}
    details: dict[str, list] = {}
    for nom, f in variantes.items():
        lat, hits, rr, stale, fam = [], Counter(), [], 0, {}
        details[nom] = []
        for g in golden:
            debut = time.perf_counter()
            qv = vectoriser_un(g["q"])
            top = f(g["q"], qv, a.k)
            lat.append((time.perf_counter() - debut) * 1000)
            att = tuple(g["attendus"])
            rang = next((i for i, c in enumerate(top) if c.startswith(att)), None)
            for kk in (1, 3, 5, 10):
                hits[kk] += rang is not None and rang < kk
            rr.append(0.0 if rang is None else 1.0 / (rang + 1))
            per = tuple(g.get("perimes") or ())
            if per:
                rp = next((i for i, c in enumerate(top[:5]) if c.startswith(per)), None)
                if rp is not None and (rang is None or rp < rang):
                    stale += 1
            fr = fam.setdefault(g["famille"], [0, 0])
            fr[0] += rang is not None and rang < 5
            fr[1] += 1
            details[nom].append({"id": g["id"], "rang": rang, "top3": top[:3]})
        n = len(golden)
        lat.sort()
        rapport[nom] = {
            **{f"hit@{kk}": round(hits[kk] / n, 3) for kk in (1, 3, 5, 10)},
            "mrr": round(statistics.mean(rr), 3),
            "stale@5": stale,
            "p50_ms": round(lat[n // 2], 1),
            "p95_ms": round(lat[min(n - 1, math.ceil(0.95 * n) - 1)], 1),
            "familles_hit@5": {k: f"{v[0]}/{v[1]}" for k, v in sorted(fam.items())},
        }
        print(nom, json.dumps(rapport[nom], ensure_ascii=False))
    if a.out:
        a.out.write_text(json.dumps({"rapport": rapport, "details": details}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
