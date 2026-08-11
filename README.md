# Analizador de clips (Kick) con Gemini + Render Studio

Sistema para analizar clips cortos ya cortados (ej. de Kick) con Gemini
multimodal (video + audio nativo, sin transcripción previa), obteniendo
para cada uno: score de viralidad, análisis de por qué funcionaría (o no),
título para overlay dentro del video, título/caption para TikTok, y
hashtags sugeridos. Desde el mismo dashboard podés mandar cualquier clip
directo a **Render Studio** para exportarlo en el formato que quieras.

## Estructura del proyecto

```
tu_carpeta/
├── api.py                    # servidor (API + sirve el frontend)
├── gemini_clip_analyzer.py   # lógica de análisis con Gemini
├── audio_utils.py            # descarga de clips (m3u8 -> mp4) con ffmpeg
├── requirements.txt
└── public/
    ├── dashboard-clips-kick.html   # dashboard: buscar streamer, ver clips, analizar
    └── render-studio.html          # editor/exportador de clips
```

`api.py` sirve automáticamente todo lo que esté en `public/` en la raíz del
servidor — un solo proceso, un solo puerto, atiende tanto la API como el
frontend.

## Cómo funciona

1. **`public/dashboard-clips-kick.html`**: buscás un streamer de Kick (vía
   tu Worker de Cloudflare, que lista sus clips recientes), los ves con
   preview (click para reproducir), seleccionás los que quieras con
   checkboxes, y le das a **"Analizar seleccionados"**.
2. El dashboard manda esos clips a `POST /analyze_clips`. El servidor
   descarga cada clip (ffmpeg) y lo sube directo a Gemini (`google-genai`),
   que analiza video+audio en una sola llamada — sin pasar por Whisper ni
   transcripción intermedia.
3. Cada clip recibe: score (0-10), análisis de por qué funcionaría,
   título overlay, título TikTok, y hashtags. Los clips con score bajo se
   atenúan visualmente en la tarjeta (señal de descarte).
4. Con el botón **"🎬 Renderizar"** en cualquier tarjeta, el clip (+ los
   datos ya resueltos por Gemini) se manda a `render-studio.html` — se abre
   solo, con el video y los campos ya cargados, listo para exportar.

## Requisitos previos

- Python 3.9+
- `ffmpeg` instalado y en el PATH
- Una API key de Google AI Studio (`GOOGLE_API_KEY`) — gratis en
  https://aistudio.google.com/apikey
  - ⚠️ Nunca la hardcodees en el código ni la subas a un repo. Si en algún
    momento quedó expuesta en texto plano (chat, commit, captura), revócala
    y generá una nueva.

## Instalación

```bash
pip install -r requirements.txt
export GOOGLE_API_KEY="tu-api-key"
```

## Uso en PC

```bash
uvicorn api:app --host 0.0.0.0 --port 8000
```

Abrí `http://localhost:8000/dashboard-clips-kick.html`.

## Uso en celular (misma red WiFi que tu PC)

1. Corré el servidor igual que arriba, con `--host 0.0.0.0` (así acepta
   conexiones de otros dispositivos, no solo de la PC).
2. Buscá la IP local de tu PC:
   - Windows: `ipconfig` → "Dirección IPv4" (ej. `192.168.1.50`)
   - Mac/Linux: `ifconfig` o `ip addr`
3. Desde el navegador del celular (**conectado a la misma WiFi**):
   ```
   http://192.168.1.50:8000/dashboard-clips-kick.html
   ```

Todo el trabajo pesado (ffmpeg, la llamada a Gemini) corre en tu PC — el
celular es solo pantalla remota.

**Si no conecta desde el celular**: revisá que el Firewall de Windows no
esté bloqueando el puerto 8000 para conexiones entrantes de la red local
(Panel de Control → Firewall de Windows Defender → Configuración avanzada
→ Regla de entrada nueva → Puerto → TCP 8000 → Permitir la conexión).

## Por qué esto NO funciona con doble clic (`file://`)

Si abrís `dashboard-clips-kick.html` o `render-studio.html` directo desde
el explorador de archivos (doble clic), Chrome trata cada archivo `file://`
como un origen distinto y bloquea la comunicación entre ambos (el traspaso
de video+datos hacia Render Studio usa `IndexedDB`, que requiere mismo
origen). Por eso siempre hay que abrirlos a través de `http://` (el
servidor de `api.py`), nunca con doble clic directo.

## Notas sobre el free tier de Gemini

- Modelos usados (con fallback automático ante saturación 429/503):
  principal `gemini-flash-latest`, respaldo `gemini-2.0-flash-lite`.
- El servidor procesa los clips **uno por uno con una pausa de 2s entre
  cada uno** para no saturar la cuota — si analizás muchos clips de una,
  vas a ver el progreso (`i/n`) avanzar de a poco, es esperado.
- Si te da error de cuota/rate-limit igual, revisá tus límites reales en
  https://ai.google.dev/gemini-api/docs/rate-limits — pueden cambiar sin
  aviso.

## Limitaciones actuales (a tener en cuenta)

- **Jobs en memoria**: si reiniciás el servidor, se pierde el registro de
  análisis en curso/pasados. Para uso personal no es un problema; si en
  algún momento esto corre para varios usuarios a la vez, convendría una
  cola real (Redis/Celery) en vez de un dict de Python.
- **Sin autenticación**: cualquiera con la URL del servidor puede lanzar
  análisis y gastar tu cuota de Gemini. Fine para uso local/personal; si
  algún día lo exponés a internet público (no solo tu WiFi), agregar algún
  tipo de clave de acceso.
- **CORS abierto a `"*"`**: para simplificar pruebas locales. Si lo
  desplegás en un dominio público, conviene restringirlo al dominio real
  del frontend.

## Desplegarlo en la nube (Render.com, free) — para usarlo desde cualquier lado

Como el frontend (`public/`) y el backend viven en el **mismo servidor**
(`api.py` sirve todo), desplegarlo es desplegar UNA sola cosa — no hay que
separar "frontend en GitHub Pages" de "backend en otro lado". Usamos
Render porque: soporta Docker (necesario para instalar `ffmpeg`), tiene
proceso persistente de verdad (las `BackgroundTasks` de FastAPI necesitan
esto para seguir corriendo después de responder la petición HTTP), y tiene
free tier sin pedir tarjeta.

### Paso 1 — Subir el código a GitHub

```bash
git init
git add .
git commit -m "Clip analyzer"
```

Creá un repo en GitHub (puede ser privado) y pusheá:
```bash
git remote add origin https://github.com/TU_USUARIO/TU_REPO.git
git push -u origin main
```

⚠️ Con el `.gitignore` que te dejé, `streamer_presets.json` y demás
archivos locales NO se suben — está bien, son datos de tu máquina, no
código.

### Paso 2 — Crear el servicio en Render

1. Entrá a https://render.com y creá una cuenta (gratis, sin tarjeta).
2. **New +** → **Blueprint** → conectá tu repo de GitHub.
3. Render va a detectar el `render.yaml` solo y proponer el servicio
   `clip-analyzer` con plan Free.
4. Te va a pedir que cargues `GOOGLE_API_KEY` a mano (por seguridad, nunca
   se guarda en el repo/blueprint) — pegá tu key ahí.
5. **Deploy**. La primera build tarda unos minutos (instala ffmpeg +
   dependencias). Las siguientes son más rápidas por el cacheo de capas.

Si preferís no usar el Blueprint, también podés hacer **New +** → **Web
Service** → conectar el repo → Render detecta el `Dockerfile` solo →
cargás `GOOGLE_API_KEY` en Environment → Deploy.

### Paso 3 — Usarlo

Render te da una URL tipo `https://clip-analyzer-xxxx.onrender.com`. Abrí:
```
https://clip-analyzer-xxxx.onrender.com/dashboard-clips-kick.html
```

Ya no dependés de tu WiFi ni de que tu PC esté prendida — funciona desde
cualquier lado con internet, PC o celular.

### Cosas del free tier de Render a tener en cuenta

- **Se "duerme" a los 15 min sin tráfico.** La primera petición después de
  dormido tarda 30-60s en "despertar" — es normal, no es que se rompió.
  Como ya usás el patrón de `job_id` + polling, esto no rompe el análisis
  en sí, solo hace que la carga inicial de la página sea más lenta a veces.
- **Filesystem efímero — CONFIRMADO en la documentación de Render**: los
  presets de streamer (`streamer_presets.json`) se van a **perder cada vez
  que el servicio se duerma y despierte**, no solo en un redeploy. Para
  uso personal esto es medio molesto pero no bloqueante (simplemente
  reconfigurás el encuadre de vuelta la primera vez que lo uses después de
  cada sleep). Si te termina molestando mucho, avisame y lo pasamos a un
  Render Key Value (su store administrado) o algo similar — no lo armé de
  entrada para no sumar complejidad/otro servicio a esta primera vuelta.
- **750 horas gratis por mes** por workspace — de sobra para uso personal
  (un servicio corriendo full mes son ~730hs, así que si tenés más de un
  servicio free dormido gran parte del tiempo, no hay drama).

### Seguridad antes de compartir la URL con alguien más

- CORS sigue abierto a `"*"` — no es grave ahora porque frontend/backend
  son el mismo origen, pero si algún día agregás otro frontend en otro
  dominio, restringilo.
- **No hay autenticación** — cualquiera con la URL puede lanzar análisis y
  gastar tu cuota de Gemini. Para uso personal (vos desde tu celular/PC)
  no importa. Si vas a compartir la URL con otras personas, contame y
  vemos cómo agregar alguna traba simple (ej. una clave fija que pida el
  dashboard antes de dejarte usarlo).

### Lo que no pude verificar yo

No tengo Docker disponible en mi entorno para probar el build de la
imagen antes de dártelo — armé el `Dockerfile` con cuidado y la lógica es
sólida, pero la primera vez que Render lo builde de verdad es la primera
prueba real. Si el build falla, pegame el log de Render y lo arreglamos.

