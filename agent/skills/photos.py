import httpx
from agents import function_tool

URL_FILE = "/var/run/nimoos/photos.url"
_TIMEOUT = 30.0


def _photos_base_url() -> str | None:
    try:
        return open(URL_FILE).read().strip()
    except OSError:
        return None


@function_tool
async def search_photos(query: str, year: int = 0, limit: int = 20) -> str:
    """Search photos by semantic description using CLIP AI.

    Args:
        query: Natural language description, e.g. "sunset at beach", "birthday party".
        year:  Optional year filter (e.g. 2025). 0 means no filter.
        limit: Max results to return (1-50).
    """
    base_url = _photos_base_url()
    if not base_url:
        return "Photos service is unavailable (photos.url not found)."

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
            )
        if resp.status_code != 200:
            return f"Photos search error: HTTP {resp.status_code}"

        results = resp.json()
        if not results:
            return "No matching photos found."

        lines = [f"Found {len(results)} photo(s) matching '{query}':\n"]
        for item in results:
            name = item.get("originalName", item.get("id", "unknown"))
            taken = item.get("takenAt", "")
            asset_id = item.get("id", "")
            thumb_url = f"{base_url}/v1/photos/assets/{asset_id}/thumbnail" if asset_id else ""
            lines.append(f"- {name} ({taken[:10] if taken else 'no date'}) thumbnail: {thumb_url}")
        return "\n".join(lines)

    except Exception as e:
        return f"Photos service error: {e}"


ALL_TOOLS = [search_photos]
