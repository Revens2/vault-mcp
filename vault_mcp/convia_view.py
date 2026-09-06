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


def _is_error(summary: str, body: str) -> bool:
    if "**Erreur**" in body:
        return True
    result = body.split("**Résultat** :", 1)[-1] if "**Résultat** :" in body else ""
    return any(marker in result for marker in _ERROR_MARKERS[1:])


def _tool_of(summary: str) -> str:
    match = _TOOL_NAME.search(summary)
    return match.group(1).strip() if match else summary.strip()


def _clean_prose(text: str) -> str:
    text = _THINKING.sub("", text)
    text = _REMINDER.sub("", text)
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


def render(proj: Projection) -> str:
    """Le Markdown effectivement remis à la tâche d'analyse."""
    parts = [f"# Conversation — {proj.title}" if proj.title else "# Conversation", ""]
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
