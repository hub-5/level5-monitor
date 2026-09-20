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

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone

SCHEMA_VERSION = 1
MAX_EVENTS = 300
DEFAULT_FRANCHISE = "general"

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


# --------------------------------------------------------------------------- #
# Construcción de eventos
# --------------------------------------------------------------------------- #

def format_timestamp(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_event_id(timestamp: str, event_type: str, url: str, content_hash: str) -> str:
    """Id estable (se calcula una vez y se guarda) y único. El timestamp evita
    que colisionen dos eventos de la misma URL con el mismo contenido (una
    página que vuelve a un estado anterior)."""
    raw = "|".join((timestamp, event_type, url, content_hash))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _one_line(text: str, limit: int = 200) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


_SUMMARY = {
    TYPE_NEW: "Nueva página en {site}: {label}",
    TYPE_REMOVED: "Página eliminada en {site}: {label}",
    TYPE_REAPPEARED: "Página reaparecida en {site}: {label}",
    TYPE_CHANGED: "Modificada en {site}: {label}",
}


def build_franchise_map(config: dict) -> dict[str, str]:
    """nombre de la web -> franchise (según config.json)."""
    return {s["name"]: s.get("franchise") or DEFAULT_FRANCHISE
            for s in config.get("sites", []) if "name" in s}


def build_events(results, previously_removed: set[str], state_pages: dict,
                 franchise_by_site: dict[str, str], now: datetime) -> list[dict]:
    """Un evento por cada novedad de `results`, en el mismo orden que Discord
    (por web; dentro de cada una: modificadas, nuevas, eliminadas). Las
    "reaparecidas" llegan dentro de new_pages: se distinguen porque la URL
    figuraba como eliminada en el estado antes del rastreo."""
    timestamp = format_timestamp(now)
    events: list[dict] = []
    seen: set[str] = set()
    for r in results:
        entries = (
            [(TYPE_CHANGED, pc) for pc in r.changed_pages]
            + [(TYPE_REAPPEARED if pc.url in previously_removed else TYPE_NEW, pc)
               for pc in r.new_pages]
            + [(TYPE_REMOVED, pc) for pc in r.removed_pages]
        )
        franchise = franchise_by_site.get(r.site_name) or DEFAULT_FRANCHISE
        for etype, pc in entries:
            entry = state_pages.get(pc.url) or {}
            new_lines = None
            if etype == TYPE_CHANGED and entry.get("text"):
                new_lines = frozenset(entry["text"].splitlines())
            event_id = make_event_id(timestamp, etype, pc.url, entry.get("hash_text", ""))
            if event_id in seen:
                continue
            seen.add(event_id)
            title = pc.title or None
            events.append({
                "id": event_id,
                "timestamp": timestamp,
                "url": pc.url,
                "franchise": franchise,
                "type": etype,
                "title": title,
                "summary": _one_line(_SUMMARY[etype].format(
                    site=r.site_name, label=title or pc.url)),
                **detail_fields(etype, pc.detail, new_lines),
            })
    return events


# --------------------------------------------------------------------------- #
# Lectura / mezcla / escritura
# --------------------------------------------------------------------------- #

def load_events(path: str) -> tuple[list[dict] | None, bool]:
    """(eventos existentes, ok). Fichero inexistente -> ([], True). Fichero
    ilegible, con otro schemaVersion o con forma inesperada -> (None, False):
    quien llame NO debe sobrescribirlo, para no perder el histórico."""
    if not os.path.exists(path):
        return [], True
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None, False
    if (not isinstance(doc, dict) or doc.get("schemaVersion") != SCHEMA_VERSION
            or not isinstance(doc.get("events"), list)):
        return None, False
    return doc["events"], True


def merge_events(existing: list[dict], new: list[dict], limit: int = MAX_EVENTS) -> list[dict]:
    """Nuevos primero, luego los existentes (ya en orden más reciente primero),
    sin ids repetidos y con un máximo de `limit`."""
    merged: list[dict] = []
    seen: set[str] = set()
    for ev in list(new) + list(existing):
        ev_id = ev.get("id") if isinstance(ev, dict) else None
        if ev_id is not None:
            if ev_id in seen:
                continue
            seen.add(ev_id)
        merged.append(ev)
    return merged[:limit]


def atomic_write_json(path: str, data) -> None:
    """Escribe JSON UTF-8 válido sin reordenar claves: primero se serializa
    entero (si falla, el fichero no se toca), luego .tmp + fsync + replace."""
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    json.loads(text)
    tmp_path = f"{path}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def write_events(path: str, events: list[dict]) -> None:
    atomic_write_json(path, {"schemaVersion": SCHEMA_VERSION, "events": events})


# --------------------------------------------------------------------------- #
# Push resumen (uno por ejecución)
# --------------------------------------------------------------------------- #

# FCM admite 4 KB de data por mensaje: con ids de 16 caracteres, 50 caben de
# sobra. "count" lleva el total real y la app completa leyendo el feed.
MAX_PUSH_IDS = 50
MAX_PUSH_FRANCHISES_IN_BODY = 4


def _join_names(names: list[str]) -> str:
    if len(names) > MAX_PUSH_FRANCHISES_IN_BODY:
        rest = len(names) - MAX_PUSH_FRANCHISES_IN_BODY
        return ", ".join(names[:MAX_PUSH_FRANCHISES_IN_BODY]) + f" y {rest} más"
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " y " + names[-1]


def build_push_pending(new_events: list[dict], franchise_names: dict[str, str]) -> dict:
    """El único push de la ejecución: título con el nº de cambios, cuerpo con
    las franquicias afectadas y data (todo str, como exige FCM) con los ids."""
    count = len(new_events)
    franchises: list[str] = []
    for ev in new_events:
        if ev["franchise"] not in franchises:
            franchises.append(ev["franchise"])
    names = [franchise_names.get(f) or f for f in franchises]
    return {
        "title": "SrHub: 1 cambio detectado" if count == 1
                 else f"SrHub: {count} cambios detectados",
        "body": f"Novedades en {_join_names(names)}",
        "data": {
            "count": str(count),
            "ids": ",".join(ev["id"] for ev in new_events[:MAX_PUSH_IDS]),
            "franchises": ",".join(franchises),
        },
    }


def record_events(results, previously_removed: set[str], state_pages: dict,
                  franchise_by_site: dict[str, str], events_path: str,
                  now: datetime | None = None, dry_run: bool = False,
                  pending_path: str | None = None,
                  franchise_names: dict[str, str] | None = None) -> list[dict]:
    """Genera y guarda los eventos de esta ejecución. NUNCA propaga una
    excepción: si algo falla se avisa (solo el tipo de error) y devuelve [];
    el monitor sigue como siempre. Con dry_run no escribe nada: imprime los
    eventos por stdout. Si se indica pending_path, y solo después de haber
    escrito bien events.json, deja ahí el push pendiente (fichero temporal
    que NO se commitea; lo envía un paso posterior del workflow)."""
    try:
        now = now or datetime.now(timezone.utc)
        new = build_events(results, previously_removed, state_pages, franchise_by_site, now)
        if not new:
            return []
        if dry_run:
            print("[DRY-RUN] Eventos que se generarían:")
            print(json.dumps(new, ensure_ascii=False, indent=2))
            return new
        existing, ok = load_events(events_path)
        if not ok:
            print(f"[WARN] {events_path} existe pero no es un feed válido; no se "
                  "sobrescribe y se omiten los eventos de esta ejecución.", file=sys.stderr)
            return []
        write_events(events_path, merge_events(existing, new))
        print(f"[INFO] {len(new)} evento(s) añadidos a {events_path}.")
        if pending_path:
            try:
                atomic_write_json(pending_path,
                                  build_push_pending(new, franchise_names or {}))
            except Exception as exc:  # noqa: BLE001 - sin push, pero el feed ya está
                print(f"[WARN] No se pudo dejar el push pendiente ({type(exc).__name__}).",
                      file=sys.stderr)
        return new
    except Exception as exc:  # noqa: BLE001 - el feed nunca debe romper el monitor
        print(f"[WARN] No se pudo generar el feed de eventos ({type(exc).__name__}).",
              file=sys.stderr)
        return []
