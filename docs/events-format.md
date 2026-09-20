# Feed de eventos (`events.json`) y push del monitor de LEVEL-5

Documento de referencia para la app SrHub. El monitor (`level5-monitor`) escribe un
fichero `events.json` en la raíz del repositorio y envía como máximo **un** push FCM por
ejecución. Este documento describe ambos formatos.

## 1. Fichero `events.json`

- Ubicación: raíz del repositorio, rama `main`. Lo commitea el bot junto a `state.json`.
- Codificación: UTF-8 (sin BOM), JSON siempre válido (se escribe de forma atómica).
- Máximo **300** eventos, **los más recientes primero**. Al superar el tope se descartan los
  más antiguos.
- Solo se reescribe cuando hay eventos nuevos; si no existe todavía (nunca hubo cambios),
  el fichero no existe: la app debe tratar un 404 como "feed vacío".
- Un fallo generando el feed no afecta al resto del monitor: en el peor caso, una ejecución
  no añade sus eventos.

```json
{
  "schemaVersion": 1,
  "events": [
    {
      "id": "8849e6362cbe8bc6",
      "timestamp": "2026-09-20T19:45:49Z",
      "url": "https://www.inazuma.jp/re/news/",
      "franchise": "inazuma-eleven",
      "type": "PAGE_CHANGED",
      "title": "ニュース",
      "summary": "Modificada en Inazuma Eleven RE (IERE): ニュース",
      "lines": [
        { "kind": "CHANGED", "oldText": "発売日は未定です", "newText": "発売日は2026年12月です" },
        { "kind": "ADDED", "text": "新キャラクター公開 🎉" }
      ],
      "rawDetail": "✏️ 発売日は~~未定~~**2026年12月**です\n➕ 新キャラクター公開 🎉",
      "truncated": false
    }
  ]
}
```

### Raíz

| Campo | Tipo | Descripción |
|---|---|---|
| `schemaVersion` | entero | Versión del formato. Actualmente `1`. Si la app no reconoce la versión, debe ignorar el feed en vez de intentar interpretarlo. |
| `events` | lista | Eventos, más recientes primero, máximo 300. |

### Evento

| Campo | Tipo | Descripción |
|---|---|---|
| `id` | texto | Identificador único y estable (16 caracteres hexadecimales; los eventos de prueba empiezan por `test-`). Una vez generado no cambia. Es la clave para deduplicar y para relacionar con el push. |
| `timestamp` | texto | Momento de detección, ISO 8601 en UTC con sufijo `Z` (`2026-09-20T19:45:49Z`). Todos los eventos de una misma ejecución comparten timestamp. |
| `url` | texto | URL de la página. |
| `franchise` | texto | Slug de la franquicia (ver §3). `"general"` si la web no tiene franquicia asignada. |
| `type` | texto | Ver tipos. |
| `title` | texto o `null` | Título de la página (`<title>`), o `null` si no tiene. |
| `summary` | texto | Resumen de una línea en español. Nombra la web vigilada y el título (o la URL si no hay título). |
| `lines` | lista | Diff estructurado (ver §2). Vacía si no hay detalle. |
| `rawDetail` | texto | Texto original del detalle tal cual lo genera el monitor (el mismo que se envía a Discord). Si el parseo de `lines` falla o la app no lo entiende, puede mostrarse tal cual. |
| `truncated` | booleano | `true` si el detalle está recortado (ver §2). |

### Tipos (`type`)

| Valor | Significado |
|---|---|
| `PAGE_NEW` | Página nueva descubierta. `lines` es un fragmento del texto de la página. |
| `PAGE_CHANGED` | Contenido modificado. `lines` es el diff. |
| `PAGE_REMOVED` | La página dejó de responder (2 fallos consecutivos). `lines` vacía. |
| `PAGE_REAPPEARED` | Una página marcada como eliminada ha vuelto a responder. `lines` es un fragmento del texto. |
| `TEST` | Reservado: evento de prueba del flujo extremo a extremo (workflow manual). La app puede ignorarlo o mostrarlo como prueba. |

La app debe **tolerar tipos y campos desconocidos** (ignorarlos), para poder añadir nuevos en
el futuro sin romper versiones antiguas.

## 2. Detalle del cambio: `lines`, `rawDetail` y `truncated`

Las líneas son exactamente las que el monitor envía a Discord, ya recortadas por Discord
(máx. 8 líneas / 600 caracteres en un diff; 220 caracteres en un fragmento de página nueva).
La app nunca ve más de lo que ve Discord.

Cada elemento de `lines` tiene un `kind`:

| `kind` | Campos | Significado |
|---|---|---|
| `ADDED` | `text` | Línea añadida. |
| `REMOVED` | `text` | Línea quitada. |
| `CHANGED` | `oldText`, `newText` | Una sola línea con un cambio pequeño. Sin marcas markdown: la app puede pintar el tachado y el resaltado con sus propios estilos comparando ambos textos. |
| `OTHER` | `text` | Cualquier línea que no se reconoce o que no se puede interpretar con seguridad. |

Reglas:

- El orden es el del diff original.
- Los prefijos de Discord (`➕`, `➖`, `✏️`) **no** aparecen en `text`. Solo se reconocen al
  principio de la línea; un emoji igual dentro del texto no cuenta.
- **Solo `PAGE_CHANGED` se interpreta como diff.** En `PAGE_NEW` y `PAGE_REAPPEARED` el detalle
  es un fragmento del texto de la página (no un diff), así que cada línea va como `OTHER`,
  tal cual. `PAGE_REMOVED` no tiene detalle (`lines` vacía, `rawDetail` vacío).
- Una línea `✏️` pasa a `CHANGED` solo si se puede repartir con seguridad entre texto viejo y
  nuevo. Va como `OTHER` (con la línea original completa, **incluido** su prefijo `✏️` y las
  marcas `~~`/`**`) cuando: las marcas están sin cerrar o repetidas (por ejemplo, recortadas
  por el límite de caracteres), el texto real de la página contiene `**` o `~~`, hay un
  espacio cambiado (Discord lo representa con `␣`), o el texto nuevo reconstruido no existe
  como línea en la página.
- Cuando no hay diferencias de texto visible (cambio en una imagen, un enlace o un atributo),
  el monitor genera una frase explicativa; llega como una línea `OTHER`.
- `truncated` es `true` si el detalle termina en `…` (recorte por caracteres) o lleva el
  aviso "…y N líneas más de diferencia" (recorte por número de líneas; esa línea de aviso no
  se incluye en `lines`). Puede dar un falso positivo si el texto real de la página termina
  en `…`. Refleja el recorte que ya hace Discord.
- `rawDetail` está limitado a 2000 caracteres (nunca se alcanza hoy); si se recortara,
  `truncated` sería `true`.

Recomendación para la app: pintar `lines` y, si una línea no es reconocible o la lista está
vacía pero `rawDetail` no, mostrar `rawDetail`.

## 3. Franquicias

`franchise` es un **identificador estable: nunca se renombra**. La app muestra su propio
nombre visible. Varias webs pueden compartir franquicia.

| Slug | Nombre visible | Webs vigiladas |
|---|---|---|
| `level5` | LEVEL-5 | Portada, Blog de Akihiro Hino, TGS2026, VISION 2026 II |
| `fantasy-life` | Fantasy Life | Fantasy Life i (FLI) |
| `inazuma-eleven` | Inazuma Eleven | Victory Road (IEVR), Inazuma Eleven RE (IERE) |
| `professor-layton` | Professor Layton | New World of Steam (PLNWOS), Villa Misteriosa: Remake |
| `holy-horror-mansion` | Holy Horror Mansion | Holy Horror Mansion (HHM) |
| `decapolice` | Decapolice | Decapolice (DP) |
| `snack-world` | Snack World | Snack World RELOADED (SWRE) |
| `yokai-watch` | Yo-kai Watch | Yo-kai Watch 2: Hadou (YW2) |
| `general` | General | Cualquier web sin franquicia en `config.json` (y eventos de prueba) |

Si en el futuro se añade una franquicia, aparecerá un slug nuevo: la app debe mostrar
algo razonable (por ejemplo, el propio slug) para los slugs que no conozca.

## 4. Push (FCM)

- **Como máximo un push por ejecución**, aunque haya muchos eventos, y siempre **después**
  de que `events.json` esté commiteado y subido. Si el commit falla, no se envía push.
- Topic: `radar`.
- Notificación: título `SrHub: N cambios detectados` (`SrHub: 1 cambio detectado` si N=1) y
  cuerpo `Novedades en <franquicias afectadas>` (hasta 4 nombres; después "y N más").
- `data` (todos los valores son texto, como exige FCM):

| Clave | Valor |
|---|---|
| `count` | Número total de eventos de la ejecución. |
| `ids` | Ids de los eventos separados por comas, **máximo 50** (límite de 4 KB de FCM). Coinciden con `id` en `events.json`. |
| `franchises` | Slugs de las franquicias afectadas, separados por comas, sin repetir. |

Si `count` es mayor que el número de ids, la app debe completar leyendo el feed (los
eventos de la ejecución son los más recientes de `events.json`).

## 5. Cómo leer el feed desde la app

- `raw.githubusercontent.com` cachea las respuestas unos **5 minutos**. Como el push llega
  justo después del commit, una descarga inmediata puede devolver la versión anterior. Opciones:
  pedir el fichero con la API de contenidos de GitHub
  (`GET /repos/<owner>/<repo>/contents/events.json`, cabecera
  `Accept: application/vnd.github.raw+json`), o, si tras un push no aparecen los `ids`
  esperados, reintentar tras unos minutos.
- Deduplicar siempre por `id`.
- Comprobar `schemaVersion` antes de interpretar el resto.
