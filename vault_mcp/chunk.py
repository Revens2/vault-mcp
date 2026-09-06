"""Decoupage des notes en fragments indexables.

Pourquoi ce module existe : l'ancien moteur (`mcp-obsidian-semantic`) calculait **un
vecteur par note, sur ses 4 000 premiers caracteres**. Une note de 20 000 caracteres
n'existait donc, pour la recherche, que par son premier cinquieme. C'est la cause
principale du mauvais rappel -- bien avant le choix du modele.

Strategie : suivre la structure du document (titres markdown), puis fenetrer les
sections trop longues avec recouvrement. Le recouvrement evite qu'une phrase coupee
en deux au mauvais endroit disparaisse des deux fragments.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ~512 tokens pour all-MiniLM-L6-v2, a ~4 caracteres par token en francais.
# Le modele tronque au-dela de 256 tokens de toute facon : viser plus large ne
# ferait que perdre du texte silencieusement.
TAILLE_FENETRE = 1000
RECOUVREMENT = 200
TAILLE_MIN = 80

_TITRE = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)
_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)
_CHAMP_LISTE = re.compile(r"^(tags|aliases)\s*:\s*(.*)$", re.MULTILINE | re.IGNORECASE)
_LIGNE_LIEN = re.compile(r"^\s*[-*+]?\s*\[\[[^\]]+\]\]\s*$")

# Une section dont la quasi-totalite des lignes non vides sont des wikilinks est du
# balisage de navigation, pas du contenu. Seuil volontairement haut : on ecarte les
# sections « Sources liees » / « Voir aussi », pas un paragraphe qui cite des notes.
# Une section d une seule ligne qui n est qu un wikilink compte aussi : c est le cas
# le plus frequent dans ce vault, et le seuil a 2 lignes le laissait passer.
PART_LIENS_NAVIGATION = 0.8
LIGNES_MIN_NAVIGATION = 1


def metadonnees(contenu: str) -> str:
    """Tags et alias du frontmatter, en une ligne de mots-cles.

    `tags: [tools, synchronization, backup, cloud]` est exactement le vocabulaire
    qu'une requete emploie : le jeter revient a ignorer l'indexation faite a la main.
    """
    entete = _FRONTMATTER.match(contenu)
    if not entete:
        return ""
    mots: list[str] = []
    for champ in _CHAMP_LISTE.finditer(entete.group(1)):
        brut = champ.group(2).strip().strip("[]")
        mots.extend(m.strip().strip("\"'") for m in brut.split(",") if m.strip())
    return " ".join(dict.fromkeys(m for m in mots if m))


def est_navigation(section: str) -> bool:
    """Vrai si la section n'est qu'une liste de liens."""
    lignes = [ligne for ligne in section.splitlines() if ligne.strip()]
    if len(lignes) < LIGNES_MIN_NAVIGATION:
        return False
    liens = sum(1 for ligne in lignes if _LIGNE_LIEN.match(ligne))
    return liens / len(lignes) >= PART_LIENS_NAVIGATION


@dataclass(frozen=True)
class Fragment:
    """Un morceau de note, pret a etre vectorise."""

    chemin: str
    rang: int
    titre: str
    texte: str


def retirer_frontmatter(contenu: str) -> str:
    return _FRONTMATTER.sub("", contenu, count=1)


def _sections(contenu: str) -> list[tuple[str, str]]:
    """Decoupe par titres markdown. Renvoie [(titre, corps)]."""
    titres = list(_TITRE.finditer(contenu))
    if not titres:
        return [("", contenu)]

    sections: list[tuple[str, str]] = []
    preambule = contenu[: titres[0].start()].strip()
    if preambule:
        sections.append(("", preambule))

    for i, m in enumerate(titres):
        fin = titres[i + 1].start() if i + 1 < len(titres) else len(contenu)
        corps = contenu[m.end() : fin].strip()
        sections.append((m.group(2).strip(), corps))
    return sections


def _fenetrer(texte: str) -> list[str]:
    """Fenetres glissantes avec recouvrement, sur une section trop longue."""
    if len(texte) <= TAILLE_FENETRE:
        return [texte]
    pas = TAILLE_FENETRE - RECOUVREMENT
    morceaux: list[str] = []
    debut = 0
    while debut < len(texte):
        morceaux.append(texte[debut : debut + TAILLE_FENETRE])
        if debut + TAILLE_FENETRE >= len(texte):
            break
        debut += pas
    return morceaux


def fragmenter(chemin: str, contenu: str) -> list[Fragment]:
    """Transforme une note en liste de fragments indexables."""
    corps = retirer_frontmatter(contenu).strip()
    if not corps:
        return []

    fragments: list[Fragment] = []

    # Fragment d'identite : nom de la note, tags et alias declares, premieres lignes.
    # Il porte ce qu'une fiche canonique a de plus distinctif, et il est court -- donc
    # dense. Sans lui, une fiche de deux sections dont une de navigation obtenait une
    # moyenne mediocre et disparaissait du classement.
    nom = chemin.rsplit("/", 1)[-1].removesuffix(".md")
    mots_cles = metadonnees(contenu)
    # On retire les diese : le balisage de titre n apporte rien au vecteur.
    debut = " ".join(corps.replace("#", " ").split())[:400]
    identite = " ".join(part for part in (nom, mots_cles, debut) if part).strip()
    if identite:
        fragments.append(Fragment(chemin=chemin, rang=0, titre=nom, texte=identite))
    for titre, section in _sections(corps):
        if not section.strip():
            continue
        if est_navigation(section):
            continue
        fenetres = _fenetrer(section)
        for rang_fenetre, morceau in enumerate(fenetres):
            texte = morceau.strip()
            # On ecarte la *queue* d'un fenetrage (residu de quelques caracteres, qui
            # produirait un vecteur bruite), jamais une section courte : une note breve
            # est du contenu legitime, et la jeter la rendrait introuvable.
            if rang_fenetre > 0 and len(texte) < TAILLE_MIN:
                continue
            # Le titre est prefixe au texte vectorise : il porte du contexte que le
            # corps seul n'a pas ("Installation" sous "Docker" != sous "Nginx").
            entete = f"{titre}\n" if titre else ""
            fragments.append(
                Fragment(
                    chemin=chemin,
                    rang=len(fragments),
                    titre=titre,
                    texte=(entete + texte).strip(),
                )
            )
    return fragments
