"""Logique metier des ecritures : texte en entree, texte en sortie (adr/0020).

Module PUR. Aucune E/S, aucun reseau, aucun acces disque, aucun import de
`spool` ni de `mirror_store`. C est ce qui le rend testable exhaustivement, et
c est aussi ce qui garantit que `append` et `patch` produisent le contenu FINAL
complet de la note avant qu elle ne parte dans la file : le pousseur ne raisonne
jamais sur du markdown, il pousse un octet-flux.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any


class EcritureError(ValueError):
    """Operation d ecriture refusee. Message sur : ne contient pas le contenu."""


# ---------------------------------------------------------------- empreinte
# Trois fonctions de hachage, deux usages distincts (adr/0023, v4.1) :
#
#   empreinte(texte)        -- LEGACY. Hash du texte RE-ENCODE en UTF-8, apres
#                              traduction CRLF->LF. Ne correspond jamais aux
#                              octets stockes d un fichier CRLF : conserve pour
#                              compatibilite, plus utilise par le serveur.
#   empreinte_octets()      -- JETON CAS PUBLIC. SHA-256 des octets stockes,
#                              exposes tels quels par read_note_versioned
#                              (sha256_raw). C est la valeur a passer a
#                              update_note(expected_sha256=...).
#   empreinte_md5()         -- VERSION-TOKEN INTERNE DRIVE. MD5 des octets
#                              stockes, JAMAIS expose par l'API MCP : il est
#                              uniquement transporte en interne
#                              serveur -> spool -> worker. Drive expose ce md5
#                              en metadonnee (rclone lsjson --hash) : c est le
#                              seul moyen de verifier le contenu Drive sans
#                              telecharger le fichier. Suffisant comme
#                              detecteur de changement/concurrence NON
#                              adversarial (l'attaquant ne choisit pas le
#                              contenu concurrent) ; il n est PAS
#                              collision-resistant face a un writer malveillant
#                              -- SHA-256 reste le jeton public fort.

def empreinte(contenu: str) -> str:
    """SHA-256 du contenu UTF-8 normalise (LF). Legacy, voir module."""
    return hashlib.sha256(contenu.encode("utf-8")).hexdigest()


def empreinte_octets(octets: bytes) -> str:
    """SHA-256 des octets STOCKES, sans traduction de newlines.

    Jeton CAS public : sur un fichier CRLF il differe du hash du texte
    normalise -- c est exactement la divergence qui produisait les faux
    `conflit` (adr/0023).
    """
    return hashlib.sha256(octets).hexdigest()


def empreinte_md5(octets: bytes) -> str:
    """MD5 des octets STOCKES -- version-token interne Drive (jamais expose).

    Voir le commentaire de module : Drive expose ce md5 en metadonnee sans
    download ; detecteur de concurrence non adversarial uniquement.
    """
    return hashlib.md5(octets).hexdigest()


def decodage_lecture(octets: bytes) -> str:
    """Equivalent EXACT de `read_text(encoding="utf-8", errors="replace")`
    en mode newlines universels (CRLF et CR -> LF), derive des MEMES octets
    que les jetons de hachage.

    C est la reference texte unique : `lire_note` et `read_note_versioned`
    produisent le meme contenu a partir des memes octets, et ce contenu est
    toujours celui que les jetons representent.
    """
    return (
        octets.decode("utf-8", errors="replace")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )


def verifier_empreinte(contenu_actuel: str, attendue: str | None) -> None:
    """Leve si la note a change depuis sa lecture. `None` desactive le controle."""
    if attendue and empreinte(contenu_actuel) != attendue:
        raise EcritureError("conflit : la note a change depuis sa lecture")


# ------------------------------------------------------------------- append
def appliquer_append(ancien: str, ajout: str) -> str:
    """Concatene en garantissant exactement une separation de ligne.

    Ni double saut de ligne parasite, ni collage de deux paragraphes sur la meme
    ligne -- les deux sont des degradations silencieuses du markdown.
    """
    if not ancien:
        return ajout
    return ancien.rstrip("\n") + "\n" + ajout.lstrip("\n")


# -------------------------------------------------------------------- patch
def appliquer_patch(ancien: str, cible: str, remplacement: str) -> str:
    """Remplace UNE occurrence exacte, ou refuse.

    Le refus est le comportement voulu, pas une limitation. Un `replace` global
    silencieux sur une note de 400 lignes est la facon la plus simple de
    corrompre un vault sans que personne ne s en apercoive.
    """
    if not cible:
        raise EcritureError("chaine cible vide")
    occurrences = ancien.count(cible)
    if occurrences == 0:
        raise EcritureError("chaine cible introuvable")
    if occurrences > 1:
        raise EcritureError(f"chaine cible presente {occurrences} fois : preciser le contexte")
    return ancien.replace(cible, remplacement, 1)


# -------------------------------------------------------------- frontmatter
_DELIMITEUR = "---"


def appliquer_frontmatter(ancien: str, cles: dict[str, Any]) -> str:
    """Fusionne des cles dans le frontmatter YAML, en preservant tout le reste.

    Choix de conception : **aucune reserialisation YAML**. Les lignes des cles
    non concernees sont recopiees VERBATIM, y compris listes multi-lignes,
    valeurs imbriquees et commentaires. Un aller-retour par un analyseur YAML
    reordonnerait les cles, perdrait les commentaires et normaliserait les
    citations -- soit un diff enorme sur une note dont on ne voulait changer
    qu un champ. `pyyaml` est d ailleurs absent de `requirements.txt`.

    Le corps n est jamais touche, y compris s il contient lui-meme `---` (une
    ligne de separation horizontale en markdown est frequente).
    """
    if not cles:
        raise EcritureError("aucune cle a ecrire")
    for cle in cles:
        if not cle or ":" in cle or "\n" in cle or cle.strip() != cle:
            raise EcritureError(f"nom de cle invalide : {cle!r}")

    entete, corps = _decouper_frontmatter(ancien)
    restantes = dict(cles)
    sortie: list[str] = []

    for ligne in entete:
        nom = _nom_de_cle(ligne)
        if nom is not None and nom in restantes:
            sortie.append(f"{nom}: {_serialiser(restantes.pop(nom))}")
            continue
        sortie.append(ligne)

    # Les cles nouvelles vont a la fin du bloc, dans l ordre de la demande.
    sortie.extend(f"{nom}: {_serialiser(valeur)}" for nom, valeur in restantes.items())

    bloc = "\n".join([_DELIMITEUR, *sortie, _DELIMITEUR])
    return f"{bloc}\n{corps}" if corps else f"{bloc}\n"


def _decouper_frontmatter(contenu: str) -> tuple[list[str], str]:
    """Rend (lignes du frontmatter, corps). Frontmatter absent -> ([], contenu)."""
    if not contenu.startswith(_DELIMITEUR + "\n"):
        return [], contenu

    lignes = contenu.split("\n")
    for i in range(1, len(lignes)):
        if lignes[i].rstrip() == _DELIMITEUR:
            return lignes[1:i], "\n".join(lignes[i + 1 :])

    # `---` ouvrant sans fermeture : ce n est pas un frontmatter, c est du corps.
    # Le traiter comme un entete tronquerait la note.
    return [], contenu


def _nom_de_cle(ligne: str) -> str | None:
    """Nom de cle d une ligne de frontmatter de premier niveau, sinon None.

    Une ligne indentee appartient a la valeur de la cle precedente (element de
    liste, sous-cle) : la modifier casserait la structure.
    """
    if not ligne or ligne[0] in " \t-#":
        return None
    tete, separateur, _ = ligne.partition(":")
    if not separateur:
        return None
    return tete.strip() or None


def _serialiser(valeur: Any) -> str:
    """Scalaire YAML minimal. Les cas ambigus sont cites, jamais devines."""
    if isinstance(valeur, bool):
        return "true" if valeur else "false"
    if isinstance(valeur, int | float):
        return str(valeur)
    if isinstance(valeur, list):
        return "[" + ", ".join(_serialiser(element) for element in valeur) + "]"
    texte = str(valeur)
    # Une chaine vide, un caractere structurant, ou un scalaire qui se lirait
    # comme un booleen/nombre doivent etre cites pour survivre a la relecture.
    besoin_de_citation = (
        texte == ""
        or texte.strip() != texte
        or any(c in texte for c in ':#[]{},&*!|>%@`"\n')
        or texte.lower() in {"true", "false", "null", "yes", "no", "on", "off"}
        or _ressemble_a_un_nombre(texte)
    )
    if besoin_de_citation:
        return '"' + texte.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    return texte


def _ressemble_a_un_nombre(texte: str) -> bool:
    try:
        float(texte)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------- wikilinks
# Un wikilink Obsidian reference une note par son NOM, pas par son chemin :
# `[[Nom]]`, `[[Nom|alias]]`, `[[Nom#ancre]]`, `[[Nom#ancre|alias]]`. Le motif
# exige le `]]` fermant et l egalite STRICTE du nom, sans quoi renommer `Ancien`
# reecrirait aussi `[[Ancienne chose]]`.
_BLOC_DE_CODE = re.compile(r"(```.*?```|~~~.*?~~~|`[^`\n]*`)", re.DOTALL)


def reecrire_wikilinks(contenu: str, ancien_nom: str, nouveau_nom: str) -> str:
    """Reecrit `[[ancien_nom]]` et ses variantes, en preservant ancre et alias.

    Les blocs de code (clotures ou en ligne) sont EPARGNES : un `[[Nom]]` dans
    un exemple de code documente une syntaxe, il ne pointe nulle part. Le
    reecrire corromprait la documentation du vault.
    """
    if not ancien_nom or not nouveau_nom:
        raise EcritureError("nom de note vide")
    if ancien_nom == nouveau_nom:
        return contenu

    motif = re.compile(
        r"\[\[\s*" + re.escape(ancien_nom) + r"\s*(?=[#|\]])([^\]]*)\]\]",
    )

    def remplacer(correspondance: re.Match[str]) -> str:
        return f"[[{nouveau_nom}{correspondance.group(1)}]]"

    # On decoupe sur les blocs de code : les fragments impairs sont le code.
    fragments = _BLOC_DE_CODE.split(contenu)
    for i in range(0, len(fragments), 2):
        fragments[i] = motif.sub(remplacer, fragments[i])
    return "".join(fragments)


def compte_wikilinks(contenu: str, nom: str) -> int:
    """Nombre de wikilinks vers `nom`, hors blocs de code. Sert aux tests et au rapport."""
    motif = re.compile(r"\[\[\s*" + re.escape(nom) + r"\s*(?=[#|\]])[^\]]*\]\]")
    fragments = _BLOC_DE_CODE.split(contenu)
    return sum(len(motif.findall(fragments[i])) for i in range(0, len(fragments), 2))


__all__ = [
    "EcritureError",
    "appliquer_append",
    "appliquer_frontmatter",
    "appliquer_patch",
    "compte_wikilinks",
    "decodage_lecture",
    "empreinte",
    "empreinte_md5",
    "empreinte_octets",
    "reecrire_wikilinks",
    "verifier_empreinte",
]
