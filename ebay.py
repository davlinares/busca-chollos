"""Cliente de la eBay Browse API para Wallapop Chollos."""
import base64
import time
import requests

_OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
_MARKETPLACE = "EBAY_ES"

_token_cache = {"token": None, "expires": 0}


def _get_token(app_id, cert_id):
    if _token_cache["token"] and time.time() < _token_cache["expires"] - 60:
        return _token_cache["token"]
    creds = base64.b64encode(f"{app_id}:{cert_id}".encode()).decode()
    resp = requests.post(
        _OAUTH_URL,
        headers={"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"},
        data="grant_type=client_credentials&scope=https%3A%2F%2Fapi.ebay.com%2Foauth%2Fapi_scope",
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["expires"] = time.time() + int(data.get("expires_in", 7200))
    return _token_cache["token"]


def _normalize(item):
    condition_raw = (item.get("condition") or "").lower()
    condition = "new" if "new" in condition_raw else "used"
    price_info = item.get("price") or {}
    price = float(price_info.get("value", 0) or 0)
    image = ((item.get("image") or {}).get("imageUrl") or "").replace("s-l225", "s-l500")
    item_id = str(item.get("itemId", "")).replace("|", "_")
    return {
        "id": f"eb_{item_id}",
        "title": item.get("title", ""),
        "description": item.get("shortDescription", ""),
        "price": price,
        "url": item.get("itemWebUrl", ""),
        "image": image,
        "city": ((item.get("itemLocation") or {}).get("city") or ""),
        "reserved": 0,
        "source": "ebay",
        "condition": condition,
        "created_at": None,
    }


def fetch(keywords, app_id, cert_id, max_items=80):
    """Retorna lista de dicts normalizados con artículos de eBay España."""
    token = _get_token(app_id, cert_id)
    results = []
    offset = 0
    limit = 50
    while offset < max_items:
        resp = requests.get(
            _SEARCH_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": _MARKETPLACE,
            },
            params={
                "q": keywords,
                "limit": min(limit, max_items - offset),
                "offset": offset,
                "filter": "currency:EUR",
            },
            timeout=20,
        )
        if resp.status_code != 200:
            break
        data = resp.json()
        items = data.get("itemSummaries") or []
        if not items:
            break
        results.extend(_normalize(it) for it in items)
        total = data.get("total") or 0
        if len(results) >= total:
            break
        offset += limit
        time.sleep(1)
    return results
