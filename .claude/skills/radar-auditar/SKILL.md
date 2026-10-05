---
name: radar-auditar
description: Audita Radar de Titulares contra los principios del producto (sin causalidad, réplicas, afirmaciones opuestas, revisiones, seguridad, secretos, recursos, backups) comprobando el comportamiento real y no solo la documentación. Usar antes de dar por listo un cambio grande o ante "auditá el sistema".
---

# Auditar Radar de Titulares

Inspeccioná el código **y comprobá el comportamiento**; no te basés solo en README o PROGRESS.md. No declares el sistema listo para producción si quedan fallos críticos, y no inventes noticias, relaciones, mediciones ni resultados de pruebas. Diferenciá siempre lo verificado con **fixtures** de lo verificado con **datos reales**.

## Lista de comprobación
1. **Fuentes:** cada medio tiene adaptador real; `python -m radar probe` en vivo; el estado refleja disponibilidad o bloqueo (probar con respuestas simuladas 403, 500, HTML de desafío y feed vacío).
2. **Procedencia, versiones y fechas:** `article_sightings`, `headline_versions` (origen `feed`/`pagina`) y `published_precision` (sin hora inventada).
3. **Evidencia:** toda coincidencia muestra términos, frases, reglas, versión del algoritmo y puntajes parciales.
4. **Nombre compartido ≠ relación:** revisar a mano **todas** las relaciones de la base real buscando coincidencias por un nombre frecuente o por frases descriptivas/temáticas.
5. **Afirmaciones opuestas:** negación, cese ("dejó de") y cifras distintas no se presentan como mensaje idéntico; la alerta lo advierte.
6. **Réplicas:** agencias y republicaciones no suman como medio independiente; Canal E cuenta como Perfil.
7. **Causalidad:** buscar lenguaje de origen, copia o liderazgo en plantillas y código; las métricas son descriptivas y separan detección de publicación.
8. **Alertas sin duplicar:** una por grupo; unir grupos no deja alertas pendientes duplicadas; los cambios irrelevantes no suben de versión.
9. **Revisiones humanas:** sobreviven al reprocesamiento y a `purge`.
10. **Sin IA:** la app funciona con `RADAR_AI_ENABLED=false` y sin claves.
11. **Límites de IA:** diario de solicitudes y tokens, entrada máxima, circuit breaker, cuota agotada, caché. Probar con proveedores simulados.
12. **Seguridad:** login con límite de intentos, **todo POST con CSRF** (`grep` de `@router.post` sin `verify_csrf`), sesiones HttpOnly/SameSite/Secure, SSRF (esquemas, IP privadas, redirecciones), escape de contenido externo, vista pública sin acciones.
13. **Secretos:** `grep` de claves en el árbol (excluyendo `.venv*`, `data`); `.gitignore` cubre `.env`, `.env.*`, `.venv*`, `data/`; ni logs ni errores incluyen claves.
14. **Recursos (2 GB):** sin procesos residentes extra ni dependencias pesadas; medir memoria en reposo y picos de recolección y análisis; indicar si el entorno no equivale al VPS.
15. **Backup y restauración:** `backup` con WAL activo, `restore-check` y `restore` sobre una copia.
16. **Portabilidad entre motores:** la suite pasa sobre SQLite **y** sobre PostgreSQL real (`RADAR_TEST_PG_URL`); `grep` de funciones propias de SQLite (`func.min(` / `func.max(` de dos argumentos, `PRAGMA`) fuera de `radar/db.py`; índices parciales con `postgresql_where`; largos de `String(n)` respetados al guardar.
17. **Despliegue serverless:** `app.py` sin clave por defecto, se niega a usar SQLite en Vercel, `/api/cron` exige `CRON_SECRET`, el bloqueo es entre instancias (tabla `job_locks`), la IP del visitante solo se toma de `x-forwarded-for` dentro de Vercel.
18. **Finales de línea:** los archivos de `deploy/` son LF (`bash -n` en WSL).
19. **Documentación = comandos reales:** cruzar cada `python -m radar …` de README, DEPLOYMENT y skills contra la CLI (subcomandos y opciones).

## Cómo comprobar
- Suite completa: `.\.venv-win\Scripts\python -m pytest`. Las pruebas no leen el `.env` ni llaman a servicios reales.
- Extremo a extremo con fixtures: ingesta simulada → coincidencia → grupo → alerta → revisión (`tests/test_alerts.py::test_full_flow…`).
- Datos reales limitados: `probe`, `collect --force` con `RADAR_ENRICH_PER_MEDIA` bajo, `analyze`, y revisión manual de relaciones y grupos.
- Para riesgos nuevos, reproducir primero con una prueba que falle y recién después corregir (ver `tests/test_audit.py`).

## Si cambiás el motor
Subir `ALGORITHM_VERSION`, reprocesar la base real (`python -m radar analyze`) y confirmar que no aparecen alertas "actualizadas" sin cambio relevante ni se reabren revisadas.

## Entrega
Qué funciona · qué corregiste · pruebas ejecutadas y resultados · mediciones reales disponibles · fuentes que no pudieron verificarse · limitaciones pendientes · comandos para iniciar y operar.
