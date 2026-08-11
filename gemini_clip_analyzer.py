"""
Análisis editorial de clips usando Gemini multimodal (video + audio nativo).

Reemplaza el pipeline anterior (rank_clips.py con Groq + Whisper + ranking
comparativo). Este módulo NO rankea clips entre sí — da un veredicto
independiente por clip, analizando el video directamente (sin transcribir
primero), que es lo que Gemini permite al aceptar video nativo.

Flujo por clip:
    1. Descargar el clip (m3u8 -> mp4 local, con ffmpeg) si viene de una URL.
    2. Subir el mp4 a Gemini (client.files.upload).
    3. Esperar a que termine de procesarse en la nube.
    4. Pedir el veredicto con el prompt de editor (una sola llamada).
    5. Parsear VEREDICTO FINAL / CALIFICACIÓN del texto de respuesta.
    6. Borrar el archivo subido a Gemini (no dejar basura en la nube).

Requiere: pip install google-genai
Variable de entorno: GOOGLE_API_KEY (nunca hardcodear la key en el código).
"""
import os
import re
import time
from dataclasses import dataclass
from typing import Optional

from google import genai

from audio_utils import is_stream_url, download_stream

MODELO_PRINCIPAL = "gemini-flash-latest"
MODELO_RESPALDO = "gemini-2.0-flash-lite"

PROMPT_TEMPLATE = """
Eres un creador de contenido experto en la comunidad de streamers peruanos
(Dota 2, Kick, Twitch, IRL, King House, Sideral, Kingteka, Sachauzumaki,
Daarick, etc.). Tu objetivo es analizar clips de video y evaluar su
potencial viral para TikTok/Reels del o la streamer '{streamer}'.

{contexto_clip}

Para cada video debes proporcionar:
1. Análisis de Viralidad: Un puntaje del 1 al 10 y un desglose de POR QUÉ
   funcionará (tomando en cuenta el "lore", polémicas recientes, humor, o
   ganchos visuales).
2. Títulos Sugeridos:
   - Un título impactante para colocar DENTRO del video (overlay text).
   - Un título/caption atractivo para la descripción de TikTok (hook/enganche).
3. Hashtags Sugeridos: Menciones al streamer, la comunidad, eventos o
   palabras clave de tendencia.

Usa un tono fresco, directo, adaptado a la comunidad (habla barrio/criollo
sin filtro). Entiende con naturalidad la jerga local, gamer/streaming y
groserías genuinas de streaming en vivo — son información real sobre la
intensidad del momento, no algo que debas censurar, suavizar o evitar
analizar en tu diagnóstico.

Responde ÚNICAMENTE con la siguiente estructura exacta (sin texto antes ni
después, sin markdown):

---
PUNTAJE VIRAL: [X/10]

ANALISIS: [desglose concreto de por qué funcionará o no, 2-4 frases]

TITULO OVERLAY: [título corto y llamativo para superponer en el video]

TITULO TIKTOK: [título/caption para la descripción de TikTok]

HASHTAGS: [5-8 hashtags separados por espacio, cada uno con #]
---
"""


@dataclass
class ClipVerdict:
    id: str
    score: Optional[float] = None       # parseado de "PUNTAJE VIRAL: X/10"
    analysis: str = ""                  # desglose de por qué funcionará (o no)
    title_overlay: str = ""             # título para superponer en el video
    title_tiktok: str = ""              # título/caption para la descripción de TikTok
    hashtags: list = None               # lista de hashtags parseados, ej. ["#Kick", "#Peru"]
    full_text: str = ""                 # respuesta cruda completa, por si el parseo falla
    error: Optional[str] = None         # si algo falló, el resto de campos quedan vacíos/None

    def __post_init__(self):
        if self.hashtags is None:
            self.hashtags = []


def build_prompt(streamer: Optional[str] = None, category: Optional[str] = None,
                  title: Optional[str] = None) -> str:
    """Arma el prompt, agregando contexto del clip si está disponible."""
    streamer_display = streamer or "el streamer"

    contexto_lines = []
    if category:
        contexto_lines.append(f"Categoría: {category}")
    if title:
        contexto_lines.append(f"Título original del clip: {title}")

    contexto = ("Contexto adicional del clip:\n" + "\n".join(contexto_lines) + "\n") if contexto_lines else ""
    return PROMPT_TEMPLATE.format(streamer=streamer_display, contexto_clip=contexto)


_FIELD_RE = re.compile(
    r"PUNTAJE\s+VIRAL:\s*\[?\s*(?P<score>\d+(?:\.\d+)?)\s*/\s*10\s*\]?"
    r".*?AN[AÁ]LISIS:\s*\[?\s*(?P<analysis>.*?)\s*\]?\s*"
    r"T[IÍ]TULO\s+OVERLAY:\s*\[?\s*(?P<title_overlay>.*?)\s*\]?\s*"
    r"T[IÍ]TULO\s+TIKTOK:\s*\[?\s*(?P<title_tiktok>.*?)\s*\]?\s*"
    r"HASHTAGS:\s*\[?\s*(?P<hashtags>.*?)\s*\]?\s*(?:---\s*$|\Z)",
    re.IGNORECASE | re.DOTALL,
)


def parse_analysis(text: str):
    """
    Extrae score/analysis/title_overlay/title_tiktok/hashtags del texto de
    respuesta. Si el parseo estricto falla (el modelo no siguió el formato
    exacto), devuelve valores vacíos/None pero NUNCA explota — el texto
    crudo siempre queda disponible en full_text para revisar a mano.
    """
    match = _FIELD_RE.search(text)
    if not match:
        return None, "", "", "", []

    score = float(match.group("score"))
    analysis = match.group("analysis").strip()
    title_overlay = match.group("title_overlay").strip()
    title_tiktok = match.group("title_tiktok").strip()
    hashtags = re.findall(r"#\S+", match.group("hashtags"))

    return score, analysis, title_overlay, title_tiktok, hashtags


def upload_video_to_gemini(client: "genai.Client", video_path: str, poll_seconds: float = 2.0):
    """Sube el video y espera a que termine de procesarse en la nube."""
    print(f"[gemini] Subiendo {video_path}...")
    video_file = client.files.upload(file=video_path)

    while video_file.state.name == "PROCESSING":
        time.sleep(poll_seconds)
        video_file = client.files.get(name=video_file.name)

    if video_file.state.name == "FAILED":
        raise RuntimeError("Gemini falló procesando el video subido.")

    return video_file


def _extract_response_text(response) -> Optional[str]:
    """Extracción defensiva de texto, evitando None si la API no devuelve texto plano."""
    if getattr(response, "text", None):
        return response.text
    candidates = getattr(response, "candidates", None)
    if candidates and candidates[0].content:
        partes = candidates[0].content.parts or []
        text = "".join(getattr(part, "text", "") or "" for part in partes)
        return text or None
    return None


def analyze_video_with_gemini(client: "genai.Client", video_file, prompt: str,
                               max_intentos: int = 3, wait_seconds: float = 20.0) -> str:
    """
    Llama a Gemini con reintentos y fallback a modelo Lite ante saturación
    (429/RESOURCE_EXHAUSTED/503/UNAVAILABLE), igual que el script original.
    """
    last_error = None
    for intento in range(1, max_intentos + 1):
        modelo_actual = MODELO_PRINCIPAL if intento < max_intentos else MODELO_RESPALDO
        print(f"[gemini] Generando análisis con {modelo_actual} (intento {intento}/{max_intentos})...")

        try:
            response = client.models.generate_content(
                model=modelo_actual,
                contents=[video_file, prompt],
            )
            texto = _extract_response_text(response)
            if texto:
                return texto

            # La API respondió pero sin texto (ej. bloqueado por seguridad)
            candidatos = getattr(response, "candidates", None)
            motivo = getattr(candidatos[0], "finish_reason", "N/A") if candidatos else "N/A"
            raise RuntimeError(f"Gemini no devolvió texto (finish_reason={motivo})")

        except Exception as e:
            last_error = e
            error_str = str(e)
            if any(code in error_str for code in ["429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE"]):
                print(f"[gemini] Límite/saturación, esperando {wait_seconds:.0f}s antes de reintentar...")
                time.sleep(wait_seconds)
                continue
            raise

    raise RuntimeError(f"Se agotaron los reintentos ante Gemini: {last_error}")


def analyze_clip(clip_id: str, video_path_or_url: str, streamer: str = None,
                  category: str = None, title: str = None,
                  referer: str = None, user_agent: str = None,
                  api_key: str = None) -> ClipVerdict:
    """
    Pipeline completo para UN clip: descarga (si es URL) -> sube a Gemini
    -> analiza -> parsea veredicto -> limpia el archivo subido.
    """
    client = genai.Client(api_key=api_key or os.environ.get("GOOGLE_API_KEY"))
    video_path = video_path_or_url
    downloaded_locally = False

    try:
        if is_stream_url(video_path_or_url):
            video_path = download_stream(video_path_or_url, output_dir="tmp_gemini",
                                          filename=f"{clip_id}.mp4",
                                          referer=referer, user_agent=user_agent)
            downloaded_locally = True

        video_file = upload_video_to_gemini(client, video_path)

        try:
            prompt = build_prompt(streamer=streamer, category=category, title=title)
            texto = analyze_video_with_gemini(client, video_file, prompt)
            score, analysis, title_overlay, title_tiktok, hashtags = parse_analysis(texto)
            return ClipVerdict(id=clip_id, score=score, analysis=analysis,
                                title_overlay=title_overlay, title_tiktok=title_tiktok,
                                hashtags=hashtags, full_text=texto)
        finally:
            try:
                client.files.delete(name=video_file.name)
            except Exception:
                pass  # no bloqueamos el resultado por un fallo de limpieza

    except Exception as e:
        return ClipVerdict(id=clip_id, error=str(e))

    finally:
        if downloaded_locally and os.path.exists(video_path):
            try:
                os.remove(video_path)
            except OSError:
                pass


def load_clips_json(json_path: str) -> list:
    """
    Carga la lista de clips desde un archivo JSON local o una URL (mismo
    formato que devuelve la API de clips de Kick). Devuelve una lista de
    dicts con: id, title, clip_url, duration, category, streamer.
    """
    import json as json_module

    if json_path.startswith("http://") or json_path.startswith("https://"):
        import urllib.request
        req = urllib.request.Request(json_path, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json_module.loads(resp.read().decode("utf-8"))
    else:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json_module.load(f)

    raw_clips = data.get("clips", data if isinstance(data, list) else [])
    clips = []
    for c in raw_clips:
        channel = c.get("channel") or {}
        streamer_name = channel.get("username") or channel.get("slug") if isinstance(channel, dict) else None
        category = c.get("category")
        category_name = category.get("name") if isinstance(category, dict) else category
        clips.append({
            "id": c["id"],
            "title": c.get("title", ""),
            "clip_url": c.get("clip_url") or c.get("video_url"),
            "duration": float(c.get("duration", 0)),
            "category": category_name,
            "streamer": streamer_name,
        })
    return clips


def analyze_clips_batch(json_path: str, max_clips: int = 10, referer: str = None,
                         user_agent: str = None, api_key: str = None,
                         pause_between_clips: float = 2.0) -> list:
    """
    Procesa una lista de clips (JSON local/URL) uno por uno con Gemini.
    Devuelve una lista de ClipVerdict. No compara/rankea entre sí — cada
    clip recibe un veredicto independiente.
    """
    clips = load_clips_json(json_path)
    print(f"{len(clips)} clips cargados desde {json_path}")

    if max_clips and len(clips) > max_clips:
        print(f"Limitando a los primeros {max_clips} clips (de {len(clips)} disponibles)")
        clips = clips[:max_clips]

    results = []
    for i, clip in enumerate(clips, start=1):
        print(f"\n[{i}/{len(clips)}] Analizando clip {clip['id']} ({clip['title']})...")
        verdict = analyze_clip(
            clip_id=clip["id"],
            video_path_or_url=clip["clip_url"],
            streamer=clip.get("streamer"),
            category=clip.get("category"),
            title=clip.get("title"),
            referer=referer,
            user_agent=user_agent,
            api_key=api_key,
        )
        results.append(verdict)
        if verdict.error:
            print(f"  -> ERROR: {verdict.error}")
        else:
            print(f"  -> {verdict.score}/10 | overlay: {verdict.title_overlay!r} | tiktok: {verdict.title_tiktok!r}")

        if i < len(clips):
            time.sleep(pause_between_clips)

    return results


if __name__ == "__main__":
    import argparse
    import json as json_module
    from dataclasses import asdict

    parser = argparse.ArgumentParser(description="Analiza clips con Gemini multimodal (veredicto editorial, sin ranking)")
    parser.add_argument("json_path", help="Ruta a un archivo JSON local, o URL (mismo formato que la API de clips de Kick)")
    parser.add_argument("--max-clips", type=int, default=10)
    parser.add_argument("--referer", default=None)
    parser.add_argument("--user-agent", default=None)
    parser.add_argument("--output", default="verdicts.json")
    args = parser.parse_args()

    results = analyze_clips_batch(args.json_path, max_clips=args.max_clips,
                                   referer=args.referer, user_agent=args.user_agent)

    print("\n=== RESULTADOS ===")
    for r in results:
        print(f"{r.id}: {r.score}/10 - {r.title_overlay}" if not r.error else f"{r.id}: ERROR - {r.error}")

    with open(args.output, "w", encoding="utf-8") as f:
        json_module.dump([asdict(r) for r in results], f, ensure_ascii=False, indent=2)
    print(f"\nResultados guardados en {args.output}")
