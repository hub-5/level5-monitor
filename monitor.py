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
IMG_SRC_RE = re.compile(r'<img\b[^>]*\bsrc=["\']([^"\']+)', re.I)
OG_IMAGE_RE = re.compile(
    r'<meta\b[^>]*\bproperty=["\']og:image["\'][^>]*\bcontent=["\']([^"\']+)', re.I
)
# El orden de los atributos en <meta> no está garantizado; algunas webs
# escriben content antes que property.
OG_IMAGE_ALT_RE = re.compile(
    r'<meta\b[^>]*\bcontent=["\']([^"\']+)["\'][^>]*\bproperty=["\']og:image["\']', re.I
)

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

CHARSET_META_RE = re.compile(
    rb'<meta[^>]+charset=["\']?\s*([a-zA-Z0-9_\-]+)', re.I
)

# Número de fallos consecutivos de una URL ya conocida antes de considerar
# que la página ha sido retirada y avisar de ello.
REMOVED_AFTER_FAILURES = 2

# Versión del formato en que se guarda cada página en state.json. Cuando la
# forma de decodificar/comparar el contenido cambia (como aquí, al corregir
# la detección de codificación de caracteres), se sube este número: cualquier
# entrada guardada con una versión distinta se vuelve a guardar en silencio
# (sin generar aviso de "modificada"), igual que la migración del formato
# hash -> texto. Así, una mejora interna del monitor nunca se confunde con
# un cambio real de contenido.
STATE_FORMAT_VERSION = 2


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


def extract_images(html: str, base_url: str) -> list[str]:
    """Devuelve las URLs de imagen "interesantes" de una página, absolutas y
    sin duplicados, en orden de aparición: primero la og:image (la imagen
    "representativa" que usan las redes sociales al compartir el enlace, si
    la declara la página), luego el resto de <img src=...>. Se usa para
    detectar cuándo aparece una imagen nueva en una página ya conocida
    (artwork, capturas de pantalla de un anuncio) y mostrarla incrustada en
    el aviso de Discord."""
    urls: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        raw = raw.strip()
        if not raw:
            return
        try:
            abs_url = urljoin(base_url, raw)
        except ValueError:
            return
        if abs_url not in seen:
            seen.add(abs_url)
            urls.append(abs_url)

    for m in OG_IMAGE_RE.finditer(html):
        add(m.group(1))
    for m in OG_IMAGE_ALT_RE.finditer(html):
        add(m.group(1))
    for m in IMG_SRC_RE.finditer(html):
        add(m.group(1))

    return urls


def decode_response(resp: requests.Response) -> str:
    """Decodifica el cuerpo de la respuesta a texto, adivinando bien la
    codificación de caracteres.

    "requests" decodifica resp.text usando el charset declarado en la
    cabecera HTTP Content-Type; si esa cabecera no incluye un charset (algo
    muy habitual: muchas webs, incluidas las japonesas de este proyecto,
    solo lo declaran dentro de una etiqueta <meta charset> en el propio
    HTML), requests asume ISO-8859-1 por defecto, que es lo que exige el
    estándar HTTP para texto sin charset declarado. Esto rompe cualquier
    texto no-ASCII (japonés, tildes, etc.), convirtiéndolo en secuencias
    ilegibles del tipo "å ±ï¼...". Aquí se busca primero el charset real
    dentro del propio HTML y, si no aparece en ningún sitio, se prueba UTF-8
    (con diferencia el más común) antes de rendirnos al que haya adivinado
    "requests"."""
    content_type = resp.headers.get("Content-Type", "")
    if "charset=" in content_type.lower():
        return resp.text

    m = CHARSET_META_RE.search(resp.content[:4096])
    if m:
        charset = m.group(1).decode("ascii", "ignore")
        try:
            return resp.content.decode(charset, errors="replace")
        except LookupError:
            pass

    try:
        return resp.content.decode("utf-8")
    except UnicodeDecodeError:
        resp.encoding = resp.apparent_encoding
        return resp.text


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

def _truncate(text: str, limit: int = 220) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


@dataclass
class PageChange:
    """Una novedad concreta detectada en una página: nueva, modificada,
    eliminada o reaparecida. title e image_url dan contexto en el aviso
    (Discord los muestra sin que haga falta abrir el enlace)."""
    url: str
    title: str = ""
    detail: str = ""       # diff exacto (modificada) o fragmento (nueva)
    image_url: str = ""    # imagen nueva/destacada de la página, si la hay


@dataclass
class CrawlResult:
    site_name: str
    new_pages: list[PageChange] = field(default_factory=list)
    changed_pages: list[PageChange] = field(default_factory=list)
    removed_pages: list[PageChange] = field(default_factory=list)
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
        result.removed_pages.append(PageChange(url=url, title=entry.get("title", "")))


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

        html = decode_response(resp)
        new_text = extract_text(html)
        new_hash = hashlib.sha256(new_text.encode("utf-8", "ignore")).hexdigest()
        title = extract_title(html)
        images = extract_images(html, url)

        prev_text = prev.get("text")
        prev_images = set(prev.get("images") or [])
        is_new = url not in state_pages
        # Una entrada ya existía pero se guardó con una versión anterior del
        # monitor: bien en el formato antiguo (solo hash de HTML, sin texto
        # legible), bien con una versión de STATE_FORMAT_VERSION distinta
        # (p. ej. antes de corregir la detección de codificación de
        # caracteres). En ambos casos no hay forma de calcular un diff
        # fiable contra el valor guardado, así que se migra/recalcula en
        # silencio: no se avisa de "cambio" solo por una mejora interna del
        # monitor. El primer cambio real posterior sí generará ya un diff
        # exacto.
        is_migration = (not is_new) and prev.get("fmt") != STATE_FORMAT_VERSION
        was_removed = prev.get("removed", False)

        if is_new:
            result.new_pages.append(PageChange(
                url=url, title=title, detail=_truncate(new_text),
                image_url=images[0] if images else "",
            ))
        elif is_migration:
            print(f"[INFO]   Migrando estado de {url} al nuevo formato (sin aviso).")
        elif was_removed:
            # Había sido marcada como eliminada y ha vuelto a responder: se
            # trata como una "reaparición", no como una modificación.
            result.new_pages.append(PageChange(
                url=url, title=title, detail=_truncate(new_text),
                image_url=images[0] if images else "",
            ))
        elif prev.get("hash_text") != new_hash:
            new_images = [img for img in images if img not in prev_images]
            result.changed_pages.append(PageChange(
                url=url, title=title, detail=make_diff(prev_text, new_text),
                image_url=new_images[0] if new_images else "",
            ))

        state_pages[url] = {
            "fmt": STATE_FORMAT_VERSION,
            "text": new_text,
            "hash_text": new_hash,
            "title": title,
            "images": images,
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

KIND_EMOJI = {"changed": "🔄", "new": "🆕", "removed": "🗑️"}
KIND_LABEL = {"changed": "Modificada", "new": "Nueva página", "removed": "Eliminada"}
# Colores de los embeds de Discord (decimal, formato 0xRRGGBB).
KIND_COLOR = {"changed": 0xF5A623, "new": 0x2ECC71, "removed": 0xE74C3C}

# Embeds por mensaje de Discord. El límite real de la plataforma es 10, pero
# nos quedamos por debajo para no arriesgarnos a chocar con el límite de
# 6000 caracteres combinados de un mensaje si varias páginas cambian a la
# vez con diffs largos.
EMBEDS_PER_MESSAGE = 5


def _site_entries(r: CrawlResult) -> list[tuple[str, PageChange]]:
    """Todas las novedades de un sitio, etiquetadas por tipo, en el orden en
    que se muestran: primero modificadas, luego nuevas, luego eliminadas."""
    return (
        [("changed", pc) for pc in r.changed_pages]
        + [("new", pc) for pc in r.new_pages]
        + [("removed", pc) for pc in r.removed_pages]
    )


def build_text_lines(results: list[CrawlResult], max_urls: int) -> list[str]:
    """Todos los cambios como texto plano, con el título de cada página
    junto a su URL. Se usa para Telegram y para el resumen que se imprime
    en el log de la ejecución (Discord usa embeds, ver build_discord_payloads)."""
    lines = []
    for r in results:
        entries = _site_entries(r)
        if not entries:
            continue
        lines.append(f"**{r.site_name}**")
        shown = entries[:max_urls]
        for kind, pc in shown:
            label = f"{pc.title} — {pc.url}" if pc.title else pc.url
            lines.append(f"{KIND_EMOJI[kind]} {KIND_LABEL[kind]}: {label}")
            for detail_line in pc.detail.splitlines():
                lines.append(f"      {detail_line}")
        extra = len(entries) - len(shown)
        if extra > 0:
            lines.append(f"…y {extra} más en este sitio.")
        lines.append("")
    return lines


def _build_embed(site_name: str, kind: str, pc: PageChange) -> dict:
    embed = {
        "author": {"name": f"{KIND_EMOJI[kind]} {KIND_LABEL[kind]} · {site_name}"},
        "title": _truncate(pc.title, 256) if pc.title else pc.url,
        "url": pc.url,
        "color": KIND_COLOR[kind],
    }
    if pc.detail:
        embed["description"] = pc.detail
    if pc.image_url:
        embed["image"] = {"url": pc.image_url}
    return embed


def build_discord_payloads(results: list[CrawlResult], max_urls: int) -> list[dict]:
    """Construye los payloads listos para el webhook de Discord. Cada página
    con novedades se muestra como un "embed" propio: título real de la
    página (sin tener que abrir el enlace), el diff exacto o fragmento, y
    una imagen incrustada si se detectó alguna nueva. Se agrupan en mensajes
    de hasta EMBEDS_PER_MESSAGE embeds."""
    embeds: list[dict] = []
    summary_lines: list[str] = []

    for r in results:
        entries = _site_entries(r)
        if not entries:
            continue
        shown = entries[:max_urls]
        for kind, pc in shown:
            embeds.append(_build_embed(r.site_name, kind, pc))
        extra = len(entries) - len(shown)
        if extra > 0:
            summary_lines.append(f"**{r.site_name}**: …y {extra} más no mostradas arriba.")

    if not embeds:
        return []

    header = f"📢 **Cambios detectados en webs de LEVEL-5** ({now_iso()})"
    if summary_lines:
        header += "\n" + "\n".join(summary_lines)

    payloads = []
    for i in range(0, len(embeds), EMBEDS_PER_MESSAGE):
        payload = {"embeds": embeds[i:i + EMBEDS_PER_MESSAGE]}
        if i == 0:
            payload["content"] = header
        payloads.append(payload)
    return payloads


def send_discord(webhook_url: str, payloads: list[dict]) -> None:
    for payload in payloads:
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

    if not any(_site_entries(r) for r in results):
        print("[INFO] Sin cambios detectados.")
        return 0

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if webhook_url:
        send_discord(webhook_url, build_discord_payloads(results, max_urls))
    if telegram_token and telegram_chat_id:
        send_telegram(telegram_token, telegram_chat_id, build_text_lines(results, max_urls))
    if not webhook_url and not (telegram_token and telegram_chat_id):
        print("[WARN] No hay DISCORD_WEBHOOK_URL ni TELEGRAM_BOT_TOKEN/CHAT_ID configurados; "
              "los cambios se han detectado pero no se ha enviado ninguna notificación.",
              file=sys.stderr)

    print("[INFO] Cambios notificados:")
    print("\n".join(build_text_lines(results, max_urls)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
