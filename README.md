# LEVEL-5 Website Monitor

Vigila varias webs de **LEVEL-5** y te avisa por Discord en
cuanto detecta una página nueva o modificada. Corre solo, gratis, en la nube
de GitHub — no necesitas tener tu ordenador encendido.

## Cómo funciona

`monitor.py` visita cada web configurada en `config.json`, sigue los enlaces
internos (hasta un límite de páginas por sitio) y extrae el texto visible de
cada página (quitando scripts, estilos, comentarios, nonces CSP y tokens
CSRF, para no disparar avisos falsos por banners de cookies, contadores o
protecciones de seguridad que cambian en cada petición). Ese texto se guarda
en `state.json`. En cada ejecución compara el texto nuevo contra el
guardado línea a línea: si cambia, el aviso incluye exactamente qué línea se
quitó y cuál se añadió (no solo "esta página cambió"). `state.json` se hace
*commit* al propio repositorio al final de cada ejecución para que la
siguiente ejecución (que arranca desde cero, en una máquina nueva) sepa qué
vio la vez anterior.

Un `GitHub Actions workflow` (`.github/workflows/monitor.yml`) ejecuta este
script cada 10 minutos automáticamente. Cada ejecución tarda entre 3 y 5 minutos
aproximadamente, así que hay margen de sobra dentro de esos 10 minutos.
