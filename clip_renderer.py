import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Optional, List, Tuple

from audio_utils import is_stream_url, download_stream

CANVAS_W = 1080
CANVAS_H = 1920

# 📂 Rutas a tus Assets
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(BASE_DIR, "assets")
FONT_PATH = os.path.join(ASSETS_DIR, "fonts", "Anton-Regular.ttf")
LOGO_PATH = os.path.join(ASSETS_DIR, "logos", "streamradar_logo.png")
BANNERS_DIR = os.path.join(ASSETS_DIR, "banners")

# Fuente de respaldo por si no se encuentra Anton-Regular.ttf
DEFAULT_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


@dataclass
class Region:
    zoom: float = 10.0  # zoom real = zoom / 10
    x: float = 50.0     # 0-100 (%)
    y: float = 50.0     # 0-100 (%)


@dataclass
class RenderParams:
    clip_url: str
    layout: str                         # "streamer" | "blur"
    region_a: Region = field(default_factory=Region)
    region_b: Region = field(default_factory=Region)
    split_pct: float = 32.0             # solo aplica a "streamer"
    title: str = ""
    streamer: str = ""
    show_title: bool = True
    show_badge: bool = True
    show_banner: bool = True
    trim_start: Optional[float] = None
    trim_end: Optional[float] = None
    referer: Optional[str] = None
    user_agent: Optional[str] = None


def _make_even(val: int) -> int:
    """Asegura que una dimensión sea par (requerido por pix_fmt yuv420p)."""
    return round(val / 2) * 2


def _ffprobe_dimensions(video_path: str) -> Tuple[int, int]:
    """Devuelve (width, height) del video de entrada."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "json",
        video_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe falló leyendo dimensiones:\n{result.stderr}")
    data = json.loads(result.stdout)
    stream = data["streams"][0]
    return int(stream["width"]), int(stream["height"])


def _cover_crop(vW: int, vH: int, W: int, zoneH: int, region: Region) -> dict:
    zoom = max(region.zoom, 1) / 10
    pX = min(max(region.x, 0), 100) / 100
    pY = min(max(region.y, 0), 100) / 100

    scale = max(W / vW, zoneH / vH) * zoom
    crop_w = W / scale
    crop_h = zoneH / scale
    crop_x = (vW - crop_w) * pX
    crop_y = (vH - crop_h) * pY

    crop_w = min(crop_w, vW)
    crop_h = min(crop_h, vH)
    crop_x = min(max(crop_x, 0), vW - crop_w)
    crop_y = min(max(crop_y, 0), vH - crop_h)

    return {
        "w": round(crop_w), "h": round(crop_h),
        "x": round(crop_x), "y": round(crop_y),
    }


def _escape_drawtext(text: str) -> str:
    """Escapa caracteres especiales para el filtro drawtext de FFmpeg."""
    return (text.replace("\\", "\\\\")
                .replace(":", "\\:")
                .replace("'", "\u2019")
                .replace("%", "\\%"))


def _scale_bottom(bottom: dict, H: int) -> dict:
    sy = H / 1920
    return {
        "titleBarY": round(bottom["titleBarY"] * sy),
        "titleBarH": round(bottom["titleBarH"] * sy),
        "badgeY":    round(bottom["badgeY"] * sy),
        "bannerY":   round(bottom["bannerY"] * sy),
        "bannerH":   round(bottom["bannerH"] * sy),
    }


LAYOUTS = {
    "streamer": {
        "mode": "dual",
        "videoZoneH": 1920,
        "bottom": {"titleBarY": 1555, "titleBarH": 70, "badgeY": 1640, "bannerY": 1700, "bannerH": 220},
    },
    "blur": {
        "mode": "single",
        "videoZoneH": 1580,
        "bottom": {"titleBarY": 1550, "titleBarH": 70, "badgeY": 1630, "bannerY": 1665, "bannerH": 255},
    },
}


def build_filter_complex(params: RenderParams, vW: int, vH: int) -> Tuple[str, List[str]]:
    """
    Arma el grafo de filtros y retorna una tupla:
    (filter_complex_string, lista_de_entradas_adicionales_de_imagenes)
    """
    if params.layout not in LAYOUTS:
        raise ValueError(f"Layout '{params.layout}' no soportado aún.")

    W, H = CANVAS_W, CANVAS_H
    def_ = LAYOUTS[params.layout]
    video_zone_h = _make_even(round(def_["videoZoneH"] * (H / 1920)))
    bottom = _scale_bottom(def_["bottom"], H)
    
    name = params.streamer.strip().upper()
    streamer_clean = params.streamer.strip().lower().replace(" ", "")
    title = params.title.strip().upper()

    # Seleccionar Fuente
    active_font = FONT_PATH if os.path.exists(FONT_PATH) else DEFAULT_FONT
    font_arg = active_font.replace("\\", "/").replace(":", "\\:")

    # Verificar Entradas de Imágenes Extras
    extra_inputs = []
    banner_file = os.path.join(BANNERS_DIR, f"{streamer_clean}.jpg")
    has_banner = params.show_banner and os.path.exists(banner_file)
    has_logo = os.path.exists(LOGO_PATH)

    filters = []
    last_label = None

    if def_["mode"] == "dual":
        split_y = _make_even(round(video_zone_h * (params.split_pct / 100)))
        zone_a_h = split_y
        zone_b_h = video_zone_h - split_y

        crop_a = _cover_crop(vW, vH, W, zone_a_h, params.region_a)
        crop_b = _cover_crop(vW, vH, W, zone_b_h, params.region_b)

        filters.append(f"[0:v]crop={crop_a['w']}:{crop_a['h']}:{crop_a['x']}:{crop_a['y']},scale={W}:{zone_a_h}[zoneA]")
        filters.append(f"[0:v]crop={crop_b['w']}:{crop_b['h']}:{crop_b['x']}:{crop_b['y']},scale={W}:{zone_b_h}[zoneB]")
        filters.append("[zoneA][zoneB]vstack=inputs=2[stacked]")
        last_label = "stacked"

        if params.show_title and title:
            esc_title = _escape_drawtext(title)
            fontsize = round(40 * (W / 1080))
            filters.append(
                f"[{last_label}]drawtext=fontfile='{font_arg}':text='{esc_title}':"
                f"fontsize={fontsize}:fontcolor=white:"
                f"box=1:boxcolor=black@0.72:boxborderw=16:"
                f"x=(w-text_w)/2:y={split_y}-(text_h/2)[capt]"
            )
            last_label = "capt"

    else:  # Layout Blur / Single
        zoom = max(params.region_a.zoom, 1) / 10
        pX = min(max(params.region_a.x, 0), 100) / 100
        pY = min(max(params.region_a.y, 0), 100) / 100

        # ⚡ OPTIMIZACIÓN DE BLUR DE ALTO IMPACTO:
        # 1. Escala a 270x480 (16x menos píxeles a procesar)
        # 2. Aplica boxblur en lugar de gblur (mucho más liviano para la CPU)
        # 3. Escala de vuelta al canvas 1080x1920
        low_w, low_h = 270, 480
        filters.append(
            f"[0:v]scale={low_w}:{low_h}:force_original_aspect_ratio=increase,"
            f"crop={low_w}:{low_h},"
            f"boxblur=luma_radius=10:luma_power=2,"
            f"scale={W}:{H},"
            f"eq=brightness=-0.35:saturation=1.4[bg]"
        )

        src_w = round(vW / zoom)
        src_h = round(vH / zoom)
        src_x = round((vW - src_w) * pX)
        src_y = round((vH - src_h) * pY)
        src_w = min(src_w, vW); src_h = min(src_h, vH)
        src_x = min(max(src_x, 0), vW - src_w)
        src_y = min(max(src_y, 0), vH - src_h)

        sf = min(W / vW, video_zone_h / vH)
        dW = _make_even(round(vW * sf))
        dH = _make_even(round(vH * sf))
        dX = round((W - dW) / 2)
        dY = round((video_zone_h - dH) / 2)

        filters.append(f"[0:v]crop={src_w}:{src_h}:{src_x}:{src_y},scale={dW}:{dH}[fg]")
        filters.append(f"[bg][fg]overlay={dX}:{dY}[stacked]")
        last_label = "stacked"

        if params.show_title and title:
            esc_title = _escape_drawtext(title)
            fontsize = round(62 * (W / 1080))
            filters.append(
                f"[{last_label}]drawbox=x=0:y={bottom['titleBarY']}:w={W}:h={bottom['titleBarH']}:"
                f"color=black@0.9:t=fill[titlebar]"
            )
            filters.append(
                f"[titlebar]drawtext=fontfile='{font_arg}':text='{esc_title}':"
                f"fontsize={fontsize}:fontcolor=white:"
                f"x=(w-text_w)/2:y={bottom['titleBarY']}+({bottom['titleBarH']}-text_h)/2[titled]"
            )
            last_label = "titled"

    # Overlay de Logo (Arriba a la derecha)
    if has_logo:
        extra_inputs.append(LOGO_PATH)
        logo_idx = len(extra_inputs)
        filters.append(f"[{logo_idx}:v]scale=200:-1[logo_scaled]")
        filters.append(f"[{last_label}][logo_scaled]overlay=x=W-w-30:y=30[logoed]")
        last_label = "logoed"

    # Badge KICK / Streamer
    if params.show_badge and name:
        fontsize = round(38 * (W / 1080))
        combined = _escape_drawtext(f"KICK / {name}")
        filters.append(
            f"[{last_label}]drawtext=fontfile='{font_arg}':text='{combined}':"
            f"fontsize={fontsize}:fontcolor=0x53fc18:"
            f"x=(w-text_w)/2:y={bottom['badgeY']}-(text_h/2)[badged]"
        )
        last_label = "badged"

    # Overlay de Banner del Streamer
    if has_banner:
        extra_inputs.append(banner_file)
        banner_idx = len(extra_inputs)
        filters.append(f"[{banner_idx}:v]scale=1080:-1[banner_scaled]")
        filters.append(f"[{last_label}][banner_scaled]overlay=x=0:y={bottom['bannerY']}[bannered]")
        last_label = "bannered"
    elif params.show_banner and name:
        # Fallback si no hay imagen de banner
        fontsize = round(48 * (W / 1080))
        esc_name = _escape_drawtext(name)
        filters.append(
            f"[{last_label}]drawbox=x=0:y={bottom['bannerY']}:w={W}:h={bottom['bannerH']}:"
            f"color=0x0b1420:t=fill[bannerbg]"
        )
        filters.append(
            f"[bannerbg]drawtext=fontfile='{font_arg}':text='{esc_name}':"
            f"fontsize={fontsize}:fontcolor=0x53fc18:"
            f"x=(w-text_w)/2:y={bottom['bannerY']}+({bottom['bannerH']}-text_h)/2[bannered]"
        )
        last_label = "bannered"

    filters.append(f"[{last_label}]format=yuv420p[vout]")

    return ";\n".join(filters), extra_inputs


# Modifica tu archivo clip_renderer.py en render_clip():
import time

def render_clip(params: RenderParams, output_path: str) -> str:
    t0 = time.time()
    video_path = params.clip_url
    downloaded_locally = False

    try:
        if is_stream_url(params.clip_url):
            print("⏳ Descargando video fuente...")
            video_path = download_stream(
                params.clip_url, output_dir="tmp_render",
                filename="source.mp4",
                referer=params.referer, user_agent=params.user_agent,)
            downloaded_locally = True
            print(f"✅ Descarga completada en {round(time.time() - t0, 2)}s")

        t_render = time.time()
        vW, vH = _ffprobe_dimensions(video_path)
        filter_complex, extra_inputs = build_filter_complex(params, vW, vH)

        cmd = ["ffmpeg", "-y", "-threads", "1"]

        if params.trim_start is not None:
            cmd += ["-ss", str(params.trim_start)]

        cmd += ["-i", video_path]

        for extra_in in extra_inputs:
            cmd += ["-i", extra_in]

        if params.trim_end is not None and params.trim_start is not None:
            cmd += ["-t", str(max(0.1, params.trim_end - params.trim_start))]

        cmd += [
            "-filter_complex", filter_complex,
            "-map", "[vout]", "-map", "0:a?",
            "-r", "30",  # ⚡ NUEVO: Forzar 30 FPS para procesar la mitad de fotogramas
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
            "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart",
            output_path,
        ]

        print("⚡ Iniciando procesamiento FFmpeg...")
        subprocess.run(cmd, capture_output=True, text=True)
        print(f"🎉 Renderizado finalizado en {round(time.time() - t_render, 2)}s (Tiempo total: {round(time.time() - t0, 2)}s)")

        return output_path

    finally:
        if downloaded_locally and os.path.exists(video_path):
            try:
                os.remove(video_path)
            except OSError:
                pass
