"""
Renderizado de clips en el servidor con ffmpeg — reemplaza la exportación
client-side (canvas + MediaRecorder) de render-studio.html, que usaba la
GPU/CPU del dispositivo del usuario para componer y codificar el video.

IMPORTANTE — alcance de esta primera versión: replica el motor de dibujo
de render-studio.html (canvas 1080×1920) para DOS layouts:
  - "streamer" (Streamer Cam: cam arriba + gameplay abajo)
  - "blur"     (Full Frame: clip completo con fondo difuminado)

Los otros 4 layouts (fill, casino, podcast, interview) NO están
implementados todavía acá — si se piden, la función levanta un error claro
en vez de renderizar algo incorrecto en silencio.

Matemática de recorte replicada 1:1 desde el motor de canvas (ver
drawCoverZone/drawBlurZone en render-studio.html):

  cover-crop (zona rellena el rect, recorta sobrante):
    scale = max(W/vW, zoneH/vH) * zoom
    visible_src_w = W / scale
    visible_src_h = zoneH / scale
    crop_x = (vW - visible_src_w) * pX
    crop_y = (vH - visible_src_h) * pY

  contain-blur (clip completo, sin recortar, con blur de fondo):
    fondo: video escalado a "cover" de todo el canvas, con blur+oscurecido
    frente: crop de (vW/zoom, vH/zoom) en (pX,pY) del original, escalado
            "contain" dentro de la zona y centrado

Fuente: no tenemos el archivo Anton-Regular.ttf que usa el canvas del
navegador — usamos DejaVu Sans Bold (viene con el paquete fonts-dejavu-core
en el Dockerfile) como sustituto. El resultado se va a ver ligeramente
distinto tipográficamente al preview del navegador hasta que subamos la
fuente real a public/assets/fonts/ y actualicemos FONT_PATH acá.
"""
import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Optional

from audio_utils import is_stream_url, download_stream

CANVAS_W = 1080
CANVAS_H = 1920

# Sustituto de la fuente Anton del navegador (ver nota en el docstring).
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

KICK_GREEN = "0x53fc18"


@dataclass
class Region:
    zoom: float = 10.0  # igual que en el canvas: zoom real = zoom/10
    x: float = 50.0      # 0-100 (%)
    y: float = 50.0      # 0-100 (%)


@dataclass
class RenderParams:
    clip_url: str
    layout: str                       # "streamer" | "blur"
    region_a: Region = field(default_factory=Region)
    region_b: Region = field(default_factory=Region)
    split_pct: float = 32.0           # solo aplica a "streamer"
    title: str = ""
    streamer: str = ""
    show_title: bool = True
    show_badge: bool = True
    show_banner: bool = True
    trim_start: Optional[float] = None
    trim_end: Optional[float] = None
    referer: Optional[str] = None
    user_agent: Optional[str] = None


def _ffprobe_dimensions(video_path: str) -> tuple:
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
    """
    Calcula el crop (en píxeles del video ORIGINAL) equivalente al
    "cover-crop" del canvas para una zona de tamaño W×zoneH.
    Devuelve dict con crop_w/crop_h/crop_x/crop_y, ya clampeados a rangos
    válidos.
    """
    zoom = max(region.zoom, 1) / 10
    pX = min(max(region.x, 0), 100) / 100
    pY = min(max(region.y, 0), 100) / 100

    scale = max(W / vW, zoneH / vH) * zoom
    crop_w = W / scale
    crop_h = zoneH / scale
    crop_x = (vW - crop_w) * pX
    crop_y = (vH - crop_h) * pY

    # Clamps de seguridad: nunca pedir más de lo que el video tiene
    crop_w = min(crop_w, vW)
    crop_h = min(crop_h, vH)
    crop_x = min(max(crop_x, 0), vW - crop_w)
    crop_y = min(max(crop_y, 0), vH - crop_h)

    return {
        "w": round(crop_w), "h": round(crop_h),
        "x": round(crop_x), "y": round(crop_y),
    }


def _escape_drawtext(text: str) -> str:
    """Escapa texto para el filtro drawtext de ffmpeg."""
    return (text.replace("\\", "\\\\")
                .replace(":", "\\:")
                .replace("'", "\u2019")   # reemplazo visual, evita romper el filtro
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


def build_filter_complex(params: RenderParams, vW: int, vH: int) -> str:
    """
    Arma el grafo de filtros de ffmpeg completo para el layout pedido.
    Devuelve el string listo para pasarle a -filter_complex.
    """
    if params.layout not in LAYOUTS:
        raise ValueError(
            f"Layout '{params.layout}' no implementado en el renderizador de servidor todavía "
            f"(soportados: {list(LAYOUTS.keys())})"
        )

    W, H = CANVAS_W, CANVAS_H
    def_ = LAYOUTS[params.layout]
    video_zone_h = round(def_["videoZoneH"] * (H / 1920))
    bottom = _scale_bottom(def_["bottom"], H)
    name = params.streamer.strip().upper()
    title = params.title.strip().upper()

    filters = []
    last_label = None  # etiqueta del último frame compuesto, antes de overlays de texto

    if def_["mode"] == "dual":
        split_y = round(video_zone_h * (params.split_pct / 100))
        zone_a_h = split_y
        zone_b_h = video_zone_h - split_y

        crop_a = _cover_crop(vW, vH, W, zone_a_h, params.region_a)
        crop_b = _cover_crop(vW, vH, W, zone_b_h, params.region_b)

        filters.append(
            f"[0:v]crop={crop_a['w']}:{crop_a['h']}:{crop_a['x']}:{crop_a['y']},"
            f"scale={W}:{zone_a_h}[zoneA]"
        )
        filters.append(
            f"[0:v]crop={crop_b['w']}:{crop_b['h']}:{crop_b['x']}:{crop_b['y']},"
            f"scale={W}:{zone_b_h}[zoneB]"
        )
        filters.append("[zoneA][zoneB]vstack=inputs=2[stacked]")
        last_label = "stacked"

        # Caption central en el punto de división (solo si hay título)
        if params.show_title and title:
            esc_title = _escape_drawtext(title)
            fontsize = round(40 * (W / 1080))
            filters.append(
                f"[{last_label}]drawtext=fontfile={FONT_PATH}:text='{esc_title}':"
                f"fontsize={fontsize}:fontcolor=white:"
                f"box=1:boxcolor=black@0.72:boxborderw=16:"
                f"x=(w-text_w)/2:y={split_y}-(text_h/2)[capt]"
            )
            last_label = "capt"

    else:  # single / blur (contain-blur)
        zoom = max(params.region_a.zoom, 1) / 10
        pX = min(max(params.region_a.x, 0), 100) / 100
        pY = min(max(params.region_a.y, 0), 100) / 100

        # Fondo: cover de todo el canvas + blur + oscurecido
        filters.append(
            f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},gblur=sigma=20,eq=brightness=-0.35:saturation=1.4[bg]"
        )

        # Frente: crop "contain" con zoom/pan, centrado dentro de video_zone_h
        src_w = round(vW / zoom)
        src_h = round(vH / zoom)
        src_x = round((vW - src_w) * pX)
        src_y = round((vH - src_h) * pY)
        src_w = min(src_w, vW); src_h = min(src_h, vH)
        src_x = min(max(src_x, 0), vW - src_w)
        src_y = min(max(src_y, 0), vH - src_h)

        sf = min(W / vW, video_zone_h / vH)
        dW = round(vW * sf)
        dH = round(vH * sf)
        dX = round((W - dW) / 2)
        dY = round((video_zone_h - dH) / 2)

        filters.append(
            f"[0:v]crop={src_w}:{src_h}:{src_x}:{src_y},scale={dW}:{dH}[fg]"
        )
        filters.append(f"[bg][fg]overlay={dX}:{dY}[stacked]")
        last_label = "stacked"

        # Barra de título sólida (solo en modo single)
        if params.show_title and title:
            esc_title = _escape_drawtext(title)
            fontsize = round(62 * (W / 1080))
            filters.append(
                f"[{last_label}]drawbox=x=0:y={bottom['titleBarY']}:w={W}:h={bottom['titleBarH']}:"
                f"color=black@0.9:t=fill[titlebar]"
            )
            filters.append(
                f"[titlebar]drawtext=fontfile={FONT_PATH}:text='{esc_title}':"
                f"fontsize={fontsize}:fontcolor=white:"
                f"x=(w-text_w)/2:y={bottom['titleBarY']}+({bottom['titleBarH']}-text_h)/2[titled]"
            )
            last_label = "titled"

    # Badge KICK / streamer (ambos modos)
    if params.show_badge:
        fontsize = round(38 * (W / 1080))
        esc_kick = "KICK"
        esc_name = _escape_drawtext(f" / {name}")
        # Aproximación de centrado: dibujamos "KICK / NOMBRE" como un solo
        # texto centrado (el canvas original pinta KICK en verde y el resto
        # en blanco por separado — acá simplificamos a un solo color por
        # limitación de drawtext; ver nota más abajo).
        combined = _escape_drawtext(f"KICK / {name}")
        filters.append(
            f"[{last_label}]drawtext=fontfile={FONT_PATH}:text='{combined}':"
            f"fontsize={fontsize}:fontcolor=0x53fc18:"
            f"x=(w-text_w)/2:y={bottom['badgeY']}-(text_h/2)[badged]"
        )
        last_label = "badged"

    # Banner (fallback de texto — no tenemos imágenes de banner por
    # streamer todavía, así que replicamos el fallback: franja oscura +
    # nombre en verde centrado)
    if params.show_banner:
        fontsize = round(48 * (W / 1080))
        esc_name = _escape_drawtext(name)
        filters.append(
            f"[{last_label}]drawbox=x=0:y={bottom['bannerY']}:w={W}:h={bottom['bannerH']}:"
            f"color=0x0b1420:t=fill[bannerbg]"
        )
        filters.append(
            f"[bannerbg]drawtext=fontfile={FONT_PATH}:text='{esc_name}':"
            f"fontsize={fontsize}:fontcolor=0x53fc18:"
            f"x=(w-text_w)/2:y={bottom['bannerY']}+({bottom['bannerH']}-text_h)/2[bannered]"
        )
        last_label = "bannered"

    filters.append(f"[{last_label}]format=yuv420p[vout]")

    return ";\n".join(filters)


def render_clip(params: RenderParams, output_path: str) -> str:
    """
    Pipeline completo: descarga el clip (si es URL), arma el filtro y
    corre ffmpeg. Devuelve la ruta del archivo final.
    """
    video_path = params.clip_url
    downloaded_locally = False

    try:
        if is_stream_url(params.clip_url):
            video_path = download_stream(
                params.clip_url, output_dir="tmp_render",
                filename="source.mp4",
                referer=params.referer, user_agent=params.user_agent,
            )
            downloaded_locally = True

        vW, vH = _ffprobe_dimensions(video_path)
        filter_complex = build_filter_complex(params, vW, vH)

        cmd = ["ffmpeg", "-y"]
        if params.trim_start is not None:
            cmd += ["-ss", str(params.trim_start)]
        cmd += ["-i", video_path]
        if params.trim_end is not None and params.trim_start is not None:
            cmd += ["-t", str(max(0.1, params.trim_end - params.trim_start))]

        cmd += [
            "-filter_complex", filter_complex,
            "-map", "[vout]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            output_path,
        ]

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg falló renderizando:\n{result.stderr[-3000:]}")

        return output_path

    finally:
        if downloaded_locally and os.path.exists(video_path):
            try:
                os.remove(video_path)
            except OSError:
                pass
