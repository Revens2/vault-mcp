"""Projection de niveau B : la vue compacte remise à la tâche d'analyse.

Deux niveaux de nettoyage coexistent, et les confondre est l'erreur à ne pas commettre :

* **niveau A** — `convia-sanitize` sur le VPS. But : sécurité et conservation. Il retire
  les secrets et le bruit grossier mais **garde délibérément** les appels d'outils, leurs
  arguments et leurs résultats : c'est la preuve historique, et elle doit rester
  diagnosticable des années plus tard. Un fichier `convia_sanitized: 2` contient donc
  encore des centaines d'appels d'outils. Ce n'est **pas** une vue propre pour analyse.

* **niveau B** — ce module. But : donner à un modèle d'analyse la conversation humaine,
  les réponses finales, et uniquement les événements techniques qui expliquent une
  difficulté. Rien n'est écrit dans le Vault : la projection est calculée à la demande.

Le raw ConvIA n'est jamais remis tel quel à la tâche d'analyse. L'ordre est
capture → sanitization A → projection B → analyse, jamais l'inverse.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------- rédaction
# Filet de sécurité, pas la défense principale : le niveau A a déjà passé redact.py.
# Il tourne quand même ici parce que la projection est ce qui sort du périmètre, et
# qu'un corpus rattrapé avant la mise en service de la rédaction existe encore.
_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
     "<REDACTED_PRIVATE_KEY>"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "<REDACTED_API_KEY>"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), "<REDACTED_API_KEY>"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "<REDACTED_TOKEN>"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<REDACTED_API_KEY>"),
    (re.compile(r"\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
     "<REDACTED_TOKEN>"),
    (re.compile(r"(?i)\b(?:bearer|token)\s+[A-Za-z0-9._~+/=-]{20,}"), "<REDACTED_TOKEN>"),
    (re.compile(r"(?i)\b(?:authorization|cookie|set-cookie)\s*[:=]\s*\S+"),
     "<REDACTED_TOKEN>"),
    (re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret"
                r"|secret[_-]?key|totp[_-]?secret)\b\s*[:=]\s*[\"']?[A-Za-z0-9._~+/=-]{8,}[\"']?"),
     "<REDACTED_API_KEY>"),
    (re.compile(r"(?i)\b(?:password|passwd|mot[_-]?de[_-]?passe)\b\s*[:=]\s*[\"']?\S{4,}[\"']?"),
     "<REDACTED_PASSWORD>"),
    (re.compile(r"(?i)\b[A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|APIKEY|API_KEY)[A-Z0-9_]*="
                r"[\"']?[A-Za-z0-9._~+/=-]{8,}[\"']?"),
     "<REDACTED_SECRET>"),
    (re.compile(r"(?i)://[^/\s:@]+:[^/\s@]+@"), "://<REDACTED_CREDENTIALS>@"),
]


def redact(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# ------------------------------------------------------------------- découpage
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_HEADING = re.compile(r"^## (👤 User|🤖 Assistant|⚙️ System)(?: — ([0-9: \-]+))?(?: · (.+))?$")
_DETAILS = re.compile(r"<details><summary>(.*?)</summary>\n(.*?)\n</details>", re.S)
_THINKING = re.compile(r"<details><summary>💭.*?</summary>.*?</details>", re.S)
_FENCE = re.compile(r"^(`{3,})[^\n]*\n(.*?)\n\1\s*$", re.S)
_TOOL_NAME = re.compile(r"🔧\s*(.+)")
_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)

# Blocs d'enrobage injectés par le runtime dans le PREMIER message utilisateur. Ce
# n'est pas la personne qui parle : c'est le catalogue des plugins disponibles, la
# description du bac à sable, le rappel du fichier d'instructions. Sur un transcript
# Codex réel, ces trois blocs pesaient 3,2 Ko sur 5,4 — 60 % d'une projection dont
# l'analyste n'a aucun usage.
_RUNTIME_BLOCKS = re.compile(
    r"<(recommended_plugins|environment_context|INSTRUCTIONS|user_instructions|plugins"
    r"|available_tools|command-message|command-name)>.*?</\1>",
    re.S,
)
# `<command-args>` en revanche CONTIENT la demande réelle quand l'utilisateur passe
# par une commande slash : on retire la balise, jamais son contenu.
_COMMAND_ARGS = re.compile(r"</?command-args>")
# Plomberie d'orchestration inter-agents, sans charge utile exploitable.
_TASK_ENVELOPE = re.compile(
    r"^Message Type: \w+\n(?:Task name: .*\n|Sender: .*\n|Payload:[ \t]*\n?)*",
    re.M,
)

# Outils dont un succès n'apprend rien sur la difficulté rencontrée. Un ÉCHEC de ces
# mêmes outils reste retenu : « le grep n'a rien trouvé » n'est pas du bruit quand il
# explique le détour qui suit.
_LOW_SIGNAL_TOOLS = {
    "read", "glob", "grep", "ls", "find", "cat", "head", "tail", "todowrite",
    "listmcpresources", "notebookread", "webfetch", "websearch", "toolsearch",
}

_ERROR_MARKERS = ("**Erreur**", "Traceback (most recent call last)", "command not found",
                  "Permission denied", "No such file", "fatal:", "ERROR", "Exception")

MAX_TRACE_LINES = 12
MAX_EVENT_CHARS = 400
MAX_MESSAGE_CHARS = 6000


@dataclass
class Event:
    """Un fait technique retenu, avec de quoi le comprendre sans le transcript."""

    tool: str
    detail: str
    is_error: bool


@dataclass
class Projection:
    frontmatter: str
    title: str
    messages: list[tuple[str, str]] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)
    # Comptage par outil de TOUS les appels vus, retenus ou non : le résumé d'en-tête
    # dit à l'analyste ce qui a échoué sans qu'il ait à croire l'agent sur parole.
    tool_calls: dict[str, int] = field(default_factory=dict)
    tool_errors: dict[str, int] = field(default_factory=dict)
    last_tool: tuple[str, bool] | None = None


def _split_frontmatter(raw: str) -> tuple[str, str]:
    match = _FRONTMATTER.match(raw)
    if not match:
        return "", raw
    return match.group(1), raw[match.end():]


def _fence_body(block: str) -> str:
    match = _FENCE.search(block.strip())
    return match.group(2) if match else block.strip()


def _truncate_trace(text: str) -> str:
    """Une stack trace utile tient dans sa tête et sa queue : le milieu est répétitif."""
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) <= MAX_TRACE_LINES:
        out = "\n".join(lines)
    else:
        head = lines[: MAX_TRACE_LINES - 4]
        tail = lines[-3:]
        omitted = len(lines) - len(head) - len(tail)
        out = "\n".join(head + [f"[… {omitted} lignes omises …]"] + tail)
    if len(out) > MAX_EVENT_CHARS:
        out = out[:MAX_EVENT_CHARS] + " […]"
    return out


# Échecs que les marqueurs anglais ci-dessus ratent : shell Windows/PowerShell en
# français, codes de sortie, refus réseau. Mesuré le 2026-10-29 sur des transcripts
# OpenCode : « head n'est pas reconnu… », « Binary 'ls' not found on PATH » passaient
# pour des succès et l'analyste ne voyait aucune erreur.
_ERROR_RX = re.compile(
    r"n[’']est pas reconnu|is not recognized as|not found on PATH|CommandNotFoundException"
    r"|Access is denied|Accès refusé|\bintrouvable\b|Impossible de (?:trouver|charger|se connecter)"
    r"|(?im:^\s*(?:error|erreur)\s*[:\[])"
    r"|(?i:\bexit (?:code|status))[ :=]+[1-9]|returned non-zero exit status"
    r"|Connection refused|\[31;1m"
)


def _is_error(summary: str, body: str) -> bool:
    if "**Erreur**" in body:
        return True
    result = body.split("**Résultat** :", 1)[-1] if "**Résultat** :" in body else ""
    if any(marker in result for marker in _ERROR_MARKERS[1:]):
        return True
    return bool(_ERROR_RX.search(result))


def _tool_of(summary: str) -> str:
    match = _TOOL_NAME.search(summary)
    return match.group(1).strip() if match else summary.strip()


def _clean_prose(text: str) -> str:
    text = _THINKING.sub("", text)
    text = _REMINDER.sub("", text)
    text = _RUNTIME_BLOCKS.sub("", text)
    text = _TASK_ENVELOPE.sub("", text)
    text = _COMMAND_ARGS.sub("", text)
    text = _DETAILS.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def project(raw: str) -> Projection:
    """Transforme un raw ConvIA assaini en vue compacte.

    Déterministe : aucun modèle, aucun aléa. Deux appels sur le même octet donnent le
    même résultat, ce qui rend la projection cachable et l'identité d'une analyse
    reproductible.
    """
    frontmatter, body = _split_frontmatter(raw)
    lines = body.splitlines()

    title = ""
    for line in lines:
        if line.startswith("# "):
            title = line[2:].strip()
            break

    proj = Projection(frontmatter=frontmatter, title=title)
    tools_seen = 0
    tools_kept = 0
    failing_tools: set[str] = set()

    current_role = ""
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer
        if not current_role or not buffer:
            buffer = []
            return
        chunk = "\n".join(buffer)
        for match in _DETAILS.finditer(chunk):
            summary, inner = match.group(1), match.group(2)
            if summary.startswith("💭"):
                continue
            nonlocal tools_seen, tools_kept
            tools_seen += 1
            tool = _tool_of(summary)
            error = _is_error(summary, inner)
            low = tool.lower() in _LOW_SIGNAL_TOOLS
            proj.tool_calls[tool] = proj.tool_calls.get(tool, 0) + 1
            if error:
                proj.tool_errors[tool] = proj.tool_errors.get(tool, 0) + 1
            proj.last_tool = (tool, error)
            if error:
                payload = inner.split("**Erreur** :", 1)[-1]
                payload = payload.split("**Résultat** :", 1)[-1]
                proj.events.append(
                    Event(tool=tool, detail=_truncate_trace(_fence_body(payload)), is_error=True)
                )
                failing_tools.add(tool)
                tools_kept += 1
            elif tool in failing_tools and not low:
                # Reprise après échec : c'est la résolution, elle a de la valeur.
                proj.events.append(
                    Event(tool=tool, detail="reprise réussie après l'échec précédent",
                          is_error=False)
                )
                failing_tools.discard(tool)
                tools_kept += 1
        prose = _clean_prose(chunk)
        if prose:
            if len(prose) > MAX_MESSAGE_CHARS:
                prose = prose[:MAX_MESSAGE_CHARS] + "\n[… message tronqué …]"
            proj.messages.append((current_role, prose))
        buffer = []

    for line in lines:
        heading = _HEADING.match(line)
        if heading:
            flush()
            role = heading.group(1)
            # Les messages système sont du cadrage runtime, pas de la conversation.
            current_role = {"👤 User": "User", "🤖 Assistant": "Assistant"}.get(role, "")
            continue
        if line.startswith("# "):
            continue
        buffer.append(line)
    flush()

    proj.stats = {
        "messages": len(proj.messages),
        "tool_calls_seen": tools_seen,
        "tool_calls_kept": tools_kept,
        "events": len(proj.events),
    }
    return proj


MAX_SUMMARY_ERRORS = 6
MAX_SUMMARY_LINE = 160


def _first_line(text: str) -> str:
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    line = re.sub(r"\[\d+(?:;\d+)*m", "", line)  # couleurs ANSI dont l'ESC a été retiré
    return line[:MAX_SUMMARY_LINE] + (" […]" if len(line) > MAX_SUMMARY_LINE else "")


def render_tool_summary(proj: Projection) -> list[str]:
    """En-tête factuel : appels, échecs par outil, reprises, issue du dernier appel.

    Calculé par le code, pas par le modèle : c'est la seule partie de la projection qui
    dit ce que les outils ont RÉELLEMENT renvoyé, et elle survit à toute troncature
    puisqu'elle précède la conversation.
    """
    total = sum(proj.tool_calls.values())
    if not total:
        return []
    failed = sum(proj.tool_errors.values())
    resumed = sum(1 for e in proj.events if not e.is_error)
    lines = [
        "# Résumé des appels d'outils (calculé, non déclaratif)",
        "",
        f"- appels : {total} ; en échec : {failed} ; reprises réussies après échec : {resumed}",
    ]
    if proj.tool_errors:
        per_tool = ", ".join(f"{t} ×{n}" for t, n in
                             sorted(proj.tool_errors.items(), key=lambda kv: (-kv[1], kv[0])))
        lines.append(f"- outils en échec : {per_tool}")
        seen: set[str] = set()
        for event in proj.events:
            if not event.is_error:
                continue
            first = _first_line(event.detail)
            if not first or first in seen:
                continue
            seen.add(first)
            lines.append(f"- échec {event.tool} — {first}")
            if len(seen) >= MAX_SUMMARY_ERRORS:
                break
    if proj.last_tool:
        tool, error = proj.last_tool
        lines.append(f"- dernier appel d'outil : {tool} — {'échec' if error else 'succès'}")
    return lines + [""]


def render(proj: Projection) -> str:
    """Le Markdown effectivement remis à la tâche d'analyse."""
    parts = [f"# Conversation — {proj.title}" if proj.title else "# Conversation", ""]
    parts += render_tool_summary(proj)
    for role, text in proj.messages:
        parts.append(f"## {role}")
        parts.append("")
        parts.append(text)
        parts.append("")
    if proj.events:
        parts.append("# Technical events relevant to analysis")
        parts.append("")
        for event in proj.events:
            label = "erreur" if event.is_error else "suite"
            detail = event.detail.replace("\n", "\n  ")
            parts.append(f"- outil {event.tool} — {label} : {detail}")
        parts.append("")
    return redact("\n".join(parts).rstrip() + "\n")


# ------------------------------------------------------------------ compactage
# Une projection au-delà du budget d'un appel n'était jamais servie (claim : `size`) :
# 253 conversations en file le 2026-10-29, précisément les plus longues, là où se
# concentrent les difficultés. Plutôt qu'un map-reduce (N appels sur un quota gratuit),
# une vue compactée en UN appel : l'intention (début), tous les prompts de la personne
# (frictions, recadrages), l'issue (fin), et les faits techniques calculés. Chaque
# morceau est un PRÉFIXE littéral d'un message : les preuves citées restent vérifiables
# contre la projection complète.
_EVENTS_HEADING = "# Technical events relevant to analysis"
_ROLE_LINE = re.compile(r"^## (User|Assistant)$", re.M)
COMPACT_FIRST_CHARS = 4000
COMPACT_USER_CHARS = 600
COMPACT_TAIL_SHARE = 0.45
COMPACT_EVENTS_SHARE = 0.12


def _split_view(view: str) -> tuple[str, list[tuple[str, str]], str]:
    events = ""
    if "\n" + _EVENTS_HEADING + "\n" in view:
        view, events = view.split("\n" + _EVENTS_HEADING + "\n", 1)
        events = _EVENTS_HEADING + "\n" + events
    marks = list(_ROLE_LINE.finditer(view))
    head = view[: marks[0].start()] if marks else view
    messages = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(view)
        messages.append((m.group(1), view[m.end():end].strip("\n")))
    return head.rstrip() + "\n", messages, events.rstrip()


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[… message tronqué pour tenir dans un appel …]"


def _cut_lines(text: str, limit: int) -> str:
    """Tête et queue d'une liste d'événements, coupées sur des fins de ligne."""
    if len(text) <= limit:
        return text
    lines = text.splitlines()
    half = limit // 2
    head, size = [], 0
    for line in lines:
        if size + len(line) + 1 > half:
            break
        head.append(line)
        size += len(line) + 1
    tail, size = [], 0
    for line in reversed(lines[len(head):]):
        if size + len(line) + 1 > half:
            break
        tail.append(line)
        size += len(line) + 1
    tail.reverse()
    return "\n".join(head + ["- [… événements intermédiaires omis …]"] + tail)


def compact_view(view: str, budget: int) -> str:
    """Vue ≤ `budget` caractères d'une projection trop longue ; identité sinon."""
    if len(view) <= budget:
        return view
    head, messages, events = _split_view(view)
    head = _cut(head, int(budget * 0.08))
    events = _cut_lines(events, int(budget * COMPACT_EVENTS_SHARE)) if events else ""
    room = budget - len(head) - len(events) - 600
    keep: dict[int, str] = {}
    # 1. intention : le premier échange
    for i, (_role, text) in enumerate(messages[:2]):
        keep[i] = _cut(text, COMPACT_FIRST_CHARS)
    used = sum(len(t) for t in keep.values())
    # 2. issue : la fin, messages entiers, en remontant
    tail_room = int(room * COMPACT_TAIL_SHARE)
    for i in range(len(messages) - 1, 1, -1):
        text = messages[i][1]
        if len(text) > tail_room:
            if not any(j > 1 for j in keep):
                keep[i] = _cut(text, max(500, tail_room))
                used += len(keep[i])
            break
        keep[i] = text
        tail_room -= len(text)
        used += len(text)
    # 3. frictions : les prompts de la personne entre les deux, échantillonnés si besoin
    middle = [i for i, (role, _t) in enumerate(messages) if role == "User" and i not in keep]
    mid_room = room - used
    per = COMPACT_USER_CHARS + 40
    if middle and mid_room > per:
        step = max(1, -(-len(middle) * per // mid_room))
        for i in middle[::step]:
            keep[i] = _cut(messages[i][1], COMPACT_USER_CHARS)

    def assemble(chosen: dict[int, str]) -> str:
        parts = [head, f"[Vue compactée : {len(chosen)} messages sur {len(messages)} conservés,"
                       " en entier ou tronqués ; projection complète de "
                       f"{len(view)} caractères.]", ""]
        gap = 0
        for i, (role, _text) in enumerate(messages):
            if i not in chosen:
                gap += 1
                continue
            if gap:
                parts += [f"[… {gap} message(s) omis …]", ""]
                gap = 0
            parts += [f"## {role}", "", chosen[i], ""]
        if gap:
            parts += [f"[… {gap} message(s) omis …]", ""]
        if events:
            parts += [events, ""]
        return "\n".join(parts).rstrip() + "\n"

    out = assemble(keep)
    # Garde-fou : retirer d'abord les prompts intermédiaires, puis le plus ancien de la fin.
    while len(out) > budget and len(keep) > 1:
        victims = [i for i in keep if 1 < i < len(messages) - 1]
        if not victims:
            break
        del keep[victims[len(victims) // 2]]
        out = assemble(keep)
    return out if len(out) <= budget else out[:budget]


def project_markdown(raw: str) -> tuple[str, dict[str, int]]:
    """Point d'entrée : raw assaini → (vue compacte, métriques de réduction)."""
    proj = project(raw)
    view = render(proj)
    stats = dict(proj.stats)
    stats["bytes_raw"] = len(raw.encode("utf-8"))
    stats["bytes_projection"] = len(view.encode("utf-8"))
    stats["reduction_pct"] = (
        round(100 * (1 - stats["bytes_projection"] / stats["bytes_raw"]), 1)
        if stats["bytes_raw"]
        else 0.0
    )
    return view, stats
