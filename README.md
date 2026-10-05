# Radar de Titulares

Monitorea los titulares de diversos medios de comunicación, guarda su historial cronológico y detecta coincidencias temáticas y léxicas a través de un panel web en español, con temas de seguimiento y filtros configurables.

Estado actual: sistema completo para uso local y con soporte para VPS y Vercel + Neon. Recolecta Perfil, El Destape Web, Infobae y Página/12 (más los medios que se agreguen desde el panel), un motor léxico sin IA detecta coincidencias, arma grupos y genera alertas con evidencia, y opcionalmente un modelo externo analiza los pares preseleccionados. Telegram y la IA vienen desactivados (ver [PROGRESS.md](PROGRESS.md)).

## Cómo funciona

```
Medios (RSS, sitemaps, portadas)
   │  1. recolectar   python -m radar collect
   ▼
Notas + versiones de titular + procedencia
   │  2. enriquecer   lee la página de las notas pendientes: bajada, autor, enlaces
   ▼
Motor léxico (sin IA)
   │  3. comparar     notas de los últimos 3 días entre medios distintos
   ▼
Relaciones → Grupos → Alertas con evidencia
   │  4. (opcional)   IA sobre los pares preseleccionados
   ▼
Panel  ← 5. revisión humana: confirmar, rechazar, separar, unir, silenciar
```

1. **Recolectar.** Cada subfuente (un feed RSS, un sitemap de noticias o una portada) se consulta como mínimo cada ~10 minutos. Las notas se guardan una sola vez aunque aparezcan en varias fuentes; si cambia un titular, se guarda una versión nueva. Las fechas se guardan en UTC y nunca se inventa una hora que la fuente no dio. Una fuente que falla o que cambió de formato queda marcada, no se confunde con "sin noticias".
2. **Enriquecer.** Para unas pocas notas por medio y ciclo se lee la página y se completan la bajada, la firma y los enlaces a otros medios.
3. **Comparar.** El motor léxico compara notas de medios distintos y explica cada coincidencia: qué términos, frases o entidades comparten, qué regla se activó y los puntajes parciales. Excluye nombres y términos demasiado frecuentes ("Milei" no alcanza), conserva negaciones y cifras, y no cuenta las republicaciones de agencias como confirmación independiente.
4. **Agrupar y alertar.** Las relaciones se agrupan evitando encadenar notas que no se parecen entre sí, y cada grupo genera **una** alerta con prioridad baja, media o alta. La prioridad indica qué revisar primero: no significa que algo sea falso ni que haya coordinación.
5. **IA (opcional).** Si está activada, un modelo externo analiza solo los pares que el motor ya detectó, con límites diarios y salida validada. También se analiza cuando un administrador confirma una relación.
6. **Revisar.** Un administrador confirma, rechaza, corrige grupos o silencia. **La revisión humana manda**: reprocesar nunca la borra.

**Qué no hace el radar**
- No determina qué afirmación es verdadera: marca discrepancias para que las revise una persona.
- No dice que un medio "originó" o "copió" a otro: el orden de aparición no es causalidad. Se muestran por separado la *primera detección* del radar y la *primera publicación según la fuente*.
- Una coincidencia léxica indica palabras compartidas, no que el mensaje sea el mismo.

**Uso diario**
```powershell
.\.venv-win\Scripts\python -m radar collect          # recolecta, analiza y genera alertas (repetir cada ~10 min)
.\.venv-win\Scripts\uvicorn radar.web.app:create_app --factory --port 8000
```
En el panel: **Resumen** (qué revisar primero), **Alertas** y **Revisión** (la cola de trabajo), **Grupos** y **Noticias** (explorar), **Fuentes** (estado de cada medio y alta de medios nuevos) y **Configuración** (temas, secciones prioritarias, límites de alertas e IA).

## Stack

- Python 3.11+, FastAPI, Jinja2 y HTMX (servido localmente, sin compilar el frontend)
- SQLAlchemy 2 + Alembic + SQLite (WAL, `busy_timeout`, transacciones breves) o PostgreSQL (Neon)
- Recolección en un comando separado (`python -m radar collect`) que dispara un timer de systemd
- No usa Docker, Redis, Celery, Elasticsearch ni modelos de IA locales (la IA externa es opcional)

## Instalación local

```bash
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows (PowerShell)
.venv\Scripts\Activate.ps1

pip install -r requirements-dev.txt      # en producción: requirements.txt
cp .env.example .env                     # y completar RADAR_SECRET_KEY
python -c "import secrets; print(secrets.token_urlsafe(48))"   # para generar la clave
```

Crear la base, cargar las fuentes y los temas iniciales y crear el administrador:

```bash
python -m radar init-db
python -m radar create-admin admin       # pide la contraseña (mínimo 12 caracteres)
```

No hay contraseña predeterminada. Para automatizar la creación del administrador: `echo "$CLAVE" | python -m radar create-admin admin --password-stdin`.

Primera recolección y arranque del panel:

```bash
python -m radar collect --force
uvicorn radar.web.app:create_app --factory --reload --port 8000
```

Después abrí <http://127.0.0.1:8000>.

### Modo demo (datos ficticios)

```bash
# en .env: RADAR_DEMO_MODE=true
python -m radar demo-load     # medios inventados ("Diario Demo Norte"...), titulares "[DEMO] Ejemplo: ..."
python -m radar demo-clear    # los elimina
```

Los datos demo usan dominios `.invalid`, se marcan con la insignia DEMO y un aviso fijo en el panel, y se ocultan cuando `RADAR_DEMO_MODE=false`.

## Comandos

| Comando | Qué hace |
|---|---|
| `python -m radar init-db` | Aplica las migraciones (`alembic upgrade head`) y carga fuentes y temas. Se puede repetir sin problema. |
| `python -m radar create-admin USUARIO [--password-stdin]` | Crea un administrador. |
| `python -m radar set-password USUARIO [--password-stdin]` | Cambia la contraseña y cierra las sesiones abiertas. |
| `python -m radar collect` | Recolecta una vez todas las subfuentes cuyo intervalo mínimo ya venció. |
| `python -m radar collect --media perfil\|eldestape\|infobae\|pagina12` | Recolecta un solo medio. |
| `python -m radar collect --force` | Ignora el intervalo mínimo. |
| `python -m radar collect --no-enrich` | Solo feeds, sitemaps y secciones, sin leer páginas de notas. |
| `python -m radar collect --no-analyze` | Recolecta sin correr el análisis de relaciones al final. |
| `python -m radar analyze` | Detecta relaciones, grupos y alertas en las últimas 72 h (también corre al final de cada `collect`). |
| `python -m radar notify` | Envía los avisos pendientes por Telegram (solo si está activado). |
| `python -m radar ai-status` | Estado de la IA externa: configuración, uso de hoy y límites (no muestra secretos). |
| `python -m radar ai-analyze [--limit N]` | Analiza con IA hasta N relaciones preseleccionadas. También corre dentro de `collect` y `analyze` si está activada. |
| `python -m radar probe [--media ...]` | Verifica en vivo todas las fuentes declaradas, sin guardar nada. |
| `python -m radar demo-load` / `demo-clear` | Carga o elimina los datos ficticios. |
| `alembic revision --autogenerate -m "..."` | Crea una migración nueva después de cambiar `radar/models.py`. |
| `python -m radar backup [--dest DIR] [--keep N]` | Backup verificado y con rotación: API de SQLite o `pg_dump` si la base es PostgreSQL. |
| `python -m radar restore-check ARCHIVO` | Verifica un backup restaurándolo sobre una copia temporal. |
| `python -m radar restore ARCHIVO --yes` | Restaura (con los servicios detenidos); la base actual se conserva renombrada. |
| `python -m radar purge [--dry-run]` | Aplica la retención configurada del histórico. |
| `pytest` | Corre las pruebas. |

## Fuentes y descubrimiento

Cada medio tiene su adaptador en `radar/collector/adapters/`, con las fuentes declaradas, las reglas para reconocer una URL de nota, el identificador de origen y las subfuentes editoriales. `init-db` sincroniza esas fuentes con la base y respeta lo que se haya pausado desde el panel.

### Descubrimiento de fuentes

Orden de prioridad: RSS/Atom, después sitemaps de noticias y por último secciones HTML públicas. Ningún endpoint pide login, CAPTCHA ni muro de pago, y no se intenta evadir ninguno. Se respetan las directivas de `robots.txt` y las pausas entre peticiones (`Crawl-delay`). Para verificar las fuentes en vivo: `python -m radar probe`.

### Identificadores y subfuentes

- **Página/12:** la URL es `/AAAA/MM/DD/slug/` y **no incluye la sección**, así que se toma del feed de origen (`el-pais`, `economia`, `sociedad`, `el-mundo`). Una nota vista primero por el sitemap o la portada recibe la sección cuando aparece en el feed de su sección. El `guid` es un identificador alfanumérico opaco y se usa como identificador de origen. La firma llega a veces con HTML y prefijo (`Por <b>Ana Pérez</b>`) y se guarda como `Ana Pérez`. Se descartan los "clones" de prueba que el medio deja publicados (slug terminado en `-clone`). `el-pais` es su sección de política nacional y está entre las secciones prioritarias.
- **Perfil:** el `guid` del RSS es numérico y estable, y se usa como identificador de origen. Las subfuentes editoriales se detectan solo por evidencia en la URL: `/noticias/canal-e/` corresponde a **Canal E**, `noticias.perfil.com` a **Revista Noticias** y `/noticias/cordoba/` a **Perfil Córdoba**. `/noticias/bloomberg/` es **Bloomberg** y se marca como *republicada*. Todas cuentan como Perfil.
- **El Destape:** el número final de la URL **no** es un identificador; es la fecha y hora de publicación. En la prueba real dos notas distintas lo compartían, así que se deduplica por URL.
- **Infobae:** el `guid` es la URL. Las rutas con `/agencias/` y las notas firmadas por agencias (EFE, AFP, Europa Press…) se marcan como republicadas.

### Cómo se recolecta

- Un timer de systemd corre cada 10 minutos. Cada subfuente tiene un intervalo mínimo propio (unos 10 minutos, o 20 en los feeds pesados).
- Se usa httpx con timeouts y hasta 2 reintentos con backoff exponencial (respeta `Retry-After`) ante errores de red, 429 o 5xx. Los 401, 403 y 451 se registran como **bloqueo** y no se reintentan.
- Hay como máximo 2 descargas simultáneas (`RADAR_MAX_CONCURRENCY`), con separación mínima por dominio (2 s, o 10 s en El Destape) y caché de robots.txt.
- Se envían ETag/If-None-Match y Last-Modified/If-Modified-Since. Cada respuesta se limita a 10 MB, y se truncan los textos: titular 1000 caracteres, bajada 3000, cuerpo 20 000.
- Hay un bloqueo entre procesos (archivo `data/collect.lock`) además del `Type=oneshot`. Una fuente que falla no detiene a las demás, y todo es idempotente: repetir una ejecución no duplica notas, versiones ni referencias.
- Cada ejecución queda registrada en `collection_runs`, con estado, HTTP, intentos, duración, ítems, nuevos, cambios y errores.
- **Lectura de páginas:** como máximo 10 notas por medio y por ejecución (`RADAR_ENRICH_PER_MEDIA`). Se leen notas recientes de secciones prioritarias, de temas activos o sin bajada. De la página se toman solo metadatos: bajada (`og:description`), autor, fechas, sección, palabras clave, canónica y enlaces a otros medios. No se descargan imágenes ni videos, y no se usa navegador headless.

### Normalización

- **Deduplicación:** por medio, primero por URL canónica y después por identificador de origen. Un identificador repetido con un titular muy distinto no fusiona notas. Dos medios nunca comparten un artículo, aunque tengan el mismo titular.
- **URL canónica:** https, host en minúsculas, sin fragmento y sin parámetros de seguimiento conocidos (`utm_*`, `fbclid`, `gclid`…). Los demás parámetros se conservan. La canónica que declara la página solo se acepta si es una nota del mismo medio, y la URL final tras redirecciones también se valida.
- **Versiones:** cada cambio de titular o de bajada guarda una versión nueva. Si una fuente no trae bajada, no borra la que aportó otra. Completar la bajada desde la página se marca como `origin=pagina`, no como un cambio del medio.
- **Procedencia** (`article_sightings`): qué subfuentes vieron cada nota, con qué URL e identificador, y cuándo.
- **Referencias a otros medios** (`media_references`): menciones explícitas en titular o bajada (por ejemplo, "según Clarín") y enlaces a medios conocidos dentro del cuerpo, con la evidencia. Las menciones del propio medio no cuentan.

### Agregar un diario desde el panel (sin escribir código)
Como administrador: **Fuentes → Agregar medio**. Se ingresa el nombre y la dirección del sitio; el sistema abre la portada, busca los feeds que declara (`<link rel="alternate">`) y las rutas habituales (`/feed/`, `/rss.xml`, `/news-sitemap.xml`…), prueba cada uno y muestra cuántas notas del medio trae. Se tildan los que sirven (o se carga una URL a mano) y al guardar se vuelven a verificar. Un medio agregado así puede ampliarse con **Agregar fuente**.

Reglas y límites:
- Todo pasa por el descargador seguro: solo el dominio del propio sitio, sin IP privadas, respetando `robots.txt`. No se evade ningún bloqueo.
- El medio usa un adaptador **genérico**: la sección sale del primer tramo de la URL, no hay identificador de origen propio y solo se reconocen como republicación las notas firmadas por agencias (EFE, AFP, Reuters…). Si un medio necesita reglas propias (como Página/12), se escribe un adaptador (skill `radar-agregar-medio`).
- Las notas de portada (`html_section`) aceptan cualquier ruta del dominio que no sea etiqueta, autor, búsqueda o feed: conviene preferir RSS o sitemap.
- Todavía no se pueden editar ni borrar medios desde el panel; sí pausar cada fuente.
- `python -m radar collect --media SLUG` funciona con estos medios; `probe` solo verifica los que tienen adaptador propio.

## Coincidencias y grupos (motor léxico, sin IA)

Código en `radar/analysis/`. Versión del algoritmo: `lexico-1.1`; se guarda en cada relación y en cada grupo, junto con las reglas aplicadas.

**Importante:** las similitudes son **léxicas**. Indican palabras, frases o entidades compartidas; no que dos notas digan lo mismo, tengan el mismo enfoque ni que haya coordinación. El "enfoque" queda siempre **sin verificar**, y el puntaje (0 a 1) no es una probabilidad.

### Procesamiento del texto
- Minúsculas y sin tildes para comparar. **No se eliminan** negaciones (no, ni, nunca, sin, tampoco…), cifras ("2,1%", "US$ 500 millones") ni nombres propios.
- Se detectan la negación y el cese ("dejó de", "ya no", "frenó"), de modo que "La mora aumentó", "La mora no aumentó" y "La mora dejó de aumentar" se distinguen y nunca se marcan como titular idéntico.
- Las entidades (nombres y siglas) salen de una heurística de mayúsculas, sin modelo de NER. Una palabra con mayúscula al inicio del titular solo cuenta como nombre si aparece como tal en otra posición del lote.
- TF-IDF disperso en Python puro (sin numpy), con palabras y bigramas sobre titular y bajada, n-gramas de caracteres (3 a 5) sobre el titular, y similitud coseno. El vocabulario se limita a 20 000 términos.

### Eficiencia
- **Notas recientes:** las de las últimas 72 h (hasta 4000).
- **Antecedentes:** hasta 90 días atrás, con selección acotada. Se buscan solo las entidades de frecuencia intermedia, con un máximo de 15 notas por entidad y 1500 en total.
- **Sin comparar todo contra todo:** solo se evalúan pares que comparten algún término poco frecuente (índice invertido), con un máximo de 40 candidatos por nota. Los pares del mismo medio no se comparan entre sí, salvo para reapariciones.
- **Medición** en la base local (unas 700 notas): entre 2 y 10 s, con unos 42 MB de memoria de pico.

### Reglas

| Regla | Tipo | Condición |
|---|---|---|
| R1 | Titular idéntico | Igual tras normalizar, con las mismas cifras y la misma polaridad |
| R2 | Titular casi idéntico | Similitud de caracteres ≥ 0,85, con las mismas cifras y la misma polaridad |
| R3 | Expresión distintiva compartida | Frase o cita poco frecuente, con 2 o más palabras de contenido que no sean nombres ni fechas, y similitud de palabras ≥ 0,15. Si la frase tiene solo 2 palabras de contenido, debe aparecer en 3 notas o menos. |
| R4 | Posible mismo hecho (candidato) | Similitud de palabras ≥ 0,30, 2 o más coincidencias distintivas, y como máximo 48 h de diferencia |
| R5 | Mismo tema y entidades | Mismo tema de seguimiento, una entidad distintiva y similitud de palabras ≥ 0,18 |
| R6 | Reaparición | Similitud de palabras ≥ 0,35 con una nota de entre 3 y 90 días antes |
| R7 | Referencia explícita | Enlace a la otra nota, o mención del otro medio sobre el mismo tema |
| RP | Afirmaciones distintas | Titulares parecidos con polaridad opuesta: **no es el mismo mensaje** |
| RN | Cifras distintas | Impide marcar los titulares como idénticos |

Compartir un nombre muy frecuente (por ejemplo, "Milei") no alcanza: las coincidencias distintivas excluyen entidades y términos frecuentes en la ventana.

Cada relación guarda los artículos, el tipo, los puntajes parciales, los términos, entidades, frases y cifras coincidentes, las reglas, el método y la versión, y el estado de revisión (pendiente, confirmada o rechazada). También guarda la diferencia temporal, pero **solo** cuando ambas notas tienen hora de publicación; si no, se aclara que no se establece orden.

### Grupos
- Se unen notas por relaciones de tipo R1 a R4, más las confirmadas a mano, de mayor a menor puntaje.
- **Anti-encadenamiento:** dos conjuntos se unen solo si **todos** los pares cruzados tienen una similitud ≥ 0,12 y la media es ≥ 0,22. Que A se parezca a B y B a C no une A con C. Un grupo tiene como máximo 30 notas, y las relaciones rechazadas impiden juntar esas notas.
- Un grupo debe tener al menos 2 medios. Canal E, Revista Noticias y Perfil Córdoba cuentan como Perfil. "Medios independientes" excluye las notas republicadas (Bloomberg en Perfil, agencias en Infobae).
- **Datos de cada grupo:** nombre (el titular de la nota más central, no el de la primera encontrada), términos y expresiones comunes, primera aparición observada, cronología y una explicación con las aristas y la coherencia.
- **Cronología:** solo se ordenan las notas con hora de publicación; las que tienen solo fecha se listan aparte. El orden observado no indica qué medio originó la información ni que uno reproduzca a otro.

### Interfaz y correcciones manuales
- **`/grupos` y `/grupos/{id}`:** lista y detalle, con explicación, cronología, relaciones y antecedentes. Desde el detalle se puede **separar**, **quitar una nota** o **unir** con otro grupo.
- **`/relaciones/{id}`:** comparación lado a lado, con las coincidencias resaltadas, fechas y precisión, enlaces originales, puntajes y reglas. Desde ahí se puede **confirmar** o **rechazar** la relación.
- Toda corrección queda en `manual_reviews`. Las relaciones revisadas no se modifican al reprocesar, y los grupos corregidos quedan bloqueados (`locked`): el motor no cambia su composición ni vuelve a agregar las notas separadas.

## Alertas

Código en `radar/alerts/`. Se crea **una alerta por grupo** cuando el grupo tiene coincidencias entre al menos **dos medios independientes**. Las republicaciones no cuentan como medio independiente, y Canal E cuenta como Perfil.

**La prioridad indica qué revisar primero. No significa falsedad ni coordinación.** Se configura en `/configuracion`:

| Prioridad por defecto | Evidencia |
|---|---|
| Baja | Tema compartido |
| Media | Posible mismo hecho con proximidad temporal (24 h por defecto; según la publicación si ambas notas tienen hora, si no según la detección) |
| Alta | Titular idéntico o casi idéntico, expresión distintiva compartida o enfoque similar respaldado por análisis (este último, en la etapa 5) |

**Reducción de ruido**
- **Una alerta por grupo:** lo garantiza un índice único. Repetir el análisis no duplica alertas.
- **Huella de evidencia:** resume los medios independientes, la prioridad, los tipos de relación, las frases y la fuente común. Si cambia, la alerta sube de versión y queda un evento con el detalle (por ejemplo, el medio agregado). Si no cambia (puntajes que varían, más notas del mismo medio), solo se actualizan los datos, sin evento ni aviso.
- **Reapertura:** una alerta *revisada* vuelve a *pendiente* ante evidencia nueva relevante. Las *descartadas* y *silenciadas* no se reabren solas.
- **Cooldown** configurable entre avisos de una misma alerta.
- **Temas silenciados:** la alerta se crea como *silenciada* y no se avisa.
- **Términos y secciones excluidos:** no se crea la alerta.
- **Posible fuente común:** informe, conferencia, agencia o entrevista, solo con indicios explícitos en los textos o metadatos (por ejemplo, "según un informe", "en conferencia de prensa", una nota republicada de agencia, "en diálogo con"). Es una pista para revisar, no una conclusión.

**Qué muestra cada alerta**
- qué coincide y los titulares de cada medio, con su subfuente;
- fechas y precisión, separando la *primera detección* (radar) de la *primera publicación según la fuente*;
- frases coincidentes, puntajes y reglas;
- enlaces originales, limitaciones e historial;
- estado: pendiente, revisada, descartada o silenciada.

### Telegram (opcional, desactivado por defecto)
- Se activa con `RADAR_TELEGRAM_ENABLED=true`, `RADAR_TELEGRAM_BOT_TOKEN`, `RADAR_TELEGRAM_CHAT_ID` y `RADAR_PUBLIC_URL`.
- Los avisos van a una cola persistente (tabla `notifications`) con una clave por versión de alerta, así que no se duplican.
- Hasta 5 intentos, con esperas de 1, 5, 15 y 60 minutos. Cada intento queda registrado en el historial de la alerta, y el token nunca se guarda en los errores.
- Solo se avisan las alertas con prioridad igual o mayor a la configurada (alta por defecto). El mensaje es breve: prioridad, medios, titular y enlace.
- Durante las pruebas el envío real está bloqueado: se usa un transporte simulado salvo que se defina `RADAR_TELEGRAM_ALLOW_TEST_SEND=1`.

## Análisis con IA externa (opcional)

Código en `radar/ai/`. **Viene desactivado, y la aplicación funciona igual sin él.** No se ejecutan modelos locales.

**Proveedores.** Hay una interfaz común (`providers.py`). Por defecto se usan **Groq** y luego **Fireworks** (`RADAR_AI_PROVIDER=groq,fireworks`): si el primero da un límite, se queda sin cuota, está caído o falta su configuración, se usa el siguiente. El uso se registra por proveedor.

| Proveedor | Endpoint | Configuración |
|---|---|---|
| Groq | `POST https://api.groq.com/openai/v1/chat/completions` | `GROQ_API_KEY`, `RADAR_GROQ_MODEL` (obligatorio), `RADAR_GROQ_STRICT` |
| Fireworks | `POST https://api.fireworks.ai/inference/v1/chat/completions` | `FIREWORKS_API_KEY`, `RADAR_FIREWORKS_MODEL` (obligatorio, `accounts/fireworks/models/...`) |
| Anthropic (opcional) | API Messages con el SDK oficial `anthropic` | `ANTHROPIC_API_KEY`, `RADAR_AI_MODEL` (por defecto `claude-opus-5-5`), `RADAR_AI_EFFORT`, `RADAR_AI_FALLBACKS` |

- **Groq y Fireworks** usan la API de chat compatible con OpenAI. La autenticación es `Authorization: Bearer`, y la salida se pide con `response_format: {"type": "json_schema", "json_schema": {"name", "schema"}}`, sin herramientas.
- **Modo estricto en Groq** (`strict: true`, activado por defecto): solo lo admiten algunos modelos. Si el elegido no lo soporta, Groq devuelve 400; en ese caso usá `RADAR_GROQ_STRICT=false`. La validación propia se aplica igual.
- **Sin modelo por defecto en Groq y Fireworks:** el catálogo cambia, así que se elige explícitamente. Sin modelo o sin clave, ese proveedor no se llama y `ai-status` indica qué falta.
- **Anthropic:** usa salida JSON con esquema estricto (`output_config.format`). Si el modelo declina la solicitud, la API la reintenta en un modelo de respaldo (`fallbacks: "default"`); se desactiva con `RADAR_AI_FALLBACKS=false`.
- **Claves:** nunca se muestran en el panel, y se ocultan de los errores registrados.
- **Activación:** `RADAR_AI_ENABLED=true`. Antes conviene revisar los límites de abajo: cada análisis es una llamada al proveedor, que puede tener costo.

**Configuración desde el panel.** En **Configuración → Análisis con IA** un administrador puede activar o desactivar la IA, el orden de proveedores, los pares por ejecución, los límites diarios de solicitudes y tokens, si se analiza al confirmar una relación y la prioridad mínima de la alerta cuyos pares se analizan solos. Lo guardado ahí **pisa al `.env`** (tabla `app_settings`, claves `ai_*`) y rige desde la próxima ejecución, sin reiniciar; `ai-status` y los comandos de la CLI también lo respetan. Las claves API y los modelos de Groq y Fireworks se siguen definiendo solo en el `.env`.

**Qué se envía:** solo relaciones que el motor léxico ya detectó, recientes (72 h) y de tipos relevantes (mismo hecho, expresión compartida, titular casi idéntico, afirmaciones distintas). Primero van las que forman parte de alertas pendientes, con un máximo de 10 pares por ejecución. Nunca se envía el histórico completo. Desde la comparación de una relación, un administrador puede pedir el análisis de ese par. Además, **al confirmar una relación** (botón de revisión) el par se analiza solo, si la IA está activada y dentro de los límites; un par ya analizado no se vuelve a enviar y, si el proveedor falla, la confirmación queda guardada igual.

**Salida validada.** Para cada par se piden:
- si es el mismo hecho, el tema compartido, el enfoque y las entidades;
- las afirmaciones principales, clasificadas como hecho, opinión, atribución causal, generalización o cita de un tercero;
- las atribuciones de responsabilidad, el alcance geográfico y el período al que refieren los datos (separado de la fecha de publicación);
- las diferencias de cifras, si una nota cita a la otra, fragmentos de evidencia, una explicación breve y las limitaciones.

La respuesta se **rechaza** si no es JSON válido, si no cumple el esquema o si algún fragmento citado no existe literalmente en la nota indicada. Antes de darla por inválida se hace **un único reintento** que le muestra al modelo qué fragmentos no existen y le pide copiarlos exactos o eliminarlos (cuenta como una solicitud más contra los límites diarios y no se hace si ya se alcanzaron). Se guardan el proveedor, el modelo, la versión del prompt y la fecha.

**Discrepancias:** se derivan para revisión humana (enfoques opuestos, alcance local frente a nacional, períodos distintos, posible confusión entre fecha de publicación y período estadístico, cifras distintas). El sistema **no determina** si una afirmación es verdadera.

**Protección**
- El texto de las notas va delimitado como dato no confiable (`<nota_A>`, `<nota_B>`). Se neutralizan las etiquetas que intenten cerrar el bloque, y el prompt indica ignorar cualquier instrucción incluida en las notas.
- El modelo no recibe herramientas ni puede ejecutar acciones.
- Durante las pruebas, la llamada real está bloqueada; todas las pruebas usan proveedores simulados.

**Costos y límites**
- Caché por contenido de ambas notas, versión del prompt y modelo: el mismo par sin cambios no se vuelve a enviar.
- Límites diarios de solicitudes y de tokens, longitud máxima de entrada y máximo de pares por ejecución.
- Uso registrado según lo informa el proveedor (tokens de entrada, salida y caché, más el id de la solicitud), en la tabla `ai_usage`.
- El costo se **estima** solo si se configuran tarifas propias (`RADAR_AI_PRICE_*`). Es una estimación, **no** facturación real; con tarifas, también se puede fijar un presupuesto diario.
- Al alcanzar un límite se dejan de hacer llamadas. Si el proveedor informa cuota o saldo agotado, se desactivan las llamadas hasta el día siguiente.
- Hasta 2 reintentos con espera (respetando `retry-after`). Un circuit breaker se abre por 30 minutos tras 3 fallos consecutivos.

**En la interfaz:** cada relación indica si fue *detectada por reglas*, *analizada por IA* o *confirmada (o rechazada) por el usuario*.
- El análisis por IA se guarda aparte (`ai_analyses`), así que reprocesar no lo borra ni toca las revisiones humanas.
- Una alerta sube a prioridad alta por "enfoque similar respaldado por análisis" solo cuando la IA indica mismo hecho y enfoque similar **sin** discrepancias. Aun así se presenta como pendiente de confirmación humana.

## Configuración desde el panel (administrador)

**Configuración** reúne los ajustes de operación, para no editar el `.env` ni reiniciar. Lo que se guarda en el panel **pisa al `.env`** (tabla `app_settings`); sin valor guardado rige el entorno. Los secretos (claves API, token de Telegram, URL de la base) **nunca** se editan ni se muestran en el panel.

| Sección | Qué se configura | Cuándo rige |
|---|---|---|
| Temas y secciones prioritarias | Palabras clave, exclusiones y secciones a priorizar | Próximo análisis |
| Alertas | Prioridades por tipo de evidencia, mínimo de medios independientes, cooldown, silenciados, prioridad mínima para Telegram | Próximo análisis |
| Análisis con IA | Activar, proveedores y orden, límites diarios, pares por ejecución, analizar al confirmar, prioridad mínima | Próxima ejecución |
| Telegram | Activar los avisos | Próximo ciclo |
| Recolección y retención | Páginas a leer por medio y ciclo, retención de notas y de ejecuciones/avisos | Próxima recolección o mantenimiento |
| Vista pública | Activar, alertas visibles (`revisadas`, `todas`, `ninguna`) y límite por IP | En unos segundos |
| Fuentes (`/fuentes`) | Pausar, intervalo mínimo por fuente (5 a 1440 minutos) y alta de medios | Próxima recolección |

Cada formulario exige CSRF y rol administrador y valida los rangos. Activar la vista pública hace visible el panel de lectura a cualquier persona: conviene revisar antes qué alertas se muestran.

## Vista pública (visitantes sin login)

Con `RADAR_PUBLIC_MODE=true`, cualquiera puede ver el panel **en modo de solo lectura** sin ingresar. Viene desactivada.

| Visible para visitantes | Solo con login |
|---|---|
| Resumen, Noticias (lista y ficha), Grupos y comparación lado a lado, Cronología, Métricas, Fuentes (sin mensajes de error) | Revisión, Historial, Ejecuciones, Configuración y **todas** las acciones (confirmar, separar, unir, pausar…) |
| Alertas según `RADAR_PUBLIC_ALERTS`: `revisadas` (por defecto), `todas` (incluye pendientes) o `ninguna` | Notas de revisión, historiales de alertas y correcciones, análisis de IA de relaciones no confirmadas |

- **Datos DEMO:** los visitantes nunca los ven, aunque `RADAR_DEMO_MODE` esté activo.
- **Aviso fijo:** las coincidencias son léxicas y no prueban coordinación ni indican qué medio originó una información.
- **Límite de solicitudes:** `RADAR_PUBLIC_RATE_LIMIT` por minuto e IP (120 por defecto) para visitantes, para no sobrecargar el servidor. Los archivos estáticos y los usuarios logueados quedan exentos.
- **Acceso:** el enlace "Ingresar" lleva al login; un administrador logueado ve todo como siempre.

## Panel

| Sección | Contenido |
|---|---|
| Resumen (`/`) | Alertas pendientes por prioridad, salud de fuentes, últimas notas y ejecuciones |
| Alertas (`/alertas`) | Lista por estado y prioridad; detalle con evidencia y revisión |
| Noticias (`/noticias`) | Búsqueda y filtros por medio, tema, sección, período y "solo con coincidencias" |
| Grupos (`/grupos`) | Grupos y comparación lado a lado (`/relaciones/{id}`) |
| Cronología (`/cronologia`) | Primera detección y primera publicación según la fuente, por grupo |
| Revisión (`/revision`) | Cola de alertas pendientes y relaciones sin revisar |
| Historial (`/historial`) | Titulares modificados por los medios, eventos de alertas y correcciones manuales |
| Métricas (`/metricas`) | Secuencias entre medios: casos, precisión temporal y cobertura |
| Fuentes y Ejecuciones | Estado operativo de cada fuente; ejecuciones y errores con filtros |
| Configuración | Temas, secciones prioritarias, reglas de alertas, Telegram (estado) y contraseña |

**Métricas de secuencias:** son descriptivas. Cuentan, por par de medios, cuántas veces uno publicó antes que el otro (solo cuando ambas notas tienen hora) y cuántas veces fue detectado antes. También muestran los casos sin hora, la cobertura y la mediana de diferencia. Publicar antes **no** indica que un medio dirija, origine o copie a otro.

## Fechas

- Todo se guarda en **UTC** y se muestra en **America/Argentina/Buenos_Aires**.
- La **publicación** (lo que declara el medio) se guarda aparte de la **primera y última detección** (cuándo la vio el radar).
- `published_precision` vale `datetime`, `date` o `none`. Cuando la fuente solo da la fecha, `published_at` queda vacío y se completa únicamente `published_date`, sin inventar una hora. Lo mismo vale para la fecha de modificación.

## Seguridad

- Contraseñas con Argon2id (64 MiB y 3 iteraciones), mínimo 12 caracteres.
- Sesión firmada en la cookie `radar_session`: HttpOnly, SameSite=Lax y Secure cuando `RADAR_ENV=production`. Se renueva al ingresar, y al cambiar la contraseña se invalidan las demás sesiones (`session_version`).
- Token CSRF sincronizado en todos los POST, sea por campo del formulario o por el encabezado `X-CSRF-Token` que agrega HTMX.
- Límite de intentos de login: 5 fallos cada 15 minutos por IP+usuario y 20 por IP. Los intentos se guardan en la base, así que el límite sobrevive a los reinicios.
- Jinja2 escapa automáticamente el contenido externo. El CSP es estricto (sin scripts ni estilos inline), con `X-Frame-Options: DENY` y HSTS en producción.
- Los XML se parsean con `defusedxml`, y no se aceptan URLs de artículos que no sean http/https.
- **Red (anti-SSRF)** en `radar/net.py`:
  - Solo se acepta http/https y los dominios permitidos de cada medio.
  - Se rechazan IP literales, credenciales en la URL, `localhost` y los hosts de metadatos de nubes.
  - El DNS de cada host debe resolver solo a IP públicas: se bloquean las privadas, loopback, link-local, CGNAT, reservadas y multicast, incluidas las IPv4 mapeadas en IPv6.
  - Las redirecciones se siguen a mano (como máximo 5) y cada salto se valida.
- Los secretos van en variables de entorno o en `.env`, que no se versiona.

## Base de datos: SQLite o PostgreSQL (Neon)

Por defecto se usa **SQLite** (WAL) en `RADAR_DATA_DIR`: ideal para un solo servidor. Para un servicio gestionado, o para que varias instancias compartan datos (por ejemplo Vercel), se usa **PostgreSQL**, probado con **Neon**:

```powershell
$env:RADAR_DATABASE_URL = "postgresql://USUARIO:CLAVE@ep-xxxx-pooler.REGION.aws.neon.tech/BASE?sslmode=require&channel_binding=require"
python -m radar init-db        # las migraciones usan la conexión directa (sin -pooler)
```

- **Driver:** `psycopg` 3 (`postgresql://` se convierte solo a `postgresql+psycopg://`).
- **Dos URLs:** con pooler para la aplicación y directa para migraciones y backups (`RADAR_DATABASE_URL_DIRECT`; si falta, se deriva quitando `-pooler`). El pooler de Neon es PgBouncer en modo transacción: no admite bloqueos de sesión, `SET` ni `LISTEN`, así que la app no los usa; con pooler se desactivan además las sentencias preparadas del cliente.
- **Bloqueo de ejecución exclusiva:** archivo en SQLite; tabla `job_locks` con vencimiento en PostgreSQL (segura entre procesos e instancias, y se libera sola si el proceso muere).
- **Backups:** en SQLite, la API de backup del propio motor; en PostgreSQL, `pg_dump` (formato custom) con verificación por `pg_restore --list`. La restauración sobre una base PostgreSQL viva no se automatiza (ver [VERCEL.md](VERCEL.md)).
- **Tamaño:** medido en PostgreSQL local, unos 7,4 KB por nota más 7,9 MB de base vacía. Con el plan gratuito de Neon (1 GB) conviene `RADAR_RETENTION_DAYS=90`.
- **Pruebas:** `pytest` corre sobre SQLite; con `RADAR_TEST_PG_URL=postgresql://usuario@host:puerto/base_de_pruebas` la misma suite corre sobre PostgreSQL (un esquema descartable por prueba; las específicas de SQLite se omiten y las de `pg_dump` se activan).

Despliegue en Vercel con Neon: [VERCEL.md](VERCEL.md).

## Producción (VPS Linux, 2 GB de RAM)

Ver **[DEPLOYMENT.md](DEPLOYMENT.md)**. Ahí están los comandos exactos, las variables, los scripts de instalación, actualización, backup, restauración y rollback (en `deploy/scripts/`), las unidades systemd (`deploy/systemd/`), Nginx con HTTPS (`deploy/nginx/`) y las mediciones de rendimiento.

**Windows y WSL:** no compartas el mismo `.venv` ni la misma base SQLite entre Windows y WSL. SQLite en modo WAL sobre `/mnt/c` o `/mnt/f` no coordina bien los bloqueos entre los dos sistemas, y la base puede corromperse. En WSL, usá un venv propio (por ejemplo, `.venv-wsl`) y una base dentro del sistema de archivos de Linux (`RADAR_DATA_DIR=/home/usuario/radar-data`).

## Estructura

```
radar/
  config.py, db.py, models.py, security.py, timeutil.py, text.py, queries.py
  joblock.py (bloqueo exclusivo), pipeline.py (ciclos reutilizables), pgbackup.py
  seed.py, demo.py, cli.py, __main__.py
  net.py       cliente HTTP seguro (SSRF, reintentos, límites, robots.txt)
  sources_status.py  estado operativo y cobertura por fuente
  alerts/      config.py (reglas configurables), engine.py (una alerta por grupo),
               common_source.py (posible fuente común)
  notify/      telegram.py (cola persistente, opcional)
  ai/          config.py, providers.py (interfaz + Anthropic), openai_compat.py (Groq,
               Fireworks y conmutación), prompt.py, schema.py
               (esquema y validación), service.py (preselección, caché, límites, breaker)
  metrics.py   secuencias descriptivas entre medios
  analysis/    textproc.py (español), vectorize.py (TF-IDF), relations.py (reglas),
               grouping.py (anti-encadenamiento), engine.py, manual.py (correcciones)
  collector/   adapters/ (perfil, eldestape, infobae, pagina12), parsers.py, html.py,
               ingest.py (dedupe, versiones, procedencia), enrich.py (páginas),
               references.py, runner.py (concurrencia, lock, registro)
  web/         app.py, deps.py, routes/ (auth, dashboard, articles, sources, settings)
  templates/   Jinja2 + parciales HTMX
  static/      css/app.css, js/htmx.min.js (2.0.4)
alembic/       migraciones 0001 a 0006 (esquema, recolección, relaciones, alertas, IA, bloqueos)
app.py         punto de entrada para Vercel (variable `app`); vercel.json
VERCEL.md      despliegue en Vercel con PostgreSQL en Neon
deploy/systemd unidades web, recolección y timer
tests/         login, permisos, persistencia, adaptadores, red, recolector, análisis, alertas, panel, PostgreSQL/Vercel
```
