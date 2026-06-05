import json
from contextvars import ContextVar

import httpx
from agents import function_tool

URL_FILE = "/var/run/nimoos/photos.url"
_TIMEOUT = 30.0

# Per-run user JWT, set by agent.py before each run (same pattern as the other
# per-skill ContextVars). Album endpoints on the Photos service require a user
# JWT; search/smart merely tolerates its absence for localhost callers.
AUTH_HEADER_VAR: ContextVar[str] = ContextVar("photos_auth_header", default="")


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
        query: The search runs TWO channels at once:
               1. CLIP visual semantics — matches what the photo LOOKS like.
                  Use a short ENGLISH description ("sunset at beach").
               2. OCR exact text — the whole query is substring-matched
                  against text recognized INSIDE photos; hits rank on top.
                  Use a SHORT keyword in the language actually printed on
                  the photo (e.g. Chinese receipts → “电脑” / store name /
                  “发票”), never a long sentence.
               Visual subjects → one English query. Text-bearing targets
               (receipts, documents, screenshots) → query with the words
               likely printed on them, in their original language.
        year:  Optional year filter (e.g. 2025). 0 means no filter.
        limit: Max results to return (1-50).
    """
    base_url = _photos_base_url()
    if not base_url:
        return json.dumps({"error": "Photos service is unavailable."})

    if limit < 1 or limit > 50:
        limit = 20

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


ALL_TOOLS = [search_photos, create_album, add_to_album]
