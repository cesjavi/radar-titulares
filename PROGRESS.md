# PROGRESS — Radar de Titulares

## Etapa 1: base funcional (2026-10-04) ✅

### Terminado
- Proyecto creado desde cero; el repositorio estaba vacío y no había AGENTS.md.
- Modelo y migración inicial `0001`: `media`, `subsources`, `articles`, `headline_versions`, `collection_runs`, `article_relations`, `topics`, `story_groups`, `story_group_members`, `alerts`, `manual_reviews`, `users` y `login_attempts`.
- Recolector de RSS 2.0, Atom y sitemaps de noticias, con GET condicional, límite de tamaño, lock contra ejecuciones simultáneas e intervalo mínimo por subfuente. Registra cada ejecución en `collection_runs`.
- Historial de titular y bajada: se crea una versión nueva solo cuando cambia el hash de contenido.
- Fuentes reales verificadas: Perfil (RSS), El Destape (sitemap de noticias) e Infobae (RSS y sitemap de noticias). En la primera corrida real entraron unas 345 notas, sin errores, y apareció un cambio real de titular en Infobae.
- Panel: login, resumen, listado paginado con filtros por medio, tema y texto (con HTMX), detalle con historial, estado de fuentes (pausar y activar con HTMX) y configuración de temas y contraseña.
- Seguridad: Argon2id, cookie HttpOnly/SameSite/Secure en producción, CSRF, límite de intentos de login, CSP estricto y defusedxml.
- CLI: `init-db`, `create-admin`, `set-password`, `collect`, `demo-load` y `demo-clear`.
- Modo demo con medios y titulares ficticios marcados.
- Unidades systemd: web con un solo worker, más la recolección como oneshot con su timer.
- 38 pruebas (login, permisos, persistencia, parsers, fechas, recolector simulado, escape y demo), todas en verde.

### Decisiones
- **Sesión firmada en la cookie** (Starlette `SessionMiddleware`) en lugar de sesiones guardadas en el servidor. Es suficiente para un solo usuario administrador. Para revocar sesiones se usa `users.session_version`.
- **CSRF con token sincronizado guardado en la sesión.** Va como campo oculto en los formularios y como encabezado `X-CSRF-Token` en HTMX, que lo toma de `hx-headers` en `<body>`.
- **Límite de login guardado en SQLite**, no en memoria, para que sobreviva a los reinicios. `collect` purga los intentos de más de 7 días.
- **Fechas:** se usa `datetime` UTC naive en SQLite, con `published_date` y `*_precision` aparte para no inventar una hora. Una fecha con hora pero sin zona se interpreta en hora de Buenos Aires.
- **Unicidad de artículos** por (`media_id`, `canonical_url`). La URL canónica sale de normalizar la URL del feed (https, sin fragmento y sin `utm_*`); todavía no se lee `<link rel=canonical>` de la página.
- **Varias subfuentes por medio:** si un dato falta en una subfuente (por ejemplo, el sitemap no trae bajada), no se borra lo que aportó otra ni se genera una versión nueva.
- **Búsqueda y temas** sobre `articles.search_text`, que guarda titular, bajada y palabras clave en minúsculas y sin tildes, con `LIKE` de SQLite.
- **Modo demo** con medios inventados y dominios `.invalid` (no con los medios reales), para que ningún ejemplo se pueda tomar por una noticia real.
- **Sin feedparser:** el parser propio con defusedxml cubre los tres formatos y da control sobre las fechas.
- **Argon2** con 64 MiB por hash, una cantidad moderada para el VPS de 2 GB.

## Etapa 2: recolección real de los tres medios (2026-10-04) ✅

### Terminado
- **Descubrimiento documentado** (tabla en el README): se probaron RSS/Atom, sitemaps de noticias y secciones HTML de los tres medios, y se revisó robots.txt. `python -m radar probe` repite la verificación.
- **Un adaptador independiente por medio** (`radar/collector/adapters/`), cada uno con sus fuentes, reglas de URL de nota, identificador de origen y subfuentes editoriales:
  - Perfil: 3 RSS + sitemap de noticias, más 2 secciones HTML de respaldo.
  - El Destape: sitemap de noticias, más 2 secciones HTML de respaldo.
  - Infobae: RSS general + 2 RSS por categoría con cuerpo + 3 sitemaps, más 2 secciones HTML de respaldo.
- **Cliente HTTP seguro** (`radar/net.py`): anti-SSRF (dominios permitidos, DNS e IP públicas, cada redirección validada), reintentos limitados con backoff, límite por dominio, robots.txt con Crawl-delay y tamaño máximo de respuesta.
- **Recolector:** como máximo 2 descargas simultáneas, escritura en el hilo principal con transacciones breves, bloqueo entre procesos, ETag/Last-Modified y aislamiento de fallos. Distingue los estados ok, sin cambios, vacía, error y bloqueada.
- **Lectura acotada de páginas** de notas, solo metadatos: bajada, autor, fechas, canónica, referencias. Valida la URL final y la canónica.
- **Normalización:**
  - Deduplicación por URL canónica y por identificador de origen, protegida contra identificadores repetidos.
  - Parámetros de seguimiento explícitos; los funcionales se conservan.
  - Versiones con su origen (`feed` o `pagina`).
  - Procedencia por subfuente (`article_sightings`) y referencias explícitas a otros medios (`media_references`).
- **Subfuentes editoriales de Perfil** detectadas por la URL: Canal E, Revista Noticias y Perfil Córdoba. Bloomberg queda marcado como republicado. Todas cuentan como Perfil.
- **Panel:**
  - Por medio y por subfuente: estado operativo, último intento, último éxito, noticias, cobertura (titular, bajada o cuerpo) y error actual.
  - Una fuente caída o bloqueada muestra "sin datos", no "0".
  - Detalle de nota con procedencia, referencias e identificador.
  - Secciones prioritarias configurables.
- **Migración `0002`**, que completa la procedencia de los artículos existentes.
- El timer quedó cada 10 minutos.
- **101 pruebas en verde.** Las nuevas cubren adaptadores (fixtures sintéticos con la estructura verificada), red y SSRF, recolector (deduplicación, cambios, fechas, fallos, 304, concurrencia, lock, idempotencia, lectura de páginas) y panel.

### Recolección real (2026-10-04, red disponible)
- 11 subfuentes activas, todas `ok`; 369 notas nuevas en una ejecución, y 15 páginas leídas (5 por medio) sin bloqueos. Duración: unos 75 s, la mayor parte por el Crawl-delay de El Destape.
- Cobertura observada:
  - Infobae: 82 % con bajada y 86 % con cuerpo.
  - Perfil: 77 % con bajada.
  - El Destape: casi todo solo titular; la bajada se completa leyendo páginas, 10 por ejecución.
- **Error encontrado y corregido:** al principio se usó el número final de las URLs de El Destape como identificador. Con datos reales aparecieron notas distintas con el mismo número (es la hora de publicación), lo que fusionó notas en la base local. Se dejó de usar, se agregó la salvaguarda de similitud de titulares, se probó con un caso que reproduce el problema y se limpiaron los artículos afectados en la base local.

### Decisiones
- **Secciones HTML como respaldo pausado:** los feeds y sitemaps ya cubren lo mismo con menos peso y de forma más estable. Se activan desde el panel si un feed falla.
- **Feeds de Infobae por categoría (~1 MB) cada 20 minutos:** aportan el cuerpo de política y economía sin cargar demasiado.
- **El cuerpo solo se guarda cuando el feed lo publica** (Infobae). No se extrae el cuerpo de las páginas: el sistema funciona con titular y bajada.
- **Bloqueos:** 401, 403, 451, 429 persistente y páginas de desafío o CAPTCHA se registran como "bloqueada". No se reintentan ni se evaden.
- **Escritura en un solo hilo:** la concurrencia solo se usa para la red; SQLite recibe escrituras breves de un único hilo.
- **Lista de medios conocidos para las referencias** (`references.py`), con patrones sensibles a mayúsculas para no confundir "perfil", "ámbito" o "nación" con palabras comunes.

## Etapa 3: coincidencias y grupos (2026-10-04) ✅

### Terminado
- **Motor léxico sin IA** (`radar/analysis/`, versión `lexico-1.0`; desde la etapa 7, `lexico-1.1`):
  - Normalización en español que conserva negaciones, cese ("dejó de"), cifras y nombres.
  - TF-IDF disperso propio (palabras, bigramas y n-gramas de caracteres) con similitud coseno.
  - Detección de entidades por heurística.
- **Relaciones:** titular idéntico o casi idéntico, expresión distintiva o cita compartida, posible mismo hecho, mismo tema y entidades, reaparición (antecedentes de hasta 90 días), referencia explícita (enlace o mención) y afirmaciones distintas. Cada una guarda los puntajes parciales, la evidencia, las reglas, la versión, el estado de revisión y la diferencia temporal (solo si es fiable).
- **Eficiencia:**
  - Ventana de 72 h y antecedentes con selección acotada.
  - Índice invertido de términos poco frecuentes, en lugar de comparar todo contra todo, con un máximo de 40 candidatos por nota.
  - Vocabulario acotado y carga de relaciones en lotes.
- **Grupos con validación de coherencia contra el encadenamiento:**
  - Cada grupo tiene nombre (el de la nota más central), medios y medios independientes (sin contar republicaciones), expresiones comunes, primera aparición observada y una explicación con aristas, coherencia y advertencias.
  - La cronología solo ordena las notas que tienen hora.
- **Interfaz:**
  - `/grupos` y su detalle.
  - `/relaciones/{id}`: comparación lado a lado con resaltado y escape.
  - Confirmar o rechazar relaciones; separar, quitar o unir grupos.
  - Las correcciones quedan en `manual_reviews` y se respetan al reprocesar (revisiones intactas, grupos `locked`, notas excluidas).
- **CLI:** `analyze`; `collect` corre el análisis al final, salvo con `--no-analyze`. La migración es la `0003`.
- **129 pruebas en verde.** Las nuevas cubren los casos pedidos:
  - mismo nombre con temas distintos;
  - mismo hecho con titulares distintos;
  - afirmaciones opuestas (mora aumentó / no aumentó / dejó de aumentar);
  - réplicas de Perfil (Canal E, Bloomberg) que no suman medios independientes;
  - fechas incompletas sin secuencia;
  - cadenas débiles sin fusión;
  - correcciones manuales que persisten tras reprocesar;
  - idempotencia;
  - interfaz y permisos.

### Calibración con datos reales (unas 700 notas en 72 h)
- **Primera versión:** daba 104 "expresiones compartidas", casi todas falsas: nombres propios ("lula da silva", "fallo de la corte"), fechas ("sábado 3 de octubre") y fórmulas ("primera vez en la historia").
- **Ajuste:** se exigieron 2 o más palabras de contenido que no sean nombres ni fechas, una similitud mínima entre las notas, y mayor rareza para las frases de solo 2 palabras ("boca de urna" unía elecciones de países distintos). Quedaron unas 13 expresiones, revisadas a mano y razonables: el robo del celular a Paulón, la renuncia de Claudia Testa, "pollo superó a la carne vacuna", "estamos creciendo menos".
- **Bug corregido:** las relaciones se cargaban por lotes exigiendo que ambos extremos estuvieran en el mismo lote, y se perdían grupos (había 11 clusters y se guardaban 5).
- **Resultado:** 10 grupos coherentes entre 2 y 3 medios.

### Incidente
- La base local de desarrollo `data/radar.db` quedó dañada ("database disk image is malformed"). Lo más probable es que se haya usado a la vez desde Windows y desde WSL: el `.venv` se recreó desde WSL a las 19:35, y SQLite en modo WAL sobre `/mnt/f` no coordina bloqueos entre los dos sistemas.
- Se conservó el archivo como `data/radar.db.dañada-20261004-1942`, se recreó la base y se volvió a recolectar. El README ahora advierte del problema.

### Decisiones
- **TF-IDF propio en lugar de scikit-learn:** evita numpy y scipy (decenas de MB) en el VPS de 2 GB; el volumen (miles de notas) no lo justifica.
- **El análisis corre en el mismo proceso que la recolección**, después de ella y bajo el mismo bloqueo. No necesita otro timer.
- **Una relación "principal" por par**, más la referencia explícita aparte. Los tipos son exclusivos en este orden: polaridad, idéntico, casi idéntico, expresión, mismo hecho, mismo tema.
- **Los pares del mismo medio no generan relaciones entre medios.** Perfil y Canal E comparten `media_id`, así que nunca se presentan como coincidencia entre medios distintos.

## Etapa 4: alertas y panel de análisis (2026-10-04) ✅

### Terminado
- **Alertas** (`radar/alerts/`): una por grupo, con al menos 2 medios independientes.
  - Prioridad configurable: baja = tema compartido; media = mismo hecho con proximidad; alta = expresión distintiva, titular compartido o enfoque respaldado por análisis.
  - Evidencia completa: titulares, medios, fechas y precisión, frases, relaciones, puntajes, reglas, enlaces y limitaciones.
  - Separa la primera detección de la primera publicación según la fuente.
- **Reducción de ruido:**
  - Índice único por grupo y huella de evidencia: los cambios irrelevantes no generan eventos.
  - Versiones con detalle de cada cambio (medios agregados, prioridad, frases nuevas).
  - Cooldown; una alerta revisada se reabre ante evidencia nueva.
  - Temas silenciados, términos y secciones excluidos.
  - Detección de posible fuente común (informe, conferencia, agencia, entrevista).
- **Estados:** pendiente, revisada, descartada y silenciada, con nota, usuario e historial (`alert_events`).
- **Telegram opcional, desactivado por defecto:**
  - Cola en SQLite (`notifications`) con deduplicación por versión de alerta.
  - Hasta 5 intentos con espera creciente, y registro de cada envío.
  - El token se oculta en los errores, y el envío real queda bloqueado durante las pruebas.
- **Panel:**
  - Resumen con alertas y salud de fuentes.
  - Alertas, noticias con más filtros (sección, período, solo con coincidencias), grupos y comparación.
  - Cronología, revisión manual, historial (titulares, alertas, correcciones), métricas de secuencias, ejecuciones y errores, y configuración de alertas.
- **Integración:** `collect` encadena recolección, análisis, alertas y cola de Telegram. Hay un comando `notify` y la migración `0004`.
- **154 pruebas en verde.** El flujo completo ingesta → coincidencia → grupo → alerta → revisión está probado con el recolector simulado y el panel.

### Verificado con datos reales (base local)
- 10 alertas sobre 10 grupos: 8 de prioridad alta (titular o expresión compartida) y 2 de prioridad media (mismo hecho a 1 h y a 7 h). Una segunda corrida no creó ni modificó nada.
- **Falso positivo corregido:** "le robaron el celular mientras daba una entrevista" se marcaba como fuente común "entrevista". Ahora se exigen fórmulas de atribución ("en una entrevista con", "en diálogo con").
- **Métricas:** Perfil/Infobae coinciden en 6 grupos (Perfil antes en 1, Infobae antes en 4, simultáneas 1). Son pocos casos y se muestran como descriptivos.

### Decisiones
- **La huella de una alerta no incluye los ids de artículos:** sumar notas del mismo medio no es un cambio relevante; sumar un medio independiente, sí.
- **Las alertas silenciadas se crean igual (con estado "silenciada")** para conservar el registro, pero no se avisan.
- **Telegram solo encola si está activado:** así no se acumula una cola que, al activarlo, enviaría avisos viejos.
- **Las métricas toman la primera nota de cada medio por grupo y excluyen republicaciones.** El orden por publicación se cuenta aparte del orden por detección.

## Etapa 5: análisis semántico con IA externa (2026-10-04) ✅

### Terminado
- **Paquete `radar/ai/`, opcional y desactivado por defecto.** Sin configuración, la aplicación funciona igual.
- **Interfaz de proveedores y proveedor real** con la API Messages de Anthropic, mediante el SDK oficial `anthropic==1.11.0`.
  - Modelo configurable (`RADAR_AI_MODEL`, por defecto `claude-opus-5-5`) y esfuerzo configurable.
  - Salida JSON con esquema estricto (`output_config.format`), sin herramientas.
  - Respaldo automático del lado del servidor ante rechazos (`fallbacks: "default"`), desactivable.
- **Salida validada** con todos los campos pedidos. Las afirmaciones se clasifican como hecho, opinión, atribución causal, generalización o cita de un tercero.
  - Se rechaza el JSON inválido, lo que no cumple el esquema y las citas inexistentes (o atribuidas a la nota equivocada).
  - Se guardan el proveedor, el modelo, la versión del prompt y la fecha.
- **Discrepancias derivadas para revisión humana:** enfoques opuestos, alcance distinto, períodos distintos, confusión entre fecha de publicación y período estadístico, y cifras distintas. No se juzga la verdad.
- **Protección contra instrucciones inyectadas:** las notas van delimitadas como dato no confiable, con las etiquetas neutralizadas e instrucciones explícitas de no seguirlas.
- **Costos:**
  - Caché por contenido, versión y modelo.
  - Límites diarios de solicitudes y tokens, entrada máxima y máximo de pares por ejecución.
  - Presupuesto en dólares solo con tarifas configuradas; el costo se rotula como estimación.
  - Uso registrado según lo informa el proveedor; cuota agotada desactiva las llamadas hasta el día siguiente.
  - Reintentos acotados y circuit breaker. Los secretos quedan fuera de los registros y del panel.
- **Solo se envían pares preseleccionados** por el motor léxico (72 h, tipos relevantes, primero los de alertas pendientes). Nunca el histórico.
- **Interfaz:**
  - La comparación distingue *detectada por reglas*, *analizada por IA* y *confirmada o rechazada por el usuario*.
  - Hay una sección de IA con campos, fragmentos y discrepancias, y un botón para analizar el par (solo administradores, respeta los límites).
  - Configuración muestra el estado y el uso.
- **Integración:**
  - En la cadena de procesamiento, la IA corre entre el análisis y las alertas.
  - Las alertas usan el análisis para "enfoque similar respaldado por análisis", siempre pendiente de confirmación humana.
  - Comandos `ai-status` y `ai-analyze`; migración `0005`.
- **174 pruebas en verde**, con proveedores simulados para JSON inválido, timeout, cuota agotada, cita inexistente, instrucciones maliciosas, afirmaciones opuestas, cambio de alcance local a nacional y confusión entre fecha de publicación y período estadístico.
  - Se probó la forma de la solicitud y el mapeo de errores del proveedor real, con el cliente del SDK reemplazado.
  - Reprocesar preserva las revisiones humanas.

### No verificado
- **No se hizo ninguna llamada real** a la API: no hubo configuración ni autorización para llamadas pagas. Quedan sin probar contra el servicio real la forma exacta de la solicitud (`output_config` con esquema y `fallbacks`) y la calidad de las respuestas. Antes de usarlo conviene una prueba controlada con `RADAR_AI_MAX_PER_RUN=1`.

### Decisiones
- **SDK oficial en lugar de HTTP manual:** maneja autenticación, tipos y errores tipados. Los reintentos del SDK se desactivan para contarlos en el circuit breaker propio.
- **Los titulares idénticos no se envían:** la coincidencia ya es evidente y gastaría cuota sin aportar.
- **Las citas se verifican contra el texto exacto enviado** (titular, bajada y, si existe, el cuerpo recortado), tolerando solo espacios, mayúsculas y comillas tipográficas.
- **El análisis no modifica `article_relations`:** se guarda aparte y se consulta por par de notas, así las revisiones y el reprocesamiento quedan independientes.

### Cambio pedido: Groq y Fireworks como proveedores (2026-10-04)
- **Nuevo `radar/ai/openai_compat.py`:** proveedores Groq y Fireworks por HTTP (httpx), con endpoints, autenticación y `response_format` de tipo `json_schema` tomados de la documentación oficial de cada uno.
  - Esquema estricto opcional en Groq.
  - Mapeo de errores: 429 es límite de solicitudes, y si se refiere a un límite diario cuenta como cuota agotada; 402 es saldo agotado; 401/403 son credenciales; 5xx es caída. También se detectan rechazos y timeouts.
  - Las claves se ocultan en los errores.
- **Cadena con conmutación** (`RADAR_AI_PROVIDER=groq,fireworks`, nuevo valor por defecto). El uso y los fallos se registran por proveedor, y el análisis guarda qué proveedor respondió. Anthropic queda como opción.
- **Sin modelos por defecto para Groq y Fireworks** (`RADAR_GROQ_MODEL`, `RADAR_FIREWORKS_MODEL`): no se presupone ningún nombre. Si falta la clave o el modelo, no se llama y se informa el motivo.
- **`RADAR_AI_MAX_OUTPUT_TOKENS` baja a 8192 por defecto:** alcanza para el JSON y es compatible con más modelos.
- **191 pruebas en verde.** Las 17 nuevas usan transporte simulado: forma de la solicitud, uso, errores, ocultamiento de la clave, configuración faltante, conmutación y flujo completo con uso por proveedor.
- **No verificado contra los servicios reales:** no hubo claves ni autorización. Queda pendiente confirmar con una llamada controlada que el modelo elegido de cada proveedor acepte `json_schema` (y `strict` en Groq).

## Etapa 6: preparación del despliegue en el VPS (2026-10-04) ✅ preparado, no desplegado

### Terminado
- **Sin acceso al VPS:** el usuario pidió seguir en local. Todo queda listo y documentado en [DEPLOYMENT.md](DEPLOYMENT.md); no se afirma ningún despliegue.
- **Corrección para producción:** el archivo de bloqueo del recolector estaba fijo en el directorio del código, que es de solo lectura con el hardening de systemd. Ahora `RADAR_DATA_DIR` define la base, el bloqueo y los backups.
- **Directorios y puerto configurables:** `RADAR_DATA_DIR`, `RADAR_BACKUP_DIR`, `RADAR_HOST` y `RADAR_PORT`.
- **Unidades systemd:**
  - Panel con un worker en localhost, reinicio ante fallos y límites de memoria y CPU.
  - Recolección como oneshot, con un timer cada 10 minutos.
  - Mantenimiento diario: backup y retención.
  - Logs en un espacio propio de journald (200 MB, 30 días).
  - Hardening con escritura solo en `/var/lib/radar`.
- **Nginx:** sitio para un subdominio (bootstrap HTTP y HTTPS con proxy a localhost), más un script que valida con `nginx -t` antes de recargar y no toca la configuración global.
- **Scripts** (`deploy/scripts/`): `install.sh`, `update.sh` (backup previo y vuelta atrás automática), `rollback.sh`, `backup.sh`, `restore.sh` y `nginx-site.sh`. Son idempotentes y no destructivos: no pisan el entorno ni borran la base.
- **CLI nueva:** `backup` (API de SQLite, `integrity_check`, rotación), `restore-check` (sobre una copia), `restore --yes` (conserva la base anterior) y `purge` (retención configurable, protege los grupos con decisiones humanas).
- **Healthcheck:** `/salud` informa estado, base y minutos desde la última recolección exitosa, sin datos ni secretos.
- **200 pruebas en verde**, 9 de ellas nuevas: backup con WAL, rotación, verificación, restauración, retención, healthcheck y directorio de datos.

### Mediciones (WSL2 con Ubuntu 24.04, no es el VPS)
- **Memoria:** panel 77 MB en reposo y 85 MB con uso; pico del ciclo completo 117 MB; pico del análisis 100 MB (2,1 s).
- **Tiempos:** ciclo de unos 150 s, dominado por las pausas de cortesía entre solicitudes.
- **Base:** 3,8 MB con 676 notas.

## Etapa 7: auditoría final (2026-10-04)

Se inspeccionó el código y se comprobó el comportamiento con pruebas y con datos reales, sin tomar como fuente el README ni este archivo.

### Problemas encontrados y corregidos (cada uno con su prueba en `tests/test_audit.py`)
1. **Alertas duplicadas tras unir grupos.** Al unir dos grupos, la alerta del grupo absorbido quedaba pendiente junto a la del grupo resultante. Ahora se cierra sola, con una nota y un evento.
2. **Afirmaciones opuestas sin aviso en la alerta.** Si dos titulares compartían una expresión pero uno la negaba, la alerta de prioridad alta no lo advertía. Ahora el motivo y la evidencia lo muestran ("con afirmaciones distintas: revisar").
3. **Ruido en alertas al actualizar el motor.** Un cambio de formato en la huella marcaba 9 alertas reales como "actualizadas", y habría reabierto las revisadas. Ahora solo cuentan las diferencias concretas: medio nuevo, prioridad, frases, tipos, fuente común, advertencias o análisis de IA.
4. **La retención podía borrar decisiones humanas.** `purge` eliminaba en cascada las relaciones confirmadas o rechazadas a mano. Ahora conserva esos artículos.
5. **Falso positivo con datos reales.** "estado de São Paulo" se tomaba como expresión distintiva, porque el detector de nombres no reconocía "ã". Ahora se reconocen las letras del portugués y el francés, y palabras como "Le" o "Estoy" al inicio de una cita no cuentan como nombres. La versión del motor pasa a `lexico-1.1`.
6. **`.gitignore`** no cubría `.venv-win` ni `.venv-wsl`.

### Verificado
- **Fuentes:** los 17 endpoints de los tres medios respondieron OK en vivo (`radar probe`).
- **Ingesta real limitada:** 11 subfuentes sin problemas, 110 notas nuevas y 6 páginas leídas.
- **Relaciones reales:** las 22 se revisaron a mano. Ninguna se basa solo en un nombre frecuente.
- **Seguridad y documentación:**
  - todas las rutas POST exigen CSRF;
  - no hay secretos en el código; `.env` es local, de desarrollo y está ignorado;
  - todos los comandos documentados existen en la CLI.
- **Pruebas:** 205 en verde.

### Agregado: vista pública para visitantes (2026-10-04)
- **Activación:** `RADAR_PUBLIC_MODE` (desactivada por defecto). Agrega un visitante anónimo de solo lectura: la dependencia `viewer` reemplaza al login en Resumen, Noticias, Grupos y comparación, Cronología, Métricas, Fuentes y Alertas.
- **Siguen pidiendo login:** Revisión, Historial, Ejecuciones, Configuración y todas las acciones (POST).
- **Qué se oculta al visitante:** datos DEMO, alertas no publicables (`RADAR_PUBLIC_ALERTS`), notas e historiales de revisión, errores de fuentes y análisis de IA de relaciones no confirmadas.
- **Además:** aviso fijo sobre el carácter léxico de las coincidencias y límite de solicitudes por IP para visitantes (`RADAR_PUBLIC_RATE_LIMIT`).
- **Pruebas:** 12 nuevas (`tests/test_public.py`), 218 en total en verde. Se verificó con los datos reales locales como visitante.

### Agregado: AGENTS.md, skills.md, módulo de Guardrails y Autor de la nota (2026-10-04)
- **Documentación de Agentes y Habilidades:**
  - Creados [AGENTS.md](file:///f:/sistemas/political/AGENTS.md) y [agents.md](file:///f:/sistemas/political/agents.md) con la guía integral de arquitectura, restricciones operativas (2 GB RAM, SQLite WAL, Python/FastAPI/HTMX), principios epistemológicos, estructura de directorios y metodología de desarrollo.
  - Creados [skills.md](file:///f:/sistemas/political/skills.md) y [SKILLS.md](file:///f:/sistemas/political/SKILLS.md) con el catálogo exhaustivo de comandos CLI y capacidades (recolección, análisis léxico, evaluación semántica IA, alertas, administración, mantenimiento y operaciones web).
- **Módulo de Guardrails:**
  - Implementado [radar/guardrails.py](file:///f:/sistemas/political/radar/guardrails.py) con validaciones centralizadas: `NetworkGuardrail` (defensa SSRF), `ContentGuardrail` (sanitización de inyección de prompts y delimitadores), `EvidenceGuardrail` (anti-alucinación estricta de citas), `EpistemicGuardrail` (separación de orden temporal vs causalidad y detección de polaridad opuesta).
  - Creadas pruebas unitarias en [tests/test_guardrails.py](file:///f:/sistemas/political/tests/test_guardrails.py).
- **Integración y visualización del Autor de la nota:**
  - Propagado a las vistas de listado de noticias ([article_row.html](file:///f:/sistemas/political/radar/templates/partials/article_row.html)), comparación lado a lado ([relation_compare.html](file:///f:/sistemas/political/radar/templates/relation_compare.html)), cronología y listas de grupos ([group_detail.html](file:///f:/sistemas/political/radar/templates/group_detail.html)), detalle de alertas ([alert_detail.html](file:///f:/sistemas/political/radar/templates/alert_detail.html)) e historial de cambios ([history.html](file:///f:/sistemas/political/radar/templates/history.html)).
  - Indexado en `search_text` durante la ingesta y enriquecimiento ([ingest.py](file:///f:/sistemas/political/radar/collector/ingest.py), [enrich.py](file:///f:/sistemas/political/radar/collector/enrich.py)), permitiendo buscar por autor.
  - Serializado en las evidencias de alertas ([alerts/engine.py](file:///f:/sistemas/political/radar/alerts/engine.py)) y en los payloads y prompts de análisis con IA ([ai/service.py](file:///f:/sistemas/political/radar/ai/service.py), [ai/prompt.py](file:///f:/sistemas/political/radar/ai/prompt.py)).
- **Pruebas:** 224 pruebas en verde (0 fallos).

### Agregado: Adaptación para Vercel Serverless (2026-10-05)
- Creado [vercel.json](file:///f:/sistemas/political/vercel.json) con runtime `@vercel/python`, enrutamiento estático hacia `/radar/static` y cron job `/api/cron` cada 10 min.
- Creado punto de entrada [api/index.py](file:///f:/sistemas/political/api/index.py) con inicialización automática de tablas y semillas en entornos serverless.
- Adaptado [radar/config.py](file:///f:/sistemas/political/radar/config.py) para redirigir la base a `/tmp` en Vercel y admitir variables `POSTGRES_URL` / `DATABASE_URL` para PostgreSQL externo.
- Documentación completa en [VERCEL.md](file:///f:/sistemas/political/VERCEL.md).

### Agregado: Página/12 como cuarto medio (2026-10-05)
- **Adaptador `pagina12`** con 8 endpoints verificados en vivo: 5 RSS (portada, El País, Economía, Sociedad, El Mundo), el sitemap de noticias reciente y 2 portadas HTML de respaldo (pausadas). Responden todos; robots.txt permite todo, sin `Crawl-delay`.
- **Decisiones por lo que se observó:** el feed general sin sufijo es la edición regional Salta|12, y el sitemap largo es antiguo: ninguno se usa. Las URLs de feeds llevan barra final, porque sin ella hay una redirección extra por cada consulta.
- **Cambios comunes a todos los medios:** la sección puede venir del endpoint (la URL de Página/12 no la trae), y la firma se limpia (HTML y prefijo "Por "). Las opciones `--media` de la CLI salen ahora de los adaptadores registrados. `el-pais` se suma a las secciones prioritarias, sin pisar valores personalizados.
- **Ingesta real limitada (solo Página/12):** 6 fuentes sin problemas, 170 notas nuevas, 5 páginas leídas. 0 URLs duplicadas, 0 firmas con HTML, fechas con hora en las 170 notas, 53 con firma, 100 con identificador de origen.
- **Integración con el análisis (datos reales):** 17 relaciones con Página/12 y 7 grupos. Casi todas sólidas (la cita de García Cuerva en tres medios, Pandanés, Colapinto, Pampa Energía, la inflación de septiembre, las elecciones en Brasil). Tres débiles que quedaron pendientes, sin grupo ni alerta: "violencia de género", "discursos de odio" y un descriptor de Bolsonaro.
- **Pruebas:** 239 en verde (17 nuevas en `tests/test_pagina12.py`, con fixtures sintéticos).
- **Limitaciones:** 71 de las 170 notas quedaron sin sección porque se vieron primero en el sitemap o la portada, que no la informan; se completa si luego aparecen en un feed de sección. Las ediciones regionales (Salta/12, Rosario/12…) no se monitorean.

### Agregado: PostgreSQL (Neon) y Vercel (2026-10-05)
- **Punto de partida:** se había adaptado el proyecto a Vercel (`api/index.py`, `vercel.json`, `VERCEL.md`) sin que yo lo escribiera. El análisis encontró que **no arrancaba** (importaba `seed_all`, `collect_all` y `run_alerts`, que no existen), que traía una **clave secreta fija** (se probó que permitía falsificar una sesión de administrador), un `/api/cron` anónimo, y una base SQLite en `/tmp`. El usuario pidió usar PostgreSQL externo con Neon.
- **Soporte de PostgreSQL:**
  - Driver `psycopg` 3 (`psycopg==3.3.6`, con ruedas para Linux x86_64 con Python 3.12 y 3.13).
  - Normalización de URL: `postgres://` y `postgresql://` pasan a `postgresql+psycopg://`, conservando `sslmode` y `channel_binding`.
  - URL con pooler para la app y URL directa para migraciones y backups (`RADAR_DATABASE_URL_DIRECT`, o derivada quitando `-pooler`).
  - Motor: `pool_pre_ping`, `connect_timeout` de 15 s, pool chico, sentencias preparadas desactivadas con pooler y sin pool en serverless.
  - Migraciones sin cambios: las 5 existentes corrieron tal cual sobre PostgreSQL.
- **Portabilidad (hallazgos reales al correr la suite sobre PostgreSQL):** `func.min(a, b)` y `func.max(a, b)` eran de SQLite y rompían el motor de alertas en cada relación; se reemplazaron por `pair_filter`. Los índices parciales ahora llevan también `postgresql_where`.
- **Bloqueo entre instancias:** el bloqueo por archivo no sirve con PostgreSQL ni en serverless. Se agregó la tabla `job_locks` (migración `0006`), con bloqueo con vencimiento: lo toma una sola instancia (probado con 8 intentos simultáneos) y se libera solo si el proceso muere. No usa advisory locks de sesión, que el pooler de Neon no admite.
- **Backups con PostgreSQL:** `pg_dump` en formato custom, verificado con `pg_restore --list`, con rotación. La restauración no se automatiza y la CLI informa el comando y la opción de Neon.
- **Vercel:**
  - `app.py` en la raíz (Vercel detecta la variable `app`) y `vercel.json` con `functions`.
  - Se eliminó `api/`.
  - Sin clave por defecto: si falta `RADAR_SECRET_KEY` no arranca.
  - Se niega a usar SQLite en Vercel (salvo `RADAR_ALLOW_EPHEMERAL_DB`).
  - `/api/cron` exige `CRON_SECRET` (503 sin la variable, 401 con un Bearer incorrecto) y valida `tarea` y `media`.
  - Tareas: `recolectar`, `analizar`, `mantenimiento` (retención) y `todo`.
  - Una invocación duplicada responde "omitido".
  - Los errores no filtran datos internos.
  - La IP real del visitante se toma de `x-forwarded-for` solo dentro de Vercel.
- **Scripts de despliegue:** `schema_version` y el rollback de `update.sh` ya no dependen de `sqlite3`. Con PostgreSQL, `update.sh` no restaura la base solo.
- **Finales de línea:** mis scripts de edición en Python escribían CRLF en Windows, lo que rompe bash. Se normalizó `deploy/` a LF, se agregó `.gitattributes` y quedó anotado en `AGENTS.md`.
- **Pruebas:** la suite ahora puede correr sobre PostgreSQL real con `RADAR_TEST_PG_URL` (un esquema descartable por prueba). Resultados finales: **SQLite 261 pasan y 4 omitidas; PostgreSQL 18.6 258 pasan, 7 omitidas y 0 fallas.** Se agregaron 28 pruebas.
- **Ingesta real contra PostgreSQL** (clúster temporal en otro puerto, sin tocar el servicio del usuario): 17 fuentes sin problemas, 856 notas, 36 relaciones, 12 grupos y 12 alertas. El segundo análisis no creó nada y el bloqueo quedó liberado. 45 pantallas (visitante y administrador) sin errores 5xx. La base ocupó 14,2 MB: 7,9 MB de una base vacía más unos 7,4 KB por nota.
- **No verificado:**
  - Un despliegue real en Vercel y una base real en Neon (no hubo cuentas ni credenciales): el aplicado de `excludeFiles` y `maxDuration`, que el paquete incluya plantillas y estáticos, los tiempos dentro de Vercel y el arranque en frío.
  - Que `psycopg` funcione con el pooler real de Neon (se probó con PostgreSQL local, sin PgBouncer).
  - El consumo de CU-horas del plan gratuito (es una estimación: un ciclo cada 10 minutos probablemente lo supere).
  - Los backups con `pg_dump` contra un servidor más nuevo o más viejo que la versión 18 de las herramientas.

### Pendiente del despliegue
- Datos del usuario: acceso SSH, distribución, subdominio, email para el certificado y si ya hay un Nginx.
- Ejecutar en el VPS, copia de backups fuera del servidor, monitoreo externo de `/salud`, y volver a medir en el VPS.

### Pendiente
- [ ] Probar Groq y Fireworks con una llamada real controlada (`ai-analyze --limit 1`) cuando el usuario elija modelos y lo autorice.
- [ ] Coincidencia de temas por palabra completa (hoy se busca por subcadena).
- [x] Alta de medios y subfuentes desde el panel (adaptador genérico + descubrimiento de feeds).
- [ ] Edición y baja de medios agregados desde el panel; `probe` para medios genéricos.
- [ ] Retención de `collection_runs`, `login_attempts` y relaciones antiguas.
- [ ] Mitigar DNS rebinding en el cliente HTTP.
- [ ] Alertar cuando una fuente pase a "Caída" o "Bloqueada".

### Limitaciones conocidas
- La detección de entidades es heurística (mayúsculas): puede perder nombres en minúscula o confundir inicios de oración. Las siglas sí se detectan.
- Las reglas son léxicas: notas sobre un mismo hecho con vocabulario muy distinto pueden no relacionarse, y notas con vocabulario común pero hechos distintos (dos elecciones distintas) pueden relacionarse como "mismo tema". El estado "pendiente" y la revisión manual existen por eso.
- La polaridad se evalúa sobre el titular completo, no por oración ni por predicado.
- El Destape no publica bajada en el sitemap; su cobertura de bajada depende de la lectura de páginas (10 por ejecución).
- La búsqueda con `LIKE` recorre toda la tabla; con mucho volumen conviene FTS5.
- Las alertas heredan los límites del motor léxico: la prioridad alta por "expresión compartida" puede deberse a una fuente común no detectada.
- Telegram se probó solo con un transporte simulado; no se enviaron mensajes reales.
- Por ahora todo se prueba en local (pedido del usuario). No se probó detrás de un proxy HTTPS real, ni en el VPS.
