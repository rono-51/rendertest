"""
Utilidades para descargar/identificar videos (streams m3u8, VODs, archivos
locales) con ffmpeg. Usado por gemini_clip_analyzer.py para bajar clips
antes de subirlos a Gemini.
"""
import subprocess
import os


def is_stream_url(path: str) -> bool:
    """Detecta si el input es una URL (http/https) en vez de un archivo local."""
    return path.startswith("http://") or path.startswith("https://")


def download_stream(url: str, output_dir: str = "tmp", filename: str = "downloaded_video.mp4",
                     referer: str = None, user_agent: str = None,
                     start: float = None, duration: float = None) -> str:
    """
    Descarga un stream (m3u8/HLS u otro que ffmpeg soporte) a un archivo
    MP4 local. Para VOD (video ya terminado) esto trae el archivo completo;
    para streams EN VIVO, esto graba desde el momento en que se ejecuta
    hasta que el stream termine o se corte manualmente (Ctrl+C) — no es
    recomendable para lives largos sin límite de tiempo.

    Si se indican `start`/`duration` (en segundos), solo se descarga ese
    intervalo — mucho más rápido que traer el VOD completo cuando ya sabes
    qué tramo te interesa. Requiere que la URL sea de un VOD (playlist con
    duración fija); en un stream en vivo el seek a un punto pasado no
    siempre es posible (ver notas en el README).
    """
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, filename)

    header_lines = []
    if referer:
        header_lines.append(f"Referer: {referer}")
    if user_agent:
        header_lines.append(f"User-Agent: {user_agent}")

    def build_cmd(video_codec_args):
        cmd = ["ffmpeg", "-y"]
        if header_lines:
            cmd += ["-headers", "\r\n".join(header_lines) + "\r\n"]
        if start is not None:
            cmd += ["-ss", str(start)]  # seek ANTES de -i: rápido y preciso en HLS
        cmd += ["-i", url]
        if duration is not None:
            cmd += ["-t", str(duration)]
        cmd += video_codec_args + [output_path]
        return cmd

    result = subprocess.run(build_cmd(["-c", "copy", "-bsf:a", "aac_adtstoasc"]),
                             capture_output=True, text=True)
    if result.returncode != 0:
        # Fallback: recodificar si la copia directa falla (formatos mixtos,
        # codecs no soportados en mp4 por copia, etc.)
        result2 = subprocess.run(build_cmd(["-c:v", "libx264", "-c:a", "aac"]),
                                  capture_output=True, text=True)
        if result2.returncode != 0:
            raise RuntimeError(f"ffmpeg falló descargando el stream:\n{result2.stderr}")

    return output_path
