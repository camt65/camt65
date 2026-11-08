from __future__ import annotations

import csv
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import requests


VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}
SUBTITLE_EXTENSIONS = {".srt", ".vtt"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

VALID_TYPES = {
    "video",
    "imagen",
    "video_stock",
    "imagen_stock",
}

USER_AGENT = (
    "RecolectorRecursos/1.0 "
    "(aplicacion local para organizar recursos audiovisuales)"
)

MAX_IMAGE_BYTES = 40 * 1024 * 1024
MAX_VIDEO_BYTES = 700 * 1024 * 1024


def emit(message: str, callback=None) -> None:
    """Envía un mensaje al callback de la interfaz o a la consola."""
    if callback:
        try:
            callback(str(message))
            return
        except Exception:
            pass
    print(str(message), flush=True)


def safe_name(value: str, max_length: int = 80) -> str:
    """Convierte texto en un nombre de archivo razonablemente seguro."""
    text = str(value or "").strip()
    text = re.sub(r"[^\w.-]+", "_", text, flags=re.UNICODE)
    text = text.strip(" ._")
    text = text[:max_length] or "recurso"

    # Nombres reservados de Windows.
    if text.upper() in {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        text = "_" + text

    return text


def tool_path(name: str) -> str | None:
    """
    Busca ffmpeg/ffprobe en PATH o en carpetas junto al programa.

    Para una aplicación empaquetada se puede colocar:
      ffmpeg\\bin\\ffmpeg.exe
      ffmpeg\\bin\\ffprobe.exe
    junto al ejecutable.
    """
    executable_name = name + (".exe" if os.name == "nt" else "")

    if getattr(sys, "frozen", False):
        app_dir = Path(sys.executable).resolve().parent
    else:
        app_dir = Path(__file__).resolve().parent

    candidates = [
        app_dir / "ffmpeg" / "bin" / executable_name,
        app_dir / "ffmpeg" / executable_name,
        app_dir / "bin" / executable_name,
    ]

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    return shutil.which(name)


def require_ffmpeg() -> tuple[str, str]:
    ffmpeg = tool_path("ffmpeg")
    ffprobe = tool_path("ffprobe")

    if not ffmpeg or not ffprobe:
        raise RuntimeError(
            "No encuentro FFmpeg y FFprobe. Instálalos y añádelos al PATH, "
            "o coloca sus ejecutables en una carpeta ffmpeg\\bin junto al programa."
        )

    return ffmpeg, ffprobe


def probe_duration(video_path: Path) -> float:
    """Devuelve la duración del vídeo mediante ffprobe."""
    _, ffprobe = require_ffmpeg()

    command = [
        ffprobe,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )

    if result.returncode != 0:
        detail = (result.stderr or "ffprobe no pudo leer el archivo.")[-1000:]
        raise RuntimeError(detail)

    try:
        duration = float(result.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        raise RuntimeError("No se pudo determinar la duración del vídeo.")

    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError("La duración del vídeo no es válida.")

    return duration


def parse_timestamp(value: str) -> float:
    """Convierte HH:MM:SS,mmm o HH:MM:SS.mmm a segundos."""
    value = value.replace(",", ".")
    hours, minutes, seconds = value.split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def normalize_text(value: str) -> str:
    """Normaliza texto conservando letras Unicode, incluido japonés."""
    value = html.unescape(str(value or ""))
    value = re.sub(r"<[^>]*>", " ", value)
    value = re.sub(r"\{\\.*?\}", " ", value)

    value = unicodedata.normalize("NFKD", value)
    value = "".join(
        char for char in value
        if not unicodedata.combining(char)
    )
    value = value.casefold()
    value = re.sub(r"[\W_]+", " ", value, flags=re.UNICODE)
    return value.strip()


TIME_PATTERN = re.compile(
    r"(?P<start>\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})"
    r"\s*-->\s*"
    r"(?P<end>\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})"
)


def read_subtitle_cues(subtitle_path: Path) -> list[dict]:
    """Lee entradas temporizadas de SRT o VTT."""
    raw = subtitle_path.read_text(encoding="utf-8-sig", errors="replace")
    blocks = re.split(r"\r?\n\s*\r?\n", raw)

    cues = []

    for block in blocks:
        match = TIME_PATTERN.search(block)
        if not match:
            continue

        start = parse_timestamp(match.group("start"))
        end = parse_timestamp(match.group("end"))

        caption = block[match.end():]
        caption = re.sub(r"<[^>]*>", " ", caption)
        caption = re.sub(r"\{\\.*?\}", " ", caption)
        caption_text = normalize_text(caption)

        if end >= start:
            cues.append({
                "start": start,
                "end": end,
                "text": caption_text,
            })

    return cues


def find_phrase_cues(
    subtitle_path: Path,
    phrase: str,
) -> list[tuple[float, float]]:
    """Devuelve todas las marcas temporales donde aparece una frase."""
    query = normalize_text(phrase)
    if not query:
        return []

    matches = []
    for cue in read_subtitle_cues(subtitle_path):
        if query in cue["text"]:
            matches.append((cue["start"], cue["end"]))

    return matches


def get_optional_int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def get_duration_range(item: dict) -> tuple[float, float]:
    try:
        minimum = float(item.get("duracion_min", 3))
        maximum = float(item.get("duracion_max", 6))
    except (ValueError, TypeError):
        raise ValueError("duracion_min y duracion_max deben ser números.")

    if not math.isfinite(minimum) or not math.isfinite(maximum):
        raise ValueError("Las duraciones deben ser valores finitos.")

    if minimum <= 0 or maximum <= 0 or maximum < minimum:
        raise ValueError(
            "La duración debe ser positiva y duracion_max no puede ser "
            "menor que duracion_min."
        )

    return minimum, maximum


def validate_config(config: dict) -> None:
    if not isinstance(config, dict):
        raise ValueError("El JSON debe contener un objeto principal.")

    metadata = config.get("datos_obra")
    requests_list = config.get("peticiones_escenas")

    if not isinstance(metadata, dict):
        raise ValueError("Falta el objeto 'datos_obra'.")

    if not isinstance(requests_list, list) or not requests_list:
        raise ValueError(
            "Falta 'peticiones_escenas' o no contiene ninguna petición."
        )

    for index, item in enumerate(requests_list, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"La petición {index} no es un objeto JSON.")

        resource_type = str(item.get("tipo_recurso", "video")).lower()
        if resource_type not in VALID_TYPES:
            raise ValueError(
                f"tipo_recurso no válido en la petición {index}: "
                f"{resource_type}. Valores permitidos: {sorted(VALID_TYPES)}"
            )

        if resource_type in {"video", "video_stock"}:
            get_duration_range(item)


def build_search_links(metadata: dict, item: dict) -> dict:
    """Crea enlaces de búsqueda para revisar escenas manualmente."""
    title = (
        metadata.get("titulo_busqueda_en")
        or metadata.get("titulo_obra")
        or ""
    )

    parts = [str(title)]

    season = get_optional_int(metadata.get("temporada"))
    episode = get_optional_int(item.get("capitulo"))

    if season is not None:
        parts.append(f"Season {season}")
    if episode is not None:
        parts.append(f"Episode {episode}")

    keywords = (
        item.get("keywords_busqueda_en")
        or item.get("descripcion_visual")
        or item.get("nombre_identificador")
        or ""
    )
    parts.append(str(keywords))
    parts.append("scene clip")

    query = " ".join(part for part in parts if part).strip()

    return {
        "youtube": (
            "https://www.youtube.com/results?search_query="
            + quote_plus(query)
        ),
        "bing": (
            "https://www.bing.com/videos/search?q="
            + quote_plus(query)
        ),
    }


def find_local_video(
    metadata: dict,
    item: dict,
    source_dir: Path,
) -> Path | None:
    """Busca el archivo local indicado o uno con nombre de episodio."""
    explicit = item.get("archivo_local")

    if explicit:
        candidate = Path(str(explicit)).expanduser()
        if not candidate.is_absolute():
            candidate = source_dir / candidate
        if candidate.is_file():
            return candidate.resolve()
        return None

    if not source_dir.is_dir():
        return None

    files = sorted(
        path for path in source_dir.iterdir()
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
    )

    season = get_optional_int(metadata.get("temporada"))
    episode = get_optional_int(item.get("capitulo"))

    if episode is not None:
        compact_names = []
        for path in files:
            compact = re.sub(r"[^A-Z0-9]", "", path.stem.upper())
            compact_names.append((path, compact))

        prefixes = []

        if season is not None:
            prefixes.extend([
                f"S{season:02d}E{episode:02d}",
                f"S{season}E{episode}",
            ])

        prefixes.extend([
            f"EPISODE{episode:02d}",
            f"EPISODE{episode}",
            f"EP{episode:02d}",
            f"EP{episode}",
            f"E{episode:02d}",
            f"E{episode}",
            f"CAPITULO{episode:02d}",
            f"CAPITULO{episode}",
        ])

        for prefix in prefixes:
            for path, compact in compact_names:
                if compact.startswith(prefix):
                    return path.resolve()

    # Para películas, se acepta un nombre habitual o un único archivo de vídeo.
    if episode is None:
        preferred = {"pelicula", "movie", "film"}
        for path in files:
            compact = re.sub(r"[^A-Z0-9]", "", path.stem.upper()).lower()
            if compact in preferred:
                return path.resolve()

        if len(files) == 1:
            return files[0].resolve()

    return None


def find_local_subtitle(
    video_path: Path,
    item: dict,
    source_dir: Path,
) -> Path | None:
    explicit = item.get("archivo_subtitulos")

    if explicit:
        candidate = Path(str(explicit)).expanduser()
        if not candidate.is_absolute():
            candidate = source_dir / candidate
        if candidate.is_file():
            return candidate.resolve()
        return None

    for extension in SUBTITLE_EXTENSIONS:
        candidate = video_path.with_suffix(extension)
        if candidate.is_file():
            return candidate.resolve()

    return None


def cut_video(
    input_path: Path,
    output_path: Path,
    start: float,
    minimum: float,
    maximum: float,
) -> tuple[float, float]:
    """Transcodifica un fragmento a MP4 dentro del rango solicitado."""
    ffmpeg, _ = require_ffmpeg()

    total_duration = probe_duration(input_path)
    target = (minimum + maximum) / 2.0

    if total_duration < minimum:
        raise RuntimeError(
            f"El vídeo dura {total_duration:.2f}s, menos que el mínimo "
            f"solicitado ({minimum:.2f}s)."
        )

    target = min(target, maximum, total_duration)
    start = max(0.0, float(start))
    start = min(start, max(0.0, total_duration - target))

    output_path.parent.mkdir(parents=True, exist_ok=True)

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-ss", f"{start:.3f}",
        "-i", str(input_path),
        "-t", f"{target:.3f}",
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "20",
        "-c:a", "aac",
        "-b:a", "160k",
        "-movflags", "+faststart",
        str(output_path),
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )

    if result.returncode != 0:
        detail = (result.stderr or "FFmpeg no pudo crear el clip.")[-1500:]
        raise RuntimeError(detail)

    if not output_path.is_file():
        raise RuntimeError("FFmpeg terminó sin producir el clip esperado.")

    return start, probe_duration(output_path)


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def download_file(
    session: requests.Session,
    url: str,
    destination: Path,
    max_bytes: int,
) -> Path:
    """Descarga un recurso HTTPS con límite de tamaño."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise RuntimeError("Se rechazó una descarga que no usa HTTPS.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    total = 0

    try:
        with session.get(
            url,
            stream=True,
            timeout=(20, 90),
        ) as response:
            response.raise_for_status()

            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > max_bytes:
                raise RuntimeError("El archivo supera el límite de tamaño.")

            with partial.open("wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue

                    total += len(chunk)
                    if total > max_bytes:
                        raise RuntimeError(
                            "La descarga supera el límite de tamaño."
                        )

                    output.write(chunk)

        if total == 0:
            raise RuntimeError("La descarga llegó vacía.")

        partial.replace(destination)
        return destination

    except Exception:
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def clean_html(value: str) -> str:
    value = html.unescape(str(value or ""))
    value = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"\s+", " ", value).strip()


def extmetadata_value(info: dict, key: str) -> str:
    value = info.get("extmetadata", {}).get(key, {})
    if isinstance(value, dict):
        return clean_html(value.get("value", ""))
    return clean_html(value)


def is_reusable_commons_license(label: str, license_url: str) -> bool:
    """
    Acepta únicamente licencias conocidas de atribución, atribución-compartir
    igual, CC0 o dominio público. Rechaza licencias NC y ND.
    """
    label_lower = str(label or "").lower()
    url_lower = str(license_url or "").lower()

    combined = f"{label_lower} {url_lower}"

    if any(term in combined for term in (
        "noncommercial",
        "non-commercial",
        "by-nc",
        "no derivatives",
        "by-nd",
    )):
        return False

    if "public domain" in label_lower:
        return True

    if "cc0" in label_lower:
        return True

    if "creativecommons.org/publicdomain/zero/" in url_lower:
        return True

    match = re.search(
        r"/licenses/(by-sa|by)/(\d+(?:\.\d+)*)",
        url_lower,
    )

    if match:
        version = match.group(2)
        return version in {"2.0", "2.5", "3.0", "4.0"}

    # Fallback para páginas que informan la licencia en el nombre.
    label_match = re.search(
        r"\bcc\s*(by-sa|by)[\s-]+(\d+(?:\.\d+)*)",
        label_lower,
    )

    if label_match:
        version = label_match.group(2)
        return version in {"2.0", "2.5", "3.0", "4.0"}

    return False


def commons_image(
    session: requests.Session,
    query: str,
    output_base: Path,
) -> dict:
    """Busca y descarga una imagen de Wikimedia Commons con licencia conocida."""
    api_url = "https://commons.wikimedia.org/w/api.php"

    params = {
        "action": "query",
        "generator": "search",
        "gsrsearch": f"filetype:bitmap {query}",
        "gsrnamespace": 6,
        "gsrlimit": 15,
        "prop": "imageinfo",
        "iiprop": "url|extmetadata",
        "iiurlwidth": 1600,
        "format": "json",
    }

    response = session.get(api_url, params=params, timeout=(15, 45))
    response.raise_for_status()
    data = response.json()

    pages = data.get("query", {}).get("pages", {})
    page_list = sorted(
        pages.values(),
        key=lambda page: int(page.get("index", 999999)),
    )

    for page in page_list:
        imageinfo_list = page.get("imageinfo", [])
        if not imageinfo_list:
            continue

        info = imageinfo_list[0]
        license_label = extmetadata_value(info, "LicenseShortName")
        license_url = extmetadata_value(info, "LicenseUrl")

        if not is_reusable_commons_license(license_label, license_url):
            continue

        media_url = info.get("thumburl") or info.get("url")
        if not media_url:
            continue

        suffix = Path(urlparse(media_url).path).suffix.lower()
        if suffix not in IMAGE_EXTENSIONS:
            continue

        output_path = output_base.with_suffix(suffix)
        download_file(
            session,
            media_url,
            output_path,
            MAX_IMAGE_BYTES,
        )

        artist = extmetadata_value(info, "Artist") or "Autor no indicado"
        description = extmetadata_value(info, "ImageDescription")
        source_page = (
            page.get("canonicalurl")
            or f"https://commons.wikimedia.org/?curid={page.get('pageid', '')}"
        )

        return {
            "path": output_path,
            "provider": "Wikimedia Commons",
            "creator": artist,
            "title": page.get("title", ""),
            "license": license_label,
            "license_url": license_url,
            "source_url": source_page,
            "description": description,
        }

    raise RuntimeError(
        "No se encontró una imagen con licencia reconocida "
        "de atribución, CC0 o dominio público."
    )


def pexels_request(
    session: requests.Session,
    api_key: str,
    endpoint: str,
    params: dict,
) -> dict:
    response = session.get(
        endpoint,
        headers={"Authorization": api_key},
        params=params,
        timeout=(15, 45),
    )
    response.raise_for_status()
    return response.json()


def pexels_photo(
    session: requests.Session,
    api_key: str,
    query: str,
    output_base: Path,
) -> dict:
    data = pexels_request(
        session,
        api_key,
        "https://api.pexels.com/v1/search",
        {"query": query, "per_page": 10, "page": 1},
    )

    photos = data.get("photos", [])
    if not photos:
        raise RuntimeError("Pexels no devolvió imágenes para esa búsqueda.")

    photo = photos[0]
    sources = photo.get("src", {})
    media_url = sources.get("large2x") or sources.get("large") or sources.get("original")

    if not media_url:
        raise RuntimeError("Pexels no devolvió un enlace de imagen.")

    suffix = Path(urlparse(media_url).path).suffix.lower()
    if suffix not in IMAGE_EXTENSIONS:
        suffix = ".jpg"

    output_path = output_base.with_suffix(suffix)
    download_file(
        session,
        media_url,
        output_path,
        MAX_IMAGE_BYTES,
    )

    return {
        "path": output_path,
        "provider": "Pexels",
        "creator": photo.get("photographer") or "Autor no indicado",
        "title": photo.get("alt") or "",
        "license": "Pexels License; comprobar términos actuales",
        "license_url": "https://www.pexels.com/license/",
        "source_url": photo.get("url") or "",
        "creator_url": photo.get("photographer_url") or "",
        "description": photo.get("alt") or "",
    }


def best_pexels_video_file(video: dict) -> dict | None:
    files = [
        item for item in video.get("video_files", [])
        if item.get("link")
        and (
            "mp4" in str(item.get("file_type", "")).lower()
            or ".mp4" in str(item.get("link", "")).lower()
        )
    ]

    if not files:
        return None

    def rank(item: dict):
        try:
            width = int(item.get("width") or 0)
        except (ValueError, TypeError):
            width = 0

        # Prefiere el archivo más grande hasta 1920 px.
        if width <= 1920:
            return (0, -width)
        return (1, width)

    return sorted(files, key=rank)[0]


def pexels_video(
    session: requests.Session,
    api_key: str,
    query: str,
    output_path: Path,
    minimum: float,
    maximum: float,
    temp_dir: Path,
) -> dict:
    data = pexels_request(
        session,
        api_key,
        "https://api.pexels.com/videos/search",
        {"query": query, "per_page": 15, "page": 1},
    )

    target = (minimum + maximum) / 2.0
    videos = data.get("videos", [])

    candidates = []
    for video in videos:
        try:
            duration = float(video.get("duration") or 0)
        except (ValueError, TypeError):
            duration = 0

        if duration >= minimum:
            candidates.append(video)

    candidates.sort(
        key=lambda video: abs(float(video.get("duration") or 0) - target)
    )

    selected_video = None
    selected_file = None

    for video in candidates:
        file_info = best_pexels_video_file(video)
        if file_info:
            selected_video = video
            selected_file = file_info
            break

    if not selected_video or not selected_file:
        raise RuntimeError(
            "Pexels no devolvió un vídeo compatible con la duración solicitada."
        )

    temp_dir.mkdir(parents=True, exist_ok=True)
    raw_path = temp_dir / f"pexels_{safe_name(query)}.mp4"

    download_file(
        session,
        selected_file["link"],
        raw_path,
        MAX_VIDEO_BYTES,
    )

    total_duration = probe_duration(raw_path)
    if total_duration < minimum:
        raise RuntimeError(
            f"El vídeo de Pexels dura {total_duration:.2f}s, "
            f"menos que el mínimo solicitado."
        )

    target_duration = min(target, maximum, total_duration)
    start = max(0.0, (total_duration - target_duration) / 2.0)

    cut_start, actual_duration = cut_video(
        raw_path,
        output_path,
        start,
        minimum,
        maximum,
    )

    return {
        "path": output_path,
        "provider": "Pexels",
        "creator": selected_video.get("user", {}).get("name", "Autor no indicado"),
        "title": selected_video.get("url", ""),
        "license": "Pexels License; comprobar términos actuales",
        "license_url": "https://www.pexels.com/license/",
        "source_url": selected_video.get("url") or "",
        "creator_url": selected_video.get("user", {}).get("url") or "",
        "start_seconds": cut_start,
        "duration_seconds": actual_duration,
    }


def credits_text(metadata: dict, records: list[dict]) -> str:
    title = metadata.get("titulo_obra") or "Obra no especificada"
    creator = metadata.get("autor_original") or "No indicado"
    studio = metadata.get("estudio_productora") or "No indicado"
    platform = metadata.get("plataforma_oficial") or "No indicada"
    official_url = metadata.get("enlace_oficial") or ""
    channel = metadata.get("nombre_canal") or "Canal por completar"

    lines = [
        "BORRADOR EDITABLE DE DESCRIPCIÓN Y CRÉDITOS",
        "=" * 64,
        "",
        f"Resumen, comentario o análisis sobre: {title}",
        "",
        "OBRA DE REFERENCIA",
        f"• Título: {title}",
        f"• Autoría indicada: {creator}",
        f"• Estudio/productora indicada: {studio}",
        f"• Plataforma oficial indicada: {platform}",
        f"• Enlace oficial: {official_url}",
        f"• Guion, narración y edición: {channel}",
        "",
        "RECURSOS DESCARGADOS Y FUENTES",
    ]

    downloadable_records = [
        record for record in records
        if record.get("asset_path")
    ]

    if not downloadable_records:
        lines.append("• No se descargaron recursos externos.")
    else:
        for record in downloadable_records:
            name = record.get("nombre_escena", "Recurso")
            provider = record.get("provider") or "Archivo local"
            creator_name = record.get("creator") or "Autor no indicado"
            license_name = record.get("license") or "No especificada"
            source_url = record.get("source_url") or ""
            license_url = record.get("license_url") or ""

            lines.append(f"• {name}")
            lines.append(f"  Fuente/proveedor: {provider}")
            lines.append(f"  Autor: {creator_name}")
            lines.append(f"  Licencia: {license_name}")
            if license_url:
                lines.append(f"  Enlace de licencia: {license_url}")
            if source_url:
                lines.append(f"  Fuente: {source_url}")

            if record.get("provider") == "Archivo local":
                lines.append(
                    "  Nota: comprobar y documentar los derechos del archivo de origen."
                )

    lines.extend([
        "",
        "PETICIONES QUE REQUIEREN REVISIÓN",
    ])

    pending = [
        record for record in records
        if not record.get("asset_path")
    ]

    if not pending:
        lines.append("• Ninguna.")
    else:
        for record in pending:
            lines.append(
                f"• {record.get('id')}: {record.get('nombre_escena')} "
                f"— {record.get('status')}: {record.get('detail')}"
            )
            for label, url in (record.get("search_links") or {}).items():
                lines.append(f"  Búsqueda {label}: {url}")

    lines.extend([
        "",
        "NOTA IMPORTANTE",
        "Este documento es una plantilla informativa de atribución. "
        "Los créditos no sustituyen una licencia, permiso o evaluación de "
        "las normas aplicables. Comprueba la licencia y la fuente de cada "
        "recurso, y las políticas de la plataforma antes de publicar.",
        "",
    ])

    return "\n".join(lines)


def write_outputs(
    project_dir: Path,
    metadata: dict,
    records: list[dict],
    log_messages: list[str],
) -> None:
    info_dir = project_dir / "04_Manifiesto_y_Creditos"
    info_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "proyecto": metadata,
        "recursos": records,
    }

    (info_dir / "manifiesto_para_editor_ia.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    (info_dir / "copy_creditos_editable.txt").write_text(
        credits_text(metadata, records),
        encoding="utf-8",
    )

    (info_dir / "reporte_proceso.txt").write_text(
        "\n".join(log_messages) + "\n",
        encoding="utf-8",
    )

    csv_path = info_dir / "creditos_recursos.csv"
    fields = [
        "id",
        "nombre_escena",
        "asset_path",
        "provider",
        "creator",
        "license",
        "license_url",
        "source_url",
        "status",
    ]

    with csv_path.open("w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fields)
        writer.writeheader()
        for record in records:
            writer.writerow({
                field: record.get(field, "")
                for field in fields
            })


def process_manifest(
    json_path: str | Path,
    source_dir: str | Path | None = None,
    output_dir: str | Path | None = None,
    pexels_key: str = "",
    log_callback=None,
) -> Path:
    """Procesa el JSON y devuelve la carpeta del proyecto de salida."""
    json_path = Path(json_path).expanduser().resolve()

    if not json_path.is_file():
        raise FileNotFoundError(f"No se encontró el JSON: {json_path}")

    with json_path.open("r", encoding="utf-8-sig") as file:
        config = json.load(file)

    validate_config(config)

    metadata = config["datos_obra"]
    requests_list = config["peticiones_escenas"]

    if source_dir:
        sources = Path(source_dir).expanduser().resolve()
    else:
        sources = json_path.parent / "fuentes_locales"

    if output_dir:
        output_root = Path(output_dir).expanduser().resolve()
    else:
        output_root = json_path.parent / "salida"

    title = metadata.get("titulo_obra") or "Proyecto_Resumen"
    season = get_optional_int(metadata.get("temporada"))
    project_label = title if season is None else f"{title}_T{season:02d}"

    project_dir = output_root / safe_name(project_label)
    video_dir = project_dir / "01_Clips_Locales"
    image_dir = project_dir / "02_Imagenes_Licencia_Abierta"
    stock_video_dir = project_dir / "03_Videos_Stock"
    temp_dir = project_dir / "_temporal"

    for directory in (video_dir, image_dir, stock_video_dir, temp_dir):
        directory.mkdir(parents=True, exist_ok=True)

    session = make_session()
    pexels_key = (pexels_key or os.environ.get("PEXELS_API_KEY", "")).strip()

    records = []
    log_messages = []

    def say(message: str):
        log_messages.append(str(message))
        emit(message, log_callback)

    say(f"Proyecto: {project_dir}")
    say(f"Carpeta de fuentes locales: {sources}")
    say(f"Petición de recursos: {len(requests_list)}")

    try:
        for index, item in enumerate(requests_list, start=1):
            resource_id = item.get("id", index)
            scene_name = str(
                item.get("nombre_identificador") or f"recurso_{index:02d}"
            )
            clean_scene = safe_name(scene_name)
            resource_type = str(item.get("tipo_recurso", "video")).lower()
            episode = get_optional_int(item.get("capitulo"))

            id_text = safe_name(str(resource_id), 20)
            episode_text = f"_E{episode:02d}" if episode is not None else ""
            base_name = f"{index:02d}_{id_text}{episode_text}_{clean_scene}"

            minimum = maximum = None
            if resource_type in {"video", "video_stock"}:
                minimum, maximum = get_duration_range(item)

            keywords = (
                item.get("keywords_busqueda_en")
                or item.get("descripcion_visual")
                or scene_name
            )
            search_links = build_search_links(metadata, item)

            record = {
                "id": resource_id,
                "nombre_escena": scene_name,
                "tipo_recurso": resource_type,
                "capitulo": episode,
                "duracion_min": minimum,
                "duracion_max": maximum,
                "contexto_narracion": item.get("contexto_narracion", ""),
                "descripcion_visual": item.get("descripcion_visual", ""),
                "asset_path": None,
                "duration_seconds": None,
                "provider": None,
                "creator": item.get("autor_recurso", ""),
                "license": item.get("licencia", ""),
                "license_url": item.get("enlace_licencia", ""),
                "source_url": item.get("fuente_url", ""),
                "status": "PENDIENTE",
                "detail": "",
                "search_links": search_links,
            }

            say(f"[{index}/{len(requests_list)}] {scene_name} ({resource_type})")

            try:
                if resource_type == "video":
                    local_video = find_local_video(
                        metadata,
                        item,
                        sources,
                    )

                    if not local_video:
                        record["status"] = "PENDIENTE_FUENTE_LOCAL"
                        record["detail"] = (
                            "No se encontró un archivo de vídeo local. "
                            "Se generaron enlaces de búsqueda para revisión."
                        )
                    else:
                        phrase = str(
                            item.get("frase_clave_dialogo") or ""
                        ).strip()

                        start_seconds = item.get("inicio_segundos")
                        selected_start = None

                        if phrase:
                            subtitle = find_local_subtitle(
                                local_video,
                                item,
                                sources,
                            )

                            if not subtitle:
                                record["status"] = "PENDIENTE_SUBTITULOS"
                                record["detail"] = (
                                    f"Se encontró el vídeo {local_video.name}, "
                                    "pero no un subtítulo .srt/.vtt. Añade uno "
                                    "o especifica inicio_segundos en el JSON."
                                )
                            else:
                                matches = find_phrase_cues(subtitle, phrase)

                                if not matches:
                                    record["status"] = "PENDIENTE_FRASE"
                                    record["detail"] = (
                                        f"No se encontró la frase en "
                                        f"{subtitle.name}."
                                    )
                                else:
                                    occurrence = get_optional_int(
                                        item.get("coincidencia")
                                    ) or 1

                                    if occurrence < 1 or occurrence > len(matches):
                                        record["status"] = "PENDIENTE_COINCIDENCIA"
                                        record["detail"] = (
                                            f"Hay {len(matches)} coincidencias "
                                            "pero el número indicado no es válido."
                                        )
                                    else:
                                        cue_start, cue_end = matches[occurrence - 1]
                                        target = (minimum + maximum) / 2
                                        cue_length = max(0.0, cue_end - cue_start)

                                        if cue_length < target:
                                            selected_start = cue_start - (
                                                target - cue_length
                                            ) / 2
                                        else:
                                            selected_start = cue_start

                                        record["detail"] = (
                                            f"Frase encontrada en "
                                            f"{subtitle.name}, coincidencia "
                                            f"{occurrence}/{len(matches)}."
                                        )

                        elif start_seconds is not None:
                            try:
                                selected_start = float(start_seconds)
                            except (ValueError, TypeError):
                                record["status"] = "ERROR"
                                record["detail"] = (
                                    "inicio_segundos debe ser un número."
                                )
                        else:
                            record["status"] = "PENDIENTE_MARCA_DE_TIEMPO"
                            record["detail"] = (
                                "Se encontró el archivo local, pero para "
                                "recortar hace falta frase_clave_dialogo con "
                                "subtítulos o inicio_segundos."
                            )

                        if selected_start is not None:
                            output_path = video_dir / f"{base_name}.mp4"
                            cut_start, actual_duration = cut_video(
                                local_video,
                                output_path,
                                selected_start,
                                minimum,
                                maximum,
                            )

                            record.update({
                                "asset_path": output_path.relative_to(
                                    project_dir
                                ).as_posix(),
                                "duration_seconds": round(actual_duration, 3),
                                "provider": "Archivo local",
                                "source_url": item.get("fuente_url", ""),
                                "status": "LISTO_REVISION_DERECHOS",
                                "detail": (
                                    f"Recorte de {local_video.name} desde "
                                    f"{cut_start:.2f}s. Verifica los derechos "
                                    "del archivo de origen."
                                ),
                            })

                elif resource_type == "imagen":
                    image_query = str(keywords).strip()
                    output_base = image_dir / base_name

                    result = commons_image(
                        session,
                        image_query,
                        output_base,
                    )

                    record.update({
                        "asset_path": result["path"].relative_to(
                            project_dir
                        ).as_posix(),
                        "provider": result["provider"],
                        "creator": result["creator"],
                        "license": result["license"],
                        "license_url": result["license_url"],
                        "source_url": result["source_url"],
                        "status": "LISTO_CON_LICENCIA_REGISTRADA",
                        "detail": (
                            f"Descargado de Wikimedia Commons. "
                            f"Licencia reconocida: {result['license']}."
                        ),
                    })

                elif resource_type == "imagen_stock":
                    if not pexels_key:
                        record["status"] = "FALTA_API_KEY_PEXELS"
                        record["detail"] = (
                            "Para buscar en Pexels, introduce una clave API "
                            "en la aplicación o configura PEXELS_API_KEY."
                        )
                        record["search_links"]["pexels"] = (
                            "https://www.pexels.com/search/"
                            + quote_plus(str(keywords))
                            + "/"
                        )
                    else:
                        result = pexels_photo(
                            session,
                            pexels_key,
                            str(keywords),
                            image_dir / base_name,
                        )

                        record.update({
                            "asset_path": result["path"].relative_to(
                                project_dir
                            ).as_posix(),
                            "provider": result["provider"],
                            "creator": result["creator"],
                            "license": result["license"],
                            "license_url": result["license_url"],
                            "source_url": result["source_url"],
                            "status": "LISTO_REVISION",
                            "detail": (
                                "Descargado de Pexels. Revisa que el recurso "
                                "sea el adecuado y verifica sus términos."
                            ),
                        })

                elif resource_type == "video_stock":
                    if not pexels_key:
                        record["status"] = "FALTA_API_KEY_PEXELS"
                        record["detail"] = (
                            "Para buscar en Pexels, introduce una clave API "
                            "en la aplicación o configura PEXELS_API_KEY."
                        )
                        record["search_links"]["pexels"] = (
                            "https://www.pexels.com/search/videos/"
                            + quote_plus(str(keywords))
                            + "/"
                        )
                    else:
                        output_path = stock_video_dir / f"{base_name}.mp4"
                        result = pexels_video(
                            session,
                            pexels_key,
                            str(keywords),
                            output_path,
                            minimum,
                            maximum,
                            temp_dir,
                        )

                        record.update({
                            "asset_path": result["path"].relative_to(
                                project_dir
                            ).as_posix(),
                            "duration_seconds": round(
                                result["duration_seconds"], 3
                            ),
                            "provider": result["provider"],
                            "creator": result["creator"],
                            "license": result["license"],
                            "license_url": result["license_url"],
                            "source_url": result["source_url"],
                            "status": "LISTO_REVISION",
                            "detail": (
                                "Vídeo de stock descargado y recortado. "
                                "Revisa que represente lo solicitado."
                            ),
                        })

            except Exception as error:
                record["status"] = "ERROR"
                record["detail"] = str(error)

            records.append(record)
            say(f"    {record['status']}: {record['detail']}")

    finally:
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass

    write_outputs(
        project_dir,
        metadata,
        records,
        log_messages,
    )

    say(f"Finalizado. Salida: {project_dir}")
    return project_dir
