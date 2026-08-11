# Imagen base liviana con Python 3.11
FROM python:3.11-slim

# ffmpeg es necesario para descargar/remuxear los clips (m3u8 -> mp4)
# antes de subirlos a Gemini. --no-install-recommends para mantener la
# imagen chica.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copiamos requirements primero (aparte del resto del código) para que
# Docker cachee esta capa y no reinstale todo en cada build si solo
# cambiaste un .py.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Resto del código (api.py, gemini_clip_analyzer.py, audio_utils.py, public/)
COPY . .

# Render (y la mayoría de los hosts free) inyectan la variable de entorno
# PORT en tiempo de ejecución — el server tiene que escuchar ahí, no en un
# puerto fijo. El valor 8000 de acá es solo un default para correrlo local
# con `docker run` sin pasar PORT explícito.
ENV PORT=8000
EXPOSE 8000

# Forma "shell" (no la forma lista/exec) para que ${PORT} se expanda en
# tiempo de ejecución con el valor real que te da el host.
CMD uvicorn api:app --host 0.0.0.0 --port ${PORT}
