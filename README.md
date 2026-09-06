# LEVEL-5 Website Monitor

Vigila varias webs de **LEVEL-5** y te avisa por Discord (o Telegram) en
cuanto detecta una página nueva o modificada. Corre solo, gratis, en la nube
de GitHub — no necesitas tener tu ordenador encendido.

Webs incluidas por defecto (`config.json`):

- **LEVEL-5** (web corporativa) — `level5.co.jp`
- **Professor Layton** — `layton.jp`
- **Inazuma Eleven** (incluye Inazuma Eleven RE, que vive dentro del mismo
  dominio) — `inazuma.jp`
- **Yo-kai Watch** — `youkai-watch.jp`
- **Fantasy Life i** — `fantasylife.jp`
- **Decapolice** — `decapolice.jp`
- **Holy Horror Mansion** — `holy-horror.jp`

> **Nota sobre Inazuma Eleven RE**: no tiene web propia, se anuncia dentro de
> `inazuma.jp`, así que ya queda cubierto por la entrada "Inazuma Eleven" —
> no hace falta (ni conviene) añadirlo por separado.

## Cómo funciona

`monitor.py` visita cada web configurada en `config.json`, sigue los enlaces
internos (hasta un límite de páginas por sitio) y calcula un hash del
contenido de cada página (quitando scripts, comentarios y espacios, para no
disparar avisos falsos por banners de cookies o contadores). Ese hash se
guarda en `state.json`. En cada ejecución compara el hash nuevo contra el
guardado: si cambia, o si aparece una URL que no existía antes, lo añade al
aviso. `state.json` se hace *commit* al propio repositorio al final de cada
ejecución para que la siguiente ejecución (que arranca desde cero, en una
máquina nueva) sepa qué vio la vez anterior.

Un `GitHub Actions workflow` (`.github/workflows/monitor.yml`) ejecuta este
script cada 10 minutos automáticamente. Con los 7 sitios de arriba (330
páginas máximo en total) cada ejecución tarda entre 3 y 5 minutos
aproximadamente, así que hay margen de sobra dentro de esos 10 minutos.

## ¿Esto es totalmente gratis?

Sí, con una condición importante: **el repositorio debe ser público**.
GitHub Actions es gratis e ilimitado (dentro de un uso razonable) en
repositorios públicos; en repositorios privados el plan gratuito solo incluye
2.000 minutos de cómputo al mes, y una comprobación cada 10 minutos los
agota enseguida. Como aquí no hay nada privado que proteger (solo vigilamos
webs públicas), un repo público no tiene ningún inconveniente — el webhook
de Discord se guarda como *secret* cifrado y nunca queda visible en el
código, ni siquiera siendo el repo público.

No hace falta ningún servidor, ningún hosting de pago ni tarjeta de crédito.

## Puesta en marcha — guía paso a paso

No necesitas compartirme ningún token ni contraseña; todo esto lo haces tú,
directamente en tu cuenta.

### Paso 1 — Crear el repositorio en GitHub

1. Entra en [github.com/new](https://github.com/new).
2. Ponle un nombre, p. ej. `level5-monitor`.
3. Marca **Public**.
4. **No** marques "Add a README file" (ya llevamos uno) — déjalo
   completamente vacío.
5. Pulsa **Create repository**. GitHub te mostrará una página con comandos;
   no los necesitas, sigue con el paso 2.

### Paso 2 — Subir los archivos

Descomprime el `.zip` que te envié en una carpeta de tu ordenador, abre una
terminal dentro de esa carpeta (`level5-monitor/`) y ejecuta, sustituyendo
`TU-USUARIO` por tu usuario de GitHub:

```bash
git init
git add -A
git commit -m "Monitor inicial de webs de LEVEL-5"
git branch -M main
git remote add origin https://github.com/TU-USUARIO/level5-monitor.git
git push -u origin main
```

Si es la primera vez que usas `git` desde ese ordenador, te pedirá iniciar
sesión con tu cuenta de GitHub (te abrirá el navegador) — es normal, solo la
primera vez.

### Paso 3 — Crear el webhook de Discord

1. En Discord, entra en el servidor donde quieras recibir los avisos (puede
   ser uno tuyo privado, con un solo canal, si no quieres usar uno existente).
2. Click derecho sobre el canal → **Editar canal** → **Integraciones** →
   **Webhooks** → **Nuevo webhook**.
3. Ponle un nombre (p. ej. "LEVEL-5 Watcher") y pulsa **Copiar URL del
   webhook**. Esa URL es todo lo que necesitas — no hace falta crear ninguna
   aplicación ni bot de Discord.

### Paso 4 — Añadir el webhook como secreto del repositorio

1. En tu repo de GitHub → **Settings** → **Secrets and variables** →
   **Actions** → **New repository secret**.
2. Nombre: `DISCORD_WEBHOOK_URL`. Valor: pega la URL que copiaste en el paso
   anterior. Guardar.

### Paso 5 — Comprobar que Actions está activo

En **Settings** → **Actions** → **General**, en "Actions permissions"
asegúrate de que está seleccionado "Allow all actions and reusable
workflows" (suele venir así por defecto en repos nuevos, pero conviene
comprobarlo).

### Paso 6 — Lanzarlo por primera vez

1. Ve a la pestaña **Actions** de tu repo. Verás el workflow "LEVEL-5
   Website Monitor" listado a la izquierda (puede tardar unos segundos en
   aparecer tras el primer push).
2. Selecciónalo → botón **Run workflow** (arriba a la derecha) → **Run
   workflow** otra vez para confirmar.
3. Espera 1-2 minutos y comprueba que termina en verde (✓). Esta primera
   ejecución **no envía ningún aviso a Discord** — solo crea la "fotografía"
   inicial de todas las páginas (verás un commit automático actualizando
   `state.json`, hecho por el propio workflow).
4. A partir de aquí, se ejecutará solo cada 10 minutos. Cualquier cambio real
   que detecte a partir de la segunda ejecución en adelante sí se enviará a
   tu Discord.

Si quieres forzar una prueba de que las notificaciones llegan sin esperar a
un cambio real: edita `state.json` en GitHub (botón del lápiz), borra el
contenido de una de las entradas dentro de `"pages"` (o dejas el fichero
como `{"pages": {}, "last_run": null}` para resetear todo), haz commit, y
lanza el workflow a mano otra vez — esa(s) página(s) se reportarán como
"nuevas" y te debería llegar el aviso a Discord.

## Cómo añadir una web nueva en el futuro

No hace falta tocar ni una línea de código. Solo edita `config.json` y añade
un objeto nuevo al array `"sites"`:

```json
{
  "name": "Nombre que quieras que aparezca en los avisos",
  "seed": "https://www.ejemplo-oficial.com/",
  "domain": "ejemplo-oficial.com",
  "max_pages": 40
}
```

- `seed`: la URL por la que el rastreador empieza a explorar (normalmente la
  portada).
- `domain`: el dominio que debe coincidir para que una URL se considere
  "de ese sitio" (incluye automáticamente subdominios, p. ej. `domain:
  "inazuma.jp"` también cubre `zukan.inazuma.jp`).
- `max_pages`: tope de páginas a rastrear en ese sitio por ejecución (súbelo
  si es una web grande y quieres cubrirla entera; bájalo si quieres que las
  ejecuciones vayan más rápido).

Guarda, haz `git add config.json && git commit -m "Añadir web X" && git push`
(o edítalo directamente en GitHub con el botón del lápiz, sin terminal) y en
la siguiente ejecución programada ya se estará vigilando. La primera vez que
vigile ese sitio nuevo, igual que en el arranque inicial, solo crea la
fotografía base sin avisar — a partir de la segunda comprobación de ese
sitio ya notificará cambios reales.

Antes de añadir una web, conviene confirmar cuál es su dominio oficial
exacto (algunas franquicias de LEVEL-5 aún no tienen web propia y su
contenido vive dentro de `level5.co.jp`, como le pasaba a Decapolice o
Fantasy Life i hasta que se les creó su propio dominio) — si no estás
seguro, pregúntamelo y lo verifico antes de añadirlo.

## Ajustar la frecuencia y el alcance

- **Frecuencia**: cambia la línea `cron: "*/10 * * * *"` en
  `.github/workflows/monitor.yml`. El mínimo que permite GitHub son 5 minutos
  (`*/5 * * * *`). GitHub puede retrasar ligeramente ejecuciones programadas
  en momentos de mucha carga en sus servidores; no es un cron perfectamente
  exacto al segundo, pero para este uso es más que suficiente.
- **`max_pages`** por sitio: cuantas más páginas rastree cada sitio, más
  tarda cada ejecución. Si añades más webs o subes `max_pages` y notas que
  las ejecuciones empiezan a tardar más de 8-9 minutos, o bien reduces
  `max_pages`, o bien alargas el cron (p. ej. `*/15`).
- **`notify_on_new_page`**: ponlo en `false` si solo quieres avisos de
  páginas *modificadas*, no de páginas nuevas.
- **`max_urls_per_notification`**: cuántas URLs como máximo detalla cada
  aviso de Discord antes de resumir el resto como "...y N más".

## Cambiar a Telegram (o usar los dos)

Pediste Discord y es la opción configurada por defecto, pero si en algún
momento quieres probar Telegram (crear el bot lleva literalmente 2 minutos
con [@BotFather](https://t.me/BotFather) en la propia app de Telegram), el
script ya lo soporta: solo tienes que añadir dos secrets más en GitHub,
`TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`, y se enviarán avisos también por
ahí (puedes tener Discord y Telegram activos a la vez, o solo uno).

## Limitaciones honestas

- **No es instantáneo al 100%**: es "casi al momento" en la escala de
  minutos (según la frecuencia que elijas), no de segundos. Un cron que
  comprobara cada pocos segundos no es viable gratis ni respetuoso con las
  webs de LEVEL-5.
- **Sitios con protección anti-bot**: si alguna web devuelve error 403 o un
  CAPTCHA de forma sistemática, este enfoque (peticiones HTTP simples) no lo
  esquiva; el log del workflow (pestaña Actions → la ejecución → "Ejecutar
  el monitor") mostrará esos errores.
- **Falsos positivos residuales**: se normaliza el HTML para ignorar
  scripts, comentarios y espacios, pero algunas webs incrustan contenido que
  cambia solo (p. ej. IDs de sesión visibles en el HTML) y podría generar
  algún aviso irrelevante de vez en cuando.
- **Se respeta `robots.txt`**: si una sección concreta de una web lo
  prohíbe expresamente, el script no la rastrea.
- **Webs sin dominio propio todavía**: alguna franquicia anunciada pero sin
  lanzar puede no tener web dedicada aún; en ese caso su contenido se
  detecta igualmente porque aparece dentro de `level5.co.jp`.

## ¿Y cómo encaja Claude / Claude Code aquí?

- **Esta misma conversación (Cowork)** ha hecho el trabajo pesado: investigar
  las URLs oficiales correctas de cada franquicia, diseñar el crawler,
  escribir el código, comprobar que compila y montar el workflow. Es la
  forma más directa de llegar de "quiero esto" a un proyecto funcionando.
- **Claude Code** (la CLI) es la herramienta natural para seguir
  manteniendo esto una vez lo tengas en tu propio repositorio: ábrelo con
  `claude` dentro de la carpeta del proyecto y pídele cosas como "añade el
  sitio X a config.json", "el workflow falla, revisa el log de Actions y
  arréglalo", o "quiero que además avise si el título de la página cambia".
  Como todo el proyecto es un repo git normal con un README, Claude Code
  puede leer el contexto completo y editar los archivos directamente.
- Para cambios pequeños de configuración (frecuencia, número de páginas,
  añadir un sitio) tampoco necesitas ninguna IA: es editar `config.json` o el
  `cron` del workflow a mano, tal como se explica arriba.
