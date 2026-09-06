#!/usr/bin/env python3
"""
LEVEL-5 Website Monitor
------------------------
Rastrea la web corporativa de LEVEL-5 y las webs oficiales de Professor
Layton e Inazuma Eleven en busca de páginas nuevas o modificadas, y avisa
por Discord (y opcionalmente Telegram) cuando detecta un cambio.

Pensado para ejecutarse periódicamente (p. ej. vía GitHub Actions) y
guardar su estado (state.json) en el propio repositorio para poder
comparar entre ejecuciones.

No requiere dependencias externas más allá de "requests".
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import sys
import time
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import unescape as unescape_html
from urllib.parse import urljoin, urlparse

import requests

CONFIG_PATH = os.environ.get("MONITOR_CONFIG", "config.json")
STATE_PATH = os.environ.get("MONITOR_STATE", "state.json")

USER_AGENT = (
    "LEVEL5-WebsiteMonitor/1.0 "
    "(+https://github.com/; monitor personal de cambios, uso no comercial)"
)

SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
WHITESPACE_RE = re.compile(r"\s+")
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
HREF_RE = re.compile(r'href=["\']([^"\'#]+)', re.I)
SITEMAP_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.I)

# Patrones de valores que cambian en cada petición HTTP aunque el contenido
# visible de la página sea idéntico (nonces de seguridad CSP, tokens CSRF,
# metaetiquetas de verificación con marca de tiempo). Si no se ignoran,
# generan avisos de "página modificada" que en realidad son falsos positivos.
NONCE_ATTR_RE = re.compile(r'\snonce=["\'][^"\']*["\']', re.I)
CSRF_META_RE = re.compile(
    r'<meta\b[^>]*name=["\'](?:csrf-token|csrf-param|_token)["\'][^>]*>', re.I
)
CSRF_INPUT_RE = re.compile(
    r'<input\b[^>]*name=["\'][^"\']*(?:csrf|_token)[^"\']*["\'][^>]*>', re.I
)

# Etiquetas que separan bloques de contenido visible (párrafos, listas,
# celdas de tabla, etc.). Se sustituyen por un salto de línea antes de quitar
# el resto de etiquetas, para que extract_text() produzca texto legible
# línea a línea en vez de una sola frase larga sin cortes.
BLOCK_TAG_RE = re.compile(
    r"</?(?:p|div|li|ul|ol|h[1-6]|br|tr|table|thead|tbody|section|article|"
    r"header|footer|nav|main|form)\b[^>]*>",
    re.I,
)
TAG_RE = re.compile(r"<[^>]+>")

# Número de fallos consecutivos de una URL ya conocida antes de considerar
# que la página ha sido retirada y avisar de ello.
REMOVED_AFTER_FAILURES = 2


# --------------------------------------------------------------------------- #
# Utilidades de E/S
# --------------------------------------------------------------------------- #

def load_json(path: str, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[WARN] No se pudo leer {path}: {exc}", file=sys.stderr)
    return default


def save_json(path: str, data) -> None:
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp_path, path)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Normalización y hashing de contenido
# --------------------------------------------------------------------------- #

def extract_title(html: str) -> str:
    m = TITLE_RE.search(html)
    if not m:
        return ""
    return WHITESPACE_RE.sub(" ", m.group(1)).strip()[:200]


def extract_links(html: str, base_url: str) -> set[str]:
    links = set()
    for href in HREF_RE.findall(html):
        href = href.strip()
        if href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        try:
            links.add(urljoin(base_url, href))
        except ValueError:
            continue
    return links


def extract_text(html: str) -> str:
    """Convierte el HTML de una página en su texto visible "limpio", una
    línea por bloque. Se usa para poder comparar el contenido real entre
    ejecuciones y generar diffs exactos (qué se ha añadido/quitado), en vez
    de un simple hash que solo dice "algo cambió".

    Igual que normalize_html(), quita antes script/style, comentarios,
    nonces CSP y tokens CSRF: no son contenido visible y solo
    ensuciarían los diffs con "cambios" que en realidad no lo son."""
    html = SCRIPT_STYLE_RE.sub("", html)
    html = COMMENT_RE.sub("", html)
    html = NONCE_ATTR_RE.sub("", html)
    html = CSRF_META_RE.sub("", html)
    html = CSRF_INPUT_RE.sub("", html)
    html = BLOCK_TAG_RE.sub("\n", html)
    html = TAG_RE.sub(" ", html)
    html = unescape_html(html)

    lines = []
    for line in html.splitlines():
        line = WHITESPACE_RE.sub(" ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def make_diff(old_text: str, new_text: str, max_lines: int = 8, max_chars: int = 600) -> str:
    """Genera un diff línea a línea legible entre dos versiones del texto
    visible de una página: qué líneas se han quitado (➖) y cuáles se han
    añadido (➕). Se recorta para no desbordar el mensaje de Discord."""
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)

    diff_lines: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag in ("delete", "replace"):
            diff_lines.extend(f"➖ {line}" for line in old_lines[i1:i2])
        if tag in ("insert", "replace"):
            diff_lines.extend(f"➕ {line}" for line in new_lines[j1:j2])

    if not diff_lines:
        return (
            "(se detectó un cambio pero sin diferencias de texto visible; "
            "puede tratarse de una imagen, un enlace o un atributo no visible)"
        )

    shown = diff_lines[:max_lines]
    text = "\n".join(shown)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    extra = len(diff_lines) - len(shown)
    if extra > 0:
        text += f"\n…y {extra} líneas más de diferencia."
    return text


# --------------------------------------------------------------------------- #
# robots.txt
# --------------------------------------------------------------------------- #

_robots_cache: dict[str, urllib.robotparser.RobotFileParser] = {}


def is_allowed(url: str, session: requests.Session) -> bool:
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    rp = _robots_cache.get(origin)
    if rp is None:
        rp = urllib.robotparser.RobotFileParser()
        try:
            resp = session.get(
                urljoin(origin, "/robots.txt"),
                headers={"User-Agent": USER_AGENT},
                timeout=10,
            )
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
            else:
                rp.parse([])  # sin robots.txt -> se permite todo
        except requests.RequestException:
            rp.parse([])
        _robots_cache[origin] = rp
    try:
        return rp.can_fetch(USER_AGENT, url)
    except Exception:
        return True


# --------------------------------------------------------------------------- #
# Descubrimiento vía sitemap.xml
# --------------------------------------------------------------------------- #

def discover_sitemap_urls(seed: str, session: requests.Session) -> set[str]:
    parsed = urlparse(seed)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    found: set[str] = set()
    for path in ("/sitemap.xml", "/sitemap_index.xml"):
        url = urljoin(origin, path)
        try:
            resp = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=10)
        except requests.RequestException:
            continue
        if resp.status_code != 200:
            continue
        locs = SITEMAP_LOC_RE.findall(resp.text)
        found.update(locs)
    return found


# --------------------------------------------------------------------------- #
# Rastreo (crawl) de un sitio
# --------------------------------------------------------------------------- #

@dataclass
class CrawlResult:
    site_name: str
    new_pages: list[tuple[str, str]] = field(default_factory=list)
    changed_pages: list[tuple[str, str]] = field(default_factory=list)
    removed_pages: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    visited_count: int = 0


def _register_failure(url: str, state_pages: dict, result: CrawlResult) -> None:
    """Cuenta fallos consecutivos de una URL que ya conocíamos (existe en
    state_pages). Tras REMOVED_AFTER_FAILURES fallos seguidos, se considera
    que la página ha sido retirada y se avisa una única vez. Las URLs que
    nunca respondieron con éxito (p. ej. una página todavía no publicada)
    no están en state_pages, así que no generan aviso de "eliminada": solo
    quedan registradas como error en el log."""
    entry = state_pages.get(url)
    if entry is None:
        return
    entry["consecutive_errors"] = entry.get("consecutive_errors", 0) + 1
    entry["last_checked"] = now_iso()
    if entry["consecutive_errors"] >= REMOVED_AFTER_FAILURES and not entry.get("removed"):
        entry["removed"] = True
        result.removed_pages.append(url)


def is_internal(url: str, domain: str) -> bool:
    netloc = urlparse(url).netloc.lower()
    return netloc == domain or netloc.endswith("." + domain)


def seed_scope_prefix(seed: str) -> str:
    """Prefijo de ruta al que se limita el rastreo de un sitio: la propia
    URL semilla, terminada en "/". Así, dos entradas en el mismo dominio
    pero con rutas distintas (p. ej. inazuma.jp/victory-road/ e
    inazuma.jp/re/) no se mezclan entre sí ni siguen enlaces del resto del
    dominio."""
    return seed if seed.endswith("/") else seed + "/"


def in_scope(url: str, domain: str, scope_prefix: str, seed: str) -> bool:
    if not is_internal(url, domain):
        return False
    if url == seed:
        return True
    return url.startswith(scope_prefix)


def crawl_site(site: dict, state_pages: dict, session: requests.Session,
                delay: float, timeout: int) -> CrawlResult:
    name = site["name"]
    seed = site["seed"]
    domain = site["domain"].lower()
    max_pages = int(site.get("max_pages", 60))
    scope_prefix = seed_scope_prefix(seed)

    result = CrawlResult(site_name=name)

    to_visit: list[str] = [seed]
    seen: set[str] = {seed}
    # IMPORTANTE: discover_sitemap_urls() devuelve un set (sin orden garantizado
    # entre ejecuciones distintas, por la aleatoriedad de hashing de Python en
    # cada proceso nuevo). Si no lo ordenamos, cada ejecución en GitHub Actions
    # recorrería un subconjunto distinto de páginas cuando el sitio tiene más
    # páginas que "max_pages", generando falsos avisos de "página nueva" cada
    # vez que cambia el orden. Ordenar alfabéticamente hace el recorrido
    # determinista: mismas páginas visitadas mientras el sitio no cambie.
    for sm_url in sorted(discover_sitemap_urls(seed, session)):
        if in_scope(sm_url, domain, scope_prefix, seed) and sm_url not in seen:
            to_visit.append(sm_url)
            seen.add(sm_url)

    while to_visit and result.visited_count < max_pages:
        url = to_visit.pop(0)

        if not is_allowed(url, session):
            continue

        prev = state_pages.get(url, {})
        headers = {"User-Agent": USER_AGENT}
        if prev.get("etag"):
            headers["If-None-Match"] = prev["etag"]
        if prev.get("last_modified"):
            headers["If-Modified-Since"] = prev["last_modified"]

        try:
            resp = session.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        except requests.RequestException as exc:
            result.errors.append(f"{url} -> {exc}")
            _register_failure(url, state_pages, result)
            time.sleep(delay)
            continue

        result.visited_count += 1

        if resp.status_code == 304:
            # Sin cambios; ya conocemos esta página, no hace falta reprocesarla.
            state_pages.setdefault(url, prev)
            state_pages[url]["last_checked"] = now_iso()
            state_pages[url]["consecutive_errors"] = 0
            time.sleep(delay)
            continue

        if resp.status_code != 200:
            result.errors.append(f"{url} -> HTTP {resp.status_code}")
            _register_failure(url, state_pages, result)
            time.sleep(delay)
            continue

        content_type = resp.headers.get("Content-Type", "")
        if "text/html" not in content_type and content_type != "":
            time.sleep(delay)
            continue

        html = resp.text
        new_text = extract_text(html)
        new_hash = hashlib.sha256(new_text.encode("utf-8", "ignore")).hexdigest()
        title = extract_title(html)

        prev_text = prev.get("text")
        is_new = url not in state_pages
        # Una entrada ya existía pero fue guardada por una versión anterior
        # del monitor (basada en hash de HTML, sin texto legible). No hay
        # forma de calcular un diff fiable contra ella, así que se migra en
        # silencio al nuevo formato: no se avisa de "cambio" solo por el
        # cambio de formato interno. El primer cambio real posterior sí
        # generará ya un diff exacto.
        is_migration = (not is_new) and prev_text is None
        was_removed = prev.get("removed", False)

        if is_new:
            result.new_pages.append((url, new_text[:300]))
        elif is_migration:
            print(f"[INFO]   Migrando estado de {url} al nuevo formato (sin aviso).")
        elif was_removed:
            # Había sido marcada como eliminada y ha vuelto a responder: se
            # trata como una "reaparición", no como una modificación.
            result.new_pages.append((url, new_text[:300]))
        elif prev.get("hash_text") != new_hash:
            result.changed_pages.append((url, make_diff(prev_text, new_text)))

        state_pages[url] = {
            "text": new_text,
            "hash_text": new_hash,
            "title": title,
            "etag": resp.headers.get("ETag", ""),
            "last_modified": resp.headers.get("Last-Modified", ""),
            "last_checked": now_iso(),
            "consecutive_errors": 0,
            "removed": False,
        }

        # Solo seguimos enlaces de páginas nuevas para nosotros o recién
        # descubiertas por el sitemap; si no ha cambiado no hace falta
        # reextraer enlaces (ya los vimos en una pasada anterior).
        # Igual que con el sitemap: extract_links() devuelve un set, así que
        # lo ordenamos antes de añadirlo a la cola para que el recorrido sea
        # determinista entre ejecuciones (ver comentario más arriba).
        for link in sorted(extract_links(html, url)):
            link = link.split("#", 1)[0]
            if link in seen:
                continue
            if in_scope(link, domain, scope_prefix, seed):
                seen.add(link)
                to_visit.append(link)

        time.sleep(delay)

    return result


# --------------------------------------------------------------------------- #
# Notificaciones
# --------------------------------------------------------------------------- #

def _truncate(text: str, limit: int = 220) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def build_message_lines(results: list[CrawlResult], max_urls: int) -> list[str]:
    lines = []
    for r in results:
        if not r.new_pages and not r.changed_pages and not r.removed_pages:
            continue
        lines.append(f"**{r.site_name}**")
        shown = 0
        total = len(r.changed_pages) + len(r.new_pages) + len(r.removed_pages)

        for url, diff in r.changed_pages:
            if shown >= max_urls:
                break
            lines.append(f"🔄 Modificada: {url}")
            for diff_line in diff.splitlines():
                lines.append(f"      {diff_line}")
            shown += 1

        for url, snippet in r.new_pages:
            if shown >= max_urls:
                break
            lines.append(f"🆕 Nueva página: {url}")
            snippet = _truncate(snippet)
            if snippet:
                lines.append(f"      {snippet}")
            shown += 1

        for url in r.removed_pages:
            if shown >= max_urls:
                break
            lines.append(f"🗑️ Eliminada: {url}")
            shown += 1

        if total > shown:
            lines.append(f"…y {total - shown} más en este sitio.")
        lines.append("")
    return lines


def send_discord(webhook_url: str, lines: list[str]) -> None:
    content = "\n".join(lines).strip()
    if not content:
        return
    # Discord limita cada mensaje a 2000 caracteres.
    chunks = [content[i:i + 1900] for i in range(0, len(content), 1900)] or [content]
    for i, chunk in enumerate(chunks):
        payload = {
            "content": (
                f"📢 **Cambios detectados en webs de LEVEL-5** ({now_iso()})\n\n{chunk}"
                if i == 0 else chunk
            )
        }
        try:
            resp = requests.post(webhook_url, json=payload, timeout=15)
            if resp.status_code >= 300:
                print(f"[WARN] Discord respondió {resp.status_code}: {resp.text}", file=sys.stderr)
        except requests.RequestException as exc:
            print(f"[WARN] Fallo enviando a Discord: {exc}", file=sys.stderr)


def send_telegram(bot_token: str, chat_id: str, lines: list[str]) -> None:
    content = "\n".join(lines).strip()
    if not content:
        return
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    chunks = [content[i:i + 3500] for i in range(0, len(content), 3500)] or [content]
    for chunk in chunks:
        try:
            resp = requests.post(
                url,
                json={"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True},
                timeout=15,
            )
            if resp.status_code >= 300:
                print(f"[WARN] Telegram respondió {resp.status_code}: {resp.text}", file=sys.stderr)
        except requests.RequestException as exc:
            print(f"[WARN] Fallo enviando a Telegram: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main() -> int:
    config = load_json(CONFIG_PATH, None)
    if config is None:
        print(f"[ERROR] No se encontró {CONFIG_PATH}", file=sys.stderr)
        return 1

    state = load_json(STATE_PATH, {"pages": {}, "last_run": None})
    state_pages = state.setdefault("pages", {})

    delay = float(config.get("request_delay_seconds", 0.35))
    timeout = int(config.get("request_timeout_seconds", 15))
    notify_new = bool(config.get("notify_on_new_page", True))
    max_urls = int(config.get("max_urls_per_notification", 15))

    session = requests.Session()

    results: list[CrawlResult] = []
    first_run = len(state_pages) == 0

    for site in config["sites"]:
        print(f"[INFO] Rastreando {site['name']} ({site['seed']}) ...")
        r = crawl_site(site, state_pages, session, delay, timeout)
        print(
            f"[INFO]   -> {r.visited_count} páginas comprobadas, "
            f"{len(r.new_pages)} nuevas, {len(r.changed_pages)} modificadas, "
            f"{len(r.removed_pages)} eliminadas, {len(r.errors)} errores."
        )
        for err in r.errors[:5]:
            print(f"[WARN]   {err}", file=sys.stderr)
        results.append(r)

    state["last_run"] = now_iso()
    save_json(STATE_PATH, state)

    if first_run:
        # En la primera ejecución solo se establece la línea base:
        # no tiene sentido notificar "todo es nuevo".
        print("[INFO] Primera ejecución: estado inicial guardado, no se notifica.")
        return 0

    if not notify_new:
        for r in results:
            r.new_pages = []

    lines = build_message_lines(results, max_urls)
    if not lines:
        print("[INFO] Sin cambios detectados.")
        return 0

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if webhook_url:
        send_discord(webhook_url, lines)
    if telegram_token and telegram_chat_id:
        send_telegram(telegram_token, telegram_chat_id, lines)
    if not webhook_url and not (telegram_token and telegram_chat_id):
        print("[WARN] No hay DISCORD_WEBHOOK_URL ni TELEGRAM_BOT_TOKEN/CHAT_ID configurados; "
              "los cambios se han detectado pero no se ha enviado ninguna notificación.",
              file=sys.stderr)

    print("[INFO] Cambios notificados:")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
