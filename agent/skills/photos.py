import base64
import json
from contextvars import ContextVar

import httpx
from agents import function_tool
from openai import AsyncOpenAI

URL_FILE = "/var/run/nimoos/photos.url"
_TIMEOUT = 30.0

# Per-run user JWT, set by agent.py before each run (same pattern as the other
# per-skill ContextVars). Album endpoints on the Photos service require a user
# JWT; search/smart merely tolerates its absence for localhost callers.
AUTH_HEADER_VAR: ContextVar[str] = ContextVar("photos_auth_header", default="")

# Per-run vision sub-call config, set by agent.py before each run:
# {"ok": bool, "base_url": str, "api_key": str, "model": str}. look_at_photos
# issues a one-shot vision request with these credentials — tool-output
# images are dropped by the chat-completions adapter, so vision happens
# out-of-band and only TEXT descriptions enter the conversation.
VISION_CFG_VAR: ContextVar[dict] = ContextVar("photos_vision_cfg", default={})


def _auth_headers() -> dict:
    auth = AUTH_HEADER_VAR.get()
    return {"Authorization": auth} if auth else {}


def _photos_base_url() -> str | None:
    try:
        return open(URL_FILE).read().strip()
    except OSError:
        return None


@function_tool
async def search_photos(query: str, year: int = 0, limit: int = 20) -> str:
    """Search photos by semantic description using CLIP AI.

    Args:
        query: MUST be ENGLISH ONLY — never pass Chinese or any other
               language; non-English queries are rejected by this tool.
               Translate the user's request into ONE short English
               description of what the photo looks like ("sunset at
               beach", "computer store receipt") before calling.
        year:  Optional year filter (e.g. 2025). 0 means no filter.
        limit: Max results to return (1-50).
    """
    base_url = _photos_base_url()
    if not base_url:
        return json.dumps({"error": "Photos service is unavailable."})

    if limit < 1 or limit > 50:
        limit = 20

    # Hard guard: the caller (LLM) must translate to English first.
    if any(ord(ch) > 127 for ch in query):
        return json.dumps({
            "error": "query must be English only. Translate the request "
                     "into a short English description and call again.",
        })

    payload: dict = {"query": query, "limit": limit}
    if year > 0:
        payload["filters"] = {"year": year}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{base_url}/v1/photos/search/smart",
                json=payload,
                headers=_auth_headers(),
            )
        if resp.status_code != 200:
            return json.dumps({"error": f"HTTP {resp.status_code}"})

        raw = resp.json()
        if not raw:
            return json.dumps({"query": query, "count": 0, "results": []})

        results = [
            {
                "id": item.get("id", ""),
                "name": item.get("originalName", item.get("id", "unknown")),
                "takenAt": (item.get("takenAt") or "")[:10],
            }
            for item in raw
        ]
        return json.dumps({"query": query, "count": len(results), "results": results})

    except Exception as e:
        return json.dumps({"error": str(e)})


@function_tool
async def create_album(name: str) -> str:
    """Create a new photo album.

    Args:
        name: The album name, e.g. "Summer 2024".
    """
    base_url = _photos_base_url()
    if not base_url:
        return "Photos service is unavailable."
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{base_url}/v1/photos/albums",
                json={"name": name},
                headers=_auth_headers(),
            )
        if resp.status_code not in (200, 201):
            return f"Failed to create album: HTTP {resp.status_code}"
        data = resp.json()
        return f"Album '{name}' created (id: {data.get('id', '?')})"
    except Exception as e:
        return f"Photos service error: {e}"


@function_tool
async def add_to_album(album_id: str, asset_ids: list[str]) -> str:
    """Add one or more photos to an existing album.

    Args:
        album_id:  The album ID (from create_album).
        asset_ids: List of asset IDs to add.
    """
    base_url = _photos_base_url()
    if not base_url:
        return "Photos service is unavailable."
    success = 0
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            for asset_id in asset_ids:
                resp = await client.post(
                    f"{base_url}/v1/photos/albums/{album_id}/assets",
                    json={"assetId": asset_id},
                    headers=_auth_headers(),
                )
                if resp.status_code == 200:
                    success += 1
        return f"Added {success}/{len(asset_ids)} photo(s) to album {album_id}"
    except Exception as e:
        return f"Photos service error: {e}"


@function_tool
async def list_albums() -> str:
    """List the user's photo albums.

    Returns JSON: {count, albums: [{id, name, assetCount, dateStart,
    dateEnd}]} (dates are YYYY-MM-DD or empty). Use it to find albums
    that need renaming or organizing.
    """
    base_url = _photos_base_url()
    if not base_url:
        return json.dumps({"error": "Photos service is unavailable."})
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{base_url}/v1/photos/albums", headers=_auth_headers())
        if resp.status_code != 200:
            return json.dumps({"error": f"HTTP {resp.status_code}"})
        raw = resp.json() or []
        albums = [
            {
                "id": a.get("id", ""),
                "name": a.get("name", ""),
                "assetCount": a.get("assetCount", 0),
                "dateStart": (a.get("dateStart") or "")[:10],
                "dateEnd": (a.get("dateEnd") or "")[:10],
            }
            for a in raw[:100]
        ]
        return json.dumps({"count": len(albums), "albums": albums})
    except Exception as e:
        return json.dumps({"error": str(e)})


@function_tool
async def get_album_summary(album_id: str) -> str:
    """Get the naming signals of one album: photo/video counts, taken-at
    date range, top places, top named persons, OCR text samples, filename
    samples and coverCandidates (time-spread photo IDs for look_at_photos).

    Returns JSON with keys: assetCount, photoCount, videoCount, dateStart,
    dateEnd, topPlaces, topPersons, ocrSamples, sampleFilenames,
    coverCandidates.

    Args:
        album_id: The album ID (from list_albums).
    """
    base_url = _photos_base_url()
    if not base_url:
        return json.dumps({"error": "Photos service is unavailable."})
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{base_url}/v1/photos/albums/{album_id}/summary",
                headers=_auth_headers())
        if resp.status_code == 404:
            return json.dumps({"error": "album not found"})
        if resp.status_code != 200:
            return json.dumps({"error": f"HTTP {resp.status_code}"})
        return json.dumps(resp.json())
    except Exception as e:
        return json.dumps({"error": str(e)})


@function_tool
async def rename_album(album_id: str, new_name: str) -> str:
    """Rename an existing album. Albums are database records; renaming
    never touches the underlying files.

    Args:
        album_id: The album ID (from list_albums).
        new_name: The new album name.
    """
    base_url = _photos_base_url()
    if not base_url:
        return "Photos service is unavailable."
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.patch(
                f"{base_url}/v1/photos/albums/{album_id}",
                json={"name": new_name},
                headers=_auth_headers())
        if resp.status_code == 409:
            return (f"Failed: an album named '{new_name}' already exists. "
                    "Pick a different name.")
        if resp.status_code == 404:
            return "Failed: album not found."
        if resp.status_code != 200:
            return f"Failed to rename album: HTTP {resp.status_code}"
        return f"Album {album_id} renamed to '{new_name}'"
    except Exception as e:
        return f"Photos service error: {e}"


@function_tool
async def look_at_photos(asset_ids: list[str]) -> str:
    """Look at up to 3 photos and return one-line visual descriptions.

    Expensive fallback — use ONLY when get_album_summary returns no usable
    signal (no places, no named persons, no OCR text). Pass the summary's
    coverCandidates IDs. Call at most once per album.

    Args:
        asset_ids: 1-3 photo asset IDs (extra IDs are ignored).
    """
    cfg = VISION_CFG_VAR.get()
    if not cfg.get("ok"):
        return ("The current model does not support vision. "
                "Name the album from metadata instead.")
    base_url = _photos_base_url()
    if not base_url:
        return "Photos service is unavailable."

    ids = list(asset_ids)[:3]
    blocks = [{
        "type": "text",
        "text": ("Briefly describe each photo in one line (subject, scene, "
                 "activity), to help name the photo album they belong to."),
    }]
    failed = 0
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            for aid in ids:
                resp = await client.get(
                    f"{base_url}/v1/photos/assets/{aid}/thumbnail",
                    params={"size": "small"},
                    headers=_auth_headers())
                if resp.status_code != 200:
                    failed += 1
                    continue
                mime = resp.headers.get("content-type", "image/jpeg")
                if not mime.startswith("image/"):
                    failed += 1
                    continue
                b64 = base64.b64encode(resp.content).decode("ascii")
                blocks.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                })
    except Exception as e:
        return f"Photos service error: {e}"
    if failed == len(ids):
        return f"Could not load any of the {len(ids)} thumbnails."

    try:
        oai = AsyncOpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"])
        completion = await oai.chat.completions.create(
            model=cfg["model"],
            messages=[{"role": "user", "content": blocks}],
        )
        desc = (completion.choices[0].message.content or "").strip()
    except Exception as e:
        return f"Vision call failed ({e}); name the album from metadata instead."
    note = f" ({failed} of {len(ids)} thumbnails unavailable)" if failed else ""
    return f"Photo descriptions{note}:\n{desc}"


ALL_TOOLS = [search_photos, create_album, add_to_album,
             list_albums, get_album_summary, rename_album, look_at_photos]
