"""
Feed de eventos para la app SrHub
---------------------------------
Convierte las novedades que ya calcula monitor.py (las mismas que van a
Discord) en eventos estructurados para events.json. Este módulo no importa
monitor.py: trabaja con objetos que tengan los mismos atributos que
PageChange/CrawlResult, y nada de lo que hace debe poder afectar a la
detección, a Discord ni a state.json (ver record_events(), que no propaga
excepciones).

El formato está documentado en docs/events-format.md.
"""

from __future__ import annotations

import re

# Tope defensivo del texto original del detalle. Hoy make_diff ya recorta a
# ~600 caracteres y _truncate a 220, así que no se alcanza nunca.
MAX_RAW_DETAIL = 2000

KIND_ADDED = "ADDED"
KIND_REMOVED = "REMOVED"
KIND_CHANGED = "CHANGED"
KIND_OTHER = "OTHER"

TYPE_NEW = "PAGE_NEW"
TYPE_REMOVED = "PAGE_REMOVED"
TYPE_REAPPEARED = "PAGE_REAPPEARED"
TYPE_CHANGED = "PAGE_CHANGED"

# Prefijos que make_diff pone a cada línea: "➕ ", "➖ ", "✏️ " (con selector de
# variación U+FE0F; se acepta también sin él).
_LINE_RE = re.compile("^(➕|➖|✏️?) (.*)$", re.S)
# Línea final que make_diff añade cuando recorta por número de líneas.
_TRAILER_RE = re.compile(r"^…y \d+ líneas más de diferencia\.$")

_DEL = "~~"
_INS = "**"


def _split_changed(rest: str) -> tuple[str, str] | None:
    """Separa una línea "✏️" de Discord en (texto viejo, texto nuevo).

    Discord marca lo quitado como ~~tachado~~ y lo añadido como **negrita**.
    Devuelve None si no se puede repartir con seguridad: marcas sin cerrar
    (p. ej. recortadas a mitad), segmentos vacíos, marcas repetidas
    ("***", "~~~"), marcas dentro de un segmento, o "␣" en un segmento
    (Discord lo usa para un espacio cambiado y no se distingue de uno real).
    """
    old: list[str] = []
    new: list[str] = []
    marked = False
    i = 0
    n = len(rest)
    while i < n:
        for marker, into_old, into_new in ((_DEL, True, False), (_INS, False, True)):
            if not rest.startswith(marker, i):
                continue
            if rest.startswith(marker[0], i + 2):
                return None  # "~~~" / "***"
            end = rest.find(marker, i + 2)
            if end == -1:
                return None  # marca sin cerrar
            seg = rest[i + 2:end]
            if not seg or _DEL in seg or _INS in seg or "␣" in seg:
                return None
            if into_old:
                old.append(seg)
            if into_new:
                new.append(seg)
            marked = True
            i = end + 2
            break
        else:
            # Texto sin marcar: igual en ambas versiones. Se toma hasta la
            # siguiente marca.
            nxt = [p for p in (rest.find(_DEL, i), rest.find(_INS, i)) if p != -1]
            stop = min(nxt) if nxt else n
            old.append(rest[i:stop])
            new.append(rest[i:stop])
            i = stop
    if not marked:
        return None
    return "".join(old), "".join(new)


def parse_detail(event_type: str, detail: str,
                 new_lines: frozenset[str] | set[str] | None = None
                 ) -> tuple[list[dict], bool]:
    """Convierte pc.detail en la lista `lines` del evento y el flag `truncated`.

    - PAGE_CHANGED: detail es la salida de make_diff; cada línea se reconoce
      por su prefijo (➕ ADDED, ➖ REMOVED, ✏ CHANGED con oldText/newText).
      Lo que no se reconoce va como OTHER con la línea original.
    - Resto de tipos: detail es un fragmento de texto de la página, no un
      diff (y una línea real podría empezar por ➕), así que cada línea va
      como OTHER sin interpretar.

    new_lines (líneas del texto nuevo de la página, si se conocen) permite
    comprobar que el newText reconstruido de una línea ✏ existe de verdad en
    la página; si no coincide (p. ej. el texto real llevaba ** o ~~
    balanceados, o la línea quedó recortada) la línea va como OTHER.

    truncated es True si el detalle termina en "…" (recorte por caracteres
    de Discord) o lleva el trailer "…y N líneas más". Puede dar un falso
    positivo si el texto real de la página acaba en "…".
    """
    truncated = detail.endswith("…")
    lines: list[dict] = []
    for raw in detail.splitlines():
        if not raw.strip():
            continue
        if event_type != TYPE_CHANGED:
            lines.append({"kind": KIND_OTHER, "text": raw})
            continue
        if _TRAILER_RE.match(raw):
            truncated = True
            continue
        m = _LINE_RE.match(raw)
        if not m:
            lines.append({"kind": KIND_OTHER, "text": raw})
            continue
        prefix, text = m.group(1), m.group(2)
        if prefix == "➕":
            lines.append({"kind": KIND_ADDED, "text": text})
        elif prefix == "➖":
            lines.append({"kind": KIND_REMOVED, "text": text})
        else:
            pair = _split_changed(text)
            if pair is None or (new_lines is not None and pair[1] not in new_lines):
                lines.append({"kind": KIND_OTHER, "text": raw})
            else:
                lines.append({"kind": KIND_CHANGED, "oldText": pair[0], "newText": pair[1]})
    return lines, truncated


def detail_fields(event_type: str, detail: str,
                  new_lines: frozenset[str] | set[str] | None = None) -> dict:
    """lines + rawDetail + truncated de un evento, aplicando MAX_RAW_DETAIL."""
    raw = detail or ""
    capped = False
    if len(raw) > MAX_RAW_DETAIL:
        raw = raw[:MAX_RAW_DETAIL].rstrip() + "…"
        capped = True
    lines, truncated = parse_detail(event_type, raw, new_lines)
    return {"lines": lines, "rawDetail": raw, "truncated": truncated or capped}
