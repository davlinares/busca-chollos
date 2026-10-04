"""Scraper de Milanuncios para Wallapop Chollos."""
import json
import time
import requests

BASE_URL = "https://www.milanuncios.com"
RESULTS_PER_PAGE = 40

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}


def _parse_page(html):
    marker = "window.__INITIAL_PROPS__ = JSON.parse("
    idx = html.find(marker)
    if idx == -1:
        return None
    start = idx + len(marker)
    try:
        outer_str, _ = json.JSONDecoder().raw_decode(html[start:])
        return json.loads(outer_str)
    except (ValueError, json.JSONDecodeError):
        return None


def _normalize(ad):
    price = ((ad.get("price") or {}).get("cashPrice") or {}).get("value") or 0
    images = ad.get("images") or []
    image_url = ("https://" + images[0] + "?rule=hw396_70") if images else ""
    url_path = ad.get("url", "")
    return {
        "id": f"ma_{ad.get('id', '')}",
        "title": ad.get("title", ""),
        "description": ad.get("description", ""),
        "price": float(price),
        "url": BASE_URL + url_path if url_path else "",
        "image": image_url,
        "city": ((ad.get("location") or {}).get("city") or {}).get("name", ""),
        "reserved": 1 if ad.get("isReserved") else 0,
        "source": "milanuncios",
        "condition": "new" if ad.get("isNew") else "used",
        "created_at": None,
    }


def fetch(keywords, max_pages=2):
    """Retorna lista de dicts normalizados con anuncios de Milanuncios."""
    results = []
    for page in range(max_pages):
        desde = page * RESULTS_PER_PAGE
        url = f"{BASE_URL}/anuncios/?texto={requests.utils.quote(keywords)}&orden=fecha&desde={desde}"
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=20)
            if resp.status_code != 200:
                break
            data = _parse_page(resp.text)
            if not data:
                break
            ads = (data.get("adListPagination") or {}).get("adList", {}).get("ads") or []
            if not ads:
                break
            results.extend(_normalize(ad) for ad in ads)
            pg = (data.get("adListPagination") or {}).get("pagination") or {}
            if page + 1 >= (pg.get("totalPages") or 1):
                break
            time.sleep(3)
        except Exception:
            break
    return results
