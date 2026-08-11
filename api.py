"""
API HTTP para el análisis de clips cortos con Gemini y Renderizado con FFmpeg,
conectada al dashboard (public/dashboard-clips-kick.html) y Render Studio
(public/render-studio.html).

Instalación:
    pip install fastapi uvicorn python-multipart google-genai

Uso:
    export GOOGLE_API_KEY="tu-api-key"
    uvicorn api:app --host 0.0.0.0 --port 8000
"""

import json
import os
import subprocess
import traceback
import uuid
from typing import List, Optional

from fastapi import FastAPI, BackgroundTasks, HTTPException, Body, File, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from gemini_clip_analyzer import analyze_clip as gemini_analyze_clip

app = FastAPI(title="Clip Analyzer & Render API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO: restringir a tu dominio antes de producción
    allow_methods=["*"],
    allow_headers=["*"],
)

# Carpetas de trabajo para renderizado
UPLOAD_FOLDER = "uploads"
OUTPUT_FOLDER = "outputs"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# Jobs en memoria
ANALYZE_JOBS: dict = {}
RENDER_JOBS: dict = {}


# --- SECCIÓN 1: ANÁLISIS CON GEMINI ---

def _process_analyze_job(job_id: str, clips: list):
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
                time_module.sleep(2.0)

        ANALYZE_JOBS[job_id]["status"] = "done"

    except Exception as e:
        ANALYZE_JOBS[job_id]["status"] = "error"
        ANALYZE_JOBS[job_id]["error"] = str(e)
        traceback.print_exc()


@app.post("/analyze_clips")
async def analyze_clips_endpoint(background_tasks: BackgroundTasks, clips: List[dict] = Body(...)):
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


# --- SECCIÓN 2: RENDERIZADO EN EL SERVIDOR CON FFMPEG ---

def _process_render_job(
    job_id: str, 
    input_path: str, 
    output_path: str, 
    layout: str, 
    trim_start: float, 
    trim_end: float, 
    streamer: str, 
    resolution: str
):
    try:
        RENDER_JOBS[job_id]["status"] = "processing"

        target_w = 1080 if resolution == "1080" else 720
        target_h = 1920 if resolution == "1080" else 1280

        # Filtro de FFmpeg: Recorte Split-Screen + Superposición de Texto
        filter_complex = (
            f"[0:v]trim=start={trim_start}:end={trim_end},setpts=PTS-STARTPTS[v_trimmed];"
            f"[v_trimmed]split=2[cam][game];"
            f"[cam]crop=iw:ih/2:0:0,scale={target_w}:{target_h//2}[top];"
            f"[game]crop=iw:ih/2:0:ih/2,scale={target_w}:{target_h//2}[bottom];"
            f"[top][bottom]vstack=inputs=2[v_stacked];"
            f"[v_stacked]drawtext=text='{streamer.upper()}':fontcolor=white:fontsize=48:x=(w-text_w)/2:y=h-100[v_final]"
        )

        cmd = [
            'ffmpeg', '-y',
            '-i', input_path,
            '-filter_complex', filter_complex,
            '-map', '[v_final]',
            '-map', '0:a?',
            '-c:v', 'libx264',
            '-preset', 'fast',
            '-crf', '23',
            '-c:a', 'aac',
            output_path
        ]

        subprocess.run(cmd, check=True)

        RENDER_JOBS[job_id]["status"] = "done"
        RENDER_JOBS[job_id]["output_url"] = f"/download_render/{job_id}"

    except Exception as e:
        RENDER_JOBS[job_id]["status"] = "error"
        RENDER_JOBS[job_id]["error"] = str(e)
        traceback.print_exc()
    finally:
        if os.path.exists(input_path):
            os.remove(input_path)


@app.post("/render_video")
async def render_video_endpoint(
    background_tasks: BackgroundTasks,
    video: UploadFile = File(...),
    layout: str = Form("streamer"),
    trimStart: float = Form(0.0),
    trimEnd: float = Form(0.0),
    streamer: str = Form("streamer"),
    resolution: str = Form("1080")
):
    job_id = str(uuid.uuid4())
    input_path = os.path.join(UPLOAD_FOLDER, f"{job_id}_{video.filename}")
    output_path = os.path.join(OUTPUT_FOLDER, f"rendered_{job_id}.mp4")

    with open(input_path, "wb") as f:
        f.write(await video.read())

    RENDER_JOBS[job_id] = {
        "status": "queued",
        "output_url": None,
        "error": None
    }

    background_tasks.add_task(
        _process_render_job,
        job_id,
        input_path,
        output_path,
        layout,
        trimStart,
        trimEnd,
        streamer,
        resolution
    )

    return {"job_id": job_id}


@app.get("/render_status/{job_id}")
async def render_status(job_id: str):
    job = RENDER_JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job_id de renderizado no encontrado")
    return job


@app.get("/download_render/{job_id}")
async def download_render(job_id: str):
    output_path = os.path.join(OUTPUT_FOLDER, f"rendered_{job_id}.mp4")
    if not os.path.exists(output_path):
        raise HTTPException(404, "El archivo procesado no existe o venció")
    return FileResponse(output_path, media_type="video/mp4", filename=f"clip_{job_id}.mp4")


# --- SECCIÓN 3: PRESETS DE ENCUADRE POR STREAMER ---

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


# --- ARCHIVOS ESTÁTICOS ---

if os.path.isdir("public"):
    app.mount("/", StaticFiles(directory="public", html=True), name="static")

