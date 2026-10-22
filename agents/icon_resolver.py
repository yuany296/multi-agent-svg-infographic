"""图标解析器：远程图标优先，通用语义由模型自由绘制。"""

import json
import os
from pathlib import Path
import re
from typing import Any

import httpx


ICON_ROOT = Path(__file__).resolve().parents[1] / "assets" / "icons"
INDEX_PATH = ICON_ROOT / "icon-index.json"
CACHE_DIR = ICON_ROOT / "cache"
ICONIFY_API_BASE = os.getenv("ICONIFY_API_BASE", "https://api.iconify.design").rstrip("/")
SIMPLE_ICONS_CDN_BASE = os.getenv(
    "SIMPLE_ICONS_CDN_BASE", "https://cdn.simpleicons.org"
).rstrip("/")
BRAND_COLLECTIONS = ("simple-icons", "logos", "fa6-brands")
GENERAL_COLLECTIONS = ("lucide", "tabler", "ph", "material-symbols")
KNOWN_LICENSES = {
    "simple-icons": "CC0-1.0",
    "lucide": "ISC",
    "tabler": "MIT",
    "ph": "MIT",
    "material-symbols": "Apache-2.0",
}


def _network_enabled() -> bool:
    value = os.getenv("ICONIFY_SEARCH_ENABLED", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _normalize_query(value: Any) -> str:
    text = str(value or "").strip().lower().replace("_", "-")
    text = re.sub(r"\s+", "-", text)
    return re.sub(r"[^\w\-\u4e00-\u9fff]", "", text)


def _safe_part(value: Any) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value))
    return normalized.strip("-") or "0"


def _load_index() -> dict:
    try:
        return json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"aliases": {}, "icons": {}}


def _clean_body(body: str) -> str | None:
    lowered = body.lower()
    if "<script" in lowered or "javascript:" in lowered:
        return None
    if re.search(r'(?:href|src)\s*=\s*["\']https?://', body, re.IGNORECASE):
        return None
    return body.strip() or None


def _cache_path(collection: str, name: str) -> Path:
    return CACHE_DIR / f"{_safe_part(collection)}--{_safe_part(name)}.json"


def _query_cache_path(kind: str, query: str) -> Path:
    return CACHE_DIR / f"query--{_safe_part(kind)}--{_safe_part(query)}.json"


def _read_cache(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not data.get("body") or not data.get("viewBox"):
        return None
    data["source"] = "cache"
    return data


def _is_remote_backed(icon: dict | None) -> bool:
    """确认缓存或解析结果仍可追溯到外部图标来源。"""
    if not isinstance(icon, dict):
        return False
    source_url = str(icon.get("source_url", "")).strip().lower()
    return source_url.startswith(("https://", "http://"))


def _write_cache(path: Path, data: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {key: value for key, value in data.items() if key not in {"id", "stage_id", "visual_index"}}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        # 缓存失败不应影响主生成流程。
        return


async def _fetch_simple_icon_data(
    client: httpx.AsyncClient,
    name: str,
) -> dict | None:
    """从 Simple Icons 官方彩色 CDN 获取带品牌色的单色 Logo。"""
    if not _network_enabled():
        return None
    try:
        response = await client.get(f"{SIMPLE_ICONS_CDN_BASE}/{name}")
        response.raise_for_status()
        svg = response.text
    except (httpx.HTTPError, ValueError):
        return None
    viewbox_match = re.search(r'viewBox=["\']([^"\']+)["\']', svg, re.IGNORECASE)
    body_match = re.search(r"<svg\b([^>]*)>([\s\S]*?)</svg>", svg, re.IGNORECASE)
    if not body_match:
        return None
    root_attributes, raw_body = body_match.groups()
    body = _clean_body(re.sub(r"<title\b[^>]*>[\s\S]*?</title>", "", raw_body, flags=re.IGNORECASE))
    if not body:
        return None
    fill_match = re.search(r'\bfill=["\'](#[0-9a-fA-F]{3,8})["\']', root_attributes)
    color = fill_match.group(1).upper() if fill_match else ""
    if color:
        body = f'<g fill="{color}">{body}</g>'
    return {
        "collection": "simple-icons",
        "name": name,
        "viewBox": viewbox_match.group(1) if viewbox_match else "0 0 24 24",
        "body": body,
        "color": color,
        "palette_mode": "monochrome",
        "render_mode": "symbol",
        "license": "CC0-1.0",
        "source_url": f"{SIMPLE_ICONS_CDN_BASE}/{name}",
        "source": "remote",
    }


async def _fetch_icon_data(
    client: httpx.AsyncClient,
    collection: str,
    name: str,
    license_name: str | None = None,
    *,
    require_remote: bool = False,
) -> dict | None:
    cached = _read_cache(_cache_path(collection, name))
    if (
        cached
        and (collection != "simple-icons" or cached.get("color"))
        and (not require_remote or _is_remote_backed(cached))
    ):
        return cached
    if not _network_enabled():
        return None
    if collection == "simple-icons":
        result = await _fetch_simple_icon_data(client, name)
        if result:
            _write_cache(_cache_path(collection, name), result)
        return result
    try:
        response = await client.get(
            f"{ICONIFY_API_BASE}/{collection}.json",
            params={"icons": name},
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    icon = payload.get("icons", {}).get(name)
    if not isinstance(icon, dict):
        return None
    body = _clean_body(str(icon.get("body", "")))
    if not body:
        return None
    width = icon.get("width", payload.get("width", 24))
    height = icon.get("height", payload.get("height", 24))
    result = {
        "collection": collection,
        "name": name,
        "viewBox": f"0 0 {width} {height}",
        "body": body,
        "license": license_name or KNOWN_LICENSES.get(collection, "unknown"),
        "source_url": f"{ICONIFY_API_BASE}/{collection}/{name}.svg",
        "source": "remote",
        "palette_mode": "monochrome" if "currentColor" in body else "hardcoded",
        "render_mode": "symbol",
    }
    _write_cache(_cache_path(collection, name), result)
    return result


async def _search_remote(
    client: httpx.AsyncClient,
    query: str,
    kind: str,
) -> dict | None:
    query_cache = _read_cache(_query_cache_path(kind, query))
    if query_cache and (kind != "brand" or _is_remote_backed(query_cache)):
        return query_cache
    if not _network_enabled():
        return None
    collections = BRAND_COLLECTIONS if kind == "brand" else GENERAL_COLLECTIONS
    try:
        response = await client.get(
            f"{ICONIFY_API_BASE}/search",
            params={"query": query, "prefixes": ",".join(collections), "limit": 64},
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    exact: tuple[str, str] | None = None
    for candidate in payload.get("icons", []):
        if ":" not in candidate:
            continue
        collection, name = candidate.split(":", 1)
        if collection in collections and _normalize_query(name) == query:
            exact = (collection, name)
            break
    if exact is None:
        return None
    collection, name = exact
    license_info = (payload.get("collections") or {}).get(collection, {}).get("license", {})
    license_name = license_info.get("spdx") or license_info.get("title")
    result = await _fetch_icon_data(
        client,
        collection,
        name,
        license_name,
        require_remote=kind == "brand",
    )
    if result:
        _write_cache(_query_cache_path(kind, query), result)
    return result


def _normalize_visual(visual: Any) -> tuple[str, str, str]:
    if isinstance(visual, dict):
        label = str(visual.get("label") or visual.get("icon_query") or "图标")
        query = _normalize_query(visual.get("icon_query") or label)
        kind = str(visual.get("kind", "general")).lower()
    else:
        label = str(visual)
        query = _normalize_query(label)
        kind = "general"
    return label, query, "brand" if kind == "brand" else "general"


async def _resolve_one(
    client: httpx.AsyncClient,
    *,
    query: str,
    kind: str,
    index: dict,
) -> dict:
    aliases = index.get("aliases", {})
    target = aliases.get(query)
    if isinstance(target, str) and ":" in target and not target.startswith("local:"):
        collection, name = target.split(":", 1)
        if kind == "brand" and collection not in BRAND_COLLECTIONS:
            direct = None
        else:
            direct = await _fetch_icon_data(
                client,
                collection,
                name,
                require_remote=kind == "brand",
            )
        if direct:
            return direct
    searched = await _search_remote(client, query, kind)
    if searched:
        return searched
    return {
        "collection": "brand-badge" if kind == "brand" else "llm-generated",
        "name": "unresolved",
        "source_url": "",
        "source": "fallback",
        "render_mode": "brand_badge" if kind == "brand" else "llm_general",
        "fallback_reason": "未找到精确的外部图标",
    }


async def resolve_icons(
    content: list[dict],
    client: httpx.AsyncClient | None = None,
) -> tuple[list[dict], dict[str, int]]:
    """解析所有视觉元素并返回完整图标数据与来源统计。"""
    index = _load_index()
    diagnostics = {"local": 0, "cache": 0, "remote": 0, "fallback": 0}
    resolved: list[dict] = []
    memory: dict[tuple[str, str], dict] = {}
    owns_client = client is None
    active_client = client or httpx.AsyncClient(timeout=3.0)
    try:
        for stage_index, stage in enumerate(content):
            stage_id = stage.get("id", stage_index)
            visuals = stage.get("visuals", [])
            if not isinstance(visuals, list):
                visuals = []
            for visual_index, visual in enumerate(visuals):
                label, query, kind = _normalize_visual(visual)
                key = (query, kind)
                if key in memory:
                    base = dict(memory[key])
                    base["source"] = "cache"
                else:
                    base = await _resolve_one(
                        active_client,
                        query=query,
                        kind=kind,
                        index=index,
                    )
                    memory[key] = dict(base)
                if base.get("render_mode") != "brand_badge":
                    base.setdefault(
                        "palette_mode",
                        "monochrome" if "currentColor" in str(base.get("body", "")) else "hardcoded",
                    )
                base.setdefault(
                    "render_mode",
                    "brand_badge" if kind == "brand" and base.get("source") == "fallback" else "symbol",
                )
                icon = dict(base)
                icon.update(
                    {
                        "id": f"icon-s{stage_index}-v{visual_index}",
                        "label": label,
                        "query": query,
                        "kind": kind,
                        "stage_id": stage_id,
                        "visual_index": visual_index,
                    }
                )
                source = str(icon.get("source", "fallback"))
                diagnostics[source if source in diagnostics else "fallback"] += 1
                resolved.append(icon)
    finally:
        if owns_client:
            await active_client.aclose()
    return resolved, diagnostics


def icon_report(icons: list[dict]) -> list[dict]:
    """移除 SVG body，生成可传给 LLM 和 UI 的紧凑摘要。"""
    hidden = {"body"}
    return [
        {key: value for key, value in icon.items() if key not in hidden}
        for icon in icons
    ]
