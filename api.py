"""
API HTTP para el análisis de clips cortos con Gemini, conectada al
dashboard (public/dashboard-clips-kick.html) y Render Studio
(public/render-studio.html).

Instalación:
    pip install fastapi uvicorn python-multipart google-genai

Uso:
    export GOOGLE_API_KEY="tu-api-key"
    uvicorn api:app --host 0.0.0.0 --port 8000

Luego abre http://localhost:8000/dashboard-clips-kick.html (o la IP de tu
PC en la red local para usarlo desde el celular — ver README).

Endpoints:
    POST /analyze_clips
        body JSON: [{ "id", "title", "clip_url", "duration", "category", "streamer" }, ...]
        -> { "job_id": "..." }
        Analiza cada clip con Gemini multimodal (video+audio nativo, sin
        transcripción previa), devolviendo un análisis independiente por
        clip (score, título overlay, título TikTok, hashtags). No compara
        clips entre sí.

    GET /analyze_status/{job_id}
        -> { "status": "queued"|"processing"|"done"|"error",
             "results": [{ "id", "score", "analysis", "title_overlay",
                            "title_tiktok", "hashtags", "full_text", "error" }, ...],
             "progress": "3/10", "error": null }
        "results" se va llenando progresivamente a medida que cada clip
        termina, así que puedes hacer polling y mostrar resultados parciales.

Notas sobre esta versión (MVP):
- Los jobs se guardan en memoria (dict) — si reinicias el servidor, se
  pierde el registro de jobs anteriores.
- Un solo proceso corriendo (uvicorn sin --workers) procesa los jobs uno
  por uno, no en paralelo real. Suficiente para uso personal; para varios
  usuarios concurrentes convendría una cola real (Celery/RQ) más adelante.
- CORS está abierto a "*" para probar fácil. Restringe esto a tu dominio
  real antes de exponerlo público de verdad.
"""
import json
import os
import traceback
import uuid
from typing import List

from fastapi import FastAPI, BackgroundTasks, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from gemini_clip_analyzer import analyze_clip as gemini_analyze_clip

app = FastAPI(title="Clip Analyzer API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO: restringir a tu dominio antes de producción
    allow_methods=["*"],
    allow_headers=["*"],
)

# job_id -> {"status":..., "results": [...], "progress": "i/n", "error": None}
ANALYZE_JOBS: dict = {}


def _process_analyze_job(job_id: str, clips: list):
    """
    Analiza cada clip de la lista con Gemini, uno por uno. No compara clips
    entre sí — cada uno recibe un análisis independiente. Actualizamos
    ANALYZE_JOBS progresivamente para que el frontend pueda hacer polling y
    ver resultados a medida que van llegando.
    """
    import time as time_module

    try:
        ANALYZE_JOBS[job_id]["status"] = "processing"
        results = []

        for i, clip in enumerate(clips, start=1):
            verdict = gemini_analyze_clip(
                clip_id=clip["id"],
                video_path_or_url=clip.get("clip_url") or clip.get("video_url"),
                streamer=clip.get("streamer") or (clip.get("channel") or {}).get("username"),
                category=clip.get("category") if isinstance(clip.get("category"), str)
                         else (clip.get("category") or {}).get("name"),
                title=clip.get("title"),
            )
            results.append(verdict.__dict__)
            ANALYZE_JOBS[job_id]["results"] = results
            ANALYZE_JOBS[job_id]["progress"] = f"{i}/{len(clips)}"

            if i < len(clips):
                time_module.sleep(2.0)  # no saturar la API de Gemini

        ANALYZE_JOBS[job_id]["status"] = "done"

    except Exception as e:
        ANALYZE_JOBS[job_id]["status"] = "error"
        ANALYZE_JOBS[job_id]["error"] = str(e)
        traceback.print_exc()


@app.post("/analyze_clips")
async def analyze_clips_endpoint(background_tasks: BackgroundTasks, clips: List[dict] = Body(...)):
    """
    Recibe una lista de clips seleccionados desde el dashboard (mismo
    formato que devuelve la API de Kick: id, title, clip_url, duration,
    category, channel) y le pide a Gemini un análisis independiente por
    cada uno.
    """
    if not clips:
        raise HTTPException(400, "Manda al menos un clip en la lista")

    job_id = str(uuid.uuid4())
    ANALYZE_JOBS[job_id] = {"status": "queued", "results": [], "progress": "0/0", "error": None}

    background_tasks.add_task(_process_analyze_job, job_id, clips)
    return {"job_id": job_id}


@app.get("/analyze_status/{job_id}")
async def analyze_status(job_id: str):
    job = ANALYZE_JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job_id no encontrado")
    return job


# --- Presets de encuadre por streamer (Render Studio) ---
# Guarda la posición/zoom de cámara + layout elegido para cada streamer, en
# un JSON en disco (no una base de datos real — para uso personal alcanza,
# y así sobrevive a reinicios del servidor a diferencia de los jobs en
# memoria). Al guardarlo en el servidor (en vez de localStorage del
# navegador) se sincroniza solo entre la PC y el celular, ya que ambos le
# pegan al mismo servidor.
STREAMER_PRESETS_FILE = "streamer_presets.json"


def _load_streamer_presets() -> dict:
    if os.path.exists(STREAMER_PRESETS_FILE):
        try:
            with open(STREAMER_PRESETS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_streamer_presets(data: dict):
    with open(STREAMER_PRESETS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


STREAMER_PRESETS = _load_streamer_presets()


@app.get("/streamer_presets/{streamer}")
async def get_streamer_preset(streamer: str):
    key = streamer.strip().lower()
    preset = STREAMER_PRESETS.get(key)
    if preset is None:
        raise HTTPException(404, "No hay preset guardado para este streamer")
    return preset


@app.post("/streamer_presets/{streamer}")
async def save_streamer_preset(streamer: str, preset: dict = Body(...)):
    key = streamer.strip().lower()
    STREAMER_PRESETS[key] = preset
    _save_streamer_presets(STREAMER_PRESETS)
    return {"status": "ok"}


@app.delete("/streamer_presets/{streamer}")
async def delete_streamer_preset(streamer: str):
    key = streamer.strip().lower()
    if key in STREAMER_PRESETS:
        del STREAMER_PRESETS[key]
        _save_streamer_presets(STREAMER_PRESETS)
    return {"status": "ok"}


# --- Archivos estáticos (dashboard, render-studio, banners/logos) ---
# IMPORTANTE: este mount va AL FINAL, después de declarar todas las rutas
# de la API de arriba. FastAPI prioriza las rutas explícitas (@app.get/
# @app.post) sobre lo que cae dentro del mount, así que /analyze_clips y
# /analyze_status siguen funcionando normal aunque "/" también sirva
# archivos estáticos.
#
# Poné dashboard-clips-kick.html, render-studio.html, y cualquier logo/
# banner (ej. streamradar_logo.png, daarick.jpg) dentro de una carpeta
# "public/" al lado de api.py. Así todo —API y frontend— sale del mismo
# servidor/puerto, lo cual es necesario para que funcione desde el celular
# (ver README) y también evita el problema de "file:// URLs are treated as
# unique security origins" que rompe el IndexedDB entre dashboard y
# render-studio si los abrís con doble clic en vez de por HTTP.
if os.path.isdir("public"):
    app.mount("/", StaticFiles(directory="public", html=True), name="static")
