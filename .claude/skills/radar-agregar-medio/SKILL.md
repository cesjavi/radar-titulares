---
name: radar-agregar-medio
description: Suma un medio nuevo a Radar de Titulares (descubrir fuentes públicas, escribir el adaptador, probarlo con datos reales y documentarlo). Usar cuando el usuario pida "agregar el medio X" o cuando una fuente existente cambie de estructura.
---

# Agregar un medio a Radar de Titulares

Leé primero `AGENTS.md` (principios y restricciones). Un adaptador por medio, en `radar/collector/adapters/`. Seguí los pasos en orden; el caso de referencia es `pagina12.py`.

## 1. Descubrir las fuentes (sin inventar endpoints)
Orden de prioridad: RSS/Atom → sitemaps de noticias → portadas HTML de sección. Con `curl` y un User-Agent identificable:
1. `robots.txt`: qué permite, `Crawl-delay` y qué sitemaps declara. Respetarlo; no evadir bloqueos, muros de pago ni CAPTCHA.
2. Probar y **anotar el resultado de cada URL candidata**, incluidas las que dan 404 (van al README).
3. Mirar el índice de feeds del propio sitio (suele estar en `/rss`) en vez de adivinar rutas.
4. Inspeccionar un ítem real de cada fuente: formato de la URL, `guid`, autor, fechas, categorías.

Trampas ya vistas:
- El feed "general" puede ser **regional** (Página/12: `/arc/outboundfeeds/rss/` es Salta|12). Verificar el título del canal.
- Un sitemap puede ser **antiguo** (fechas de meses atrás): comprobar el rango de fechas.
- Los feeds pueden redirigir (301) a la URL con barra final: declarar la URL final, o cada consulta cuesta una solicitud y una pausa de cortesía extra.
- La URL puede **no traer la sección** (se toma del feed de origen con `Endpoint.section`).
- Un número al final de la URL puede ser una **fecha, no un identificador** (El Destape). Usar como `source_id` solo algo realmente único (el `guid` de Perfil y de Página/12).
- La firma puede venir con HTML y prefijo ("Por <b>Ana</b>"): `MediaAdapter.clean_author` ya lo limpia.
- Hay "clones" o notas de prueba publicadas: excluirlos en `article_path_re`.

## 2. Escribir el adaptador
Copiar la estructura de `pagina12.py` (o `perfil.py` si la URL trae la sección). Definir:
- `slug`, `name`, `base_url`, `allowed_domains`, `request_interval` (respetar `Crawl-delay`).
- `article_path_re`: qué rutas son notas (descarta secciones, etiquetas, videos y otros dominios).
- `endpoints`: `Endpoint(nombre, tipo, url, section=…, min_interval_seconds=…, enabled=…)`. Las portadas HTML van **pausadas** (`enabled=False`) como respaldo.
- `section_from_url` y `source_id` si el medio los permite; `classify` solo con **evidencia en los metadatos** (subfuentes editoriales como Canal E, republicaciones de agencia).
- En el docstring: fuentes verificadas con fecha, lo que no se usa y por qué.

Registrarlo en `radar/collector/adapters/__init__.py`. `seed.py`, las opciones `--media` de la CLI y el panel lo toman solos del registro.

Si el medio publica secciones editoriales prioritarias con un nombre distinto, sumarlas a `priority_sections` en `radar/seed.py` (sin pisar valores personalizados).

## 3. Probar
1. `python -m radar probe --media SLUG`: todas las fuentes en `[OK]` con cantidad de notas razonable.
2. `python -m radar init-db` y una ingesta real **limitada**: `RADAR_ENRICH_PER_MEDIA=5 python -m radar collect --media SLUG --force --no-analyze`.
3. Auditar los datos: duplicados por URL canónica, firmas con HTML o "Por " residual, secciones, identificadores, precisión de fechas, versiones de titular.
4. `python -m radar analyze` y **revisar a mano las relaciones nuevas** con el medio: una frase descriptiva ("hijo del expresidente…") o un tema genérico ("violencia de género") no es una coincidencia real. Anotar las débiles.
5. Fixtures **sintéticos** en `tests/fixtures/` con la estructura verificada y `tests/test_SLUG.py`: parseo, firma, identificador, sección, descarte de URLs ajenas, deduplicación entre feeds.
6. Actualizar las pruebas que fijan la lista de medios (`test_adapters.py`, `test_persistence.py`) y correr toda la suite.

## 4. Documentar
README (tabla de endpoints probados con su resultado y particularidades del medio), `PROGRESS.md` (qué se verificó, qué quedó débil) y, si cambia algo operativo, `DEPLOYMENT.md`.

## No hacer
- No inventar endpoints, ni activar HTML de respaldo por defecto, ni pasar por alto robots.txt.
- No usar noticias reales como fixtures.
- No declarar terminado sin una ingesta real y la revisión manual de las relaciones.
