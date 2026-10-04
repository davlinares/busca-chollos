"""Precio nuevo en Amazon.es vía SerpAPI (buscador oficial, sin raspar Amazon).
El plan gratuito da 250 búsquedas/mes compartidas con el SEO Checker: se busca solo lo que
tiene un candidato a chollo, se guarda 30 días y hay un tope mensual propio."""
import json
import logging
import time

import requests

import core
import llm

log = logging.getLogger("chollos.amazon")
TTL = 30 * 86400

DEFAULTS = {
    "amazon_enabled": "1",
    "serpapi_key": "",
    "amazon_monthly_limit": "150",
    "serp_month": "",
    "serp_used": "0",
}
COLUMNS = {"amazon_price": "REAL", "amazon_title": "TEXT", "amazon_url": "TEXT", "amazon_ts": "INTEGER"}


def init_db():
    with core.db() as con:
        for k, v in DEFAULTS.items():
            con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
        for table in ("products", "searches"):
            have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
            for col, decl in COLUMNS.items():
                if col not in have:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def used_this_month(settings):
    return int(settings["serp_used"]) if settings["serp_month"] == time.strftime("%Y-%m") else 0


def available(settings):
    return (settings.get("amazon_enabled") == "1" and bool(settings.get("serpapi_key"))
            and used_this_month(settings) < int(settings["amazon_monthly_limit"]))


def _count():
    s = core.get_settings()
    core.set_settings({"serp_month": time.strftime("%Y-%m"), "serp_used": str(used_this_month(s) + 1)})


def search(settings, query):
    r = requests.get("https://serpapi.com/search.json", timeout=60, params={
        "engine": "amazon", "amazon_domain": "amazon.es", "k": query, "api_key": settings["serpapi_key"]})
    _count()
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(data["error"])
    return [{"title": o.get("title", ""), "price": o.get("extracted_price"),
             "url": o.get("link_clean") or o.get("link")}
            for o in data.get("organic_results") or [] if o.get("extracted_price")][:12]


PICK_SYSTEM = """Te doy un producto y resultados de Amazon.es. Elige el resultado que sea EXACTAMENTE ese producto, nuevo,
tal cual (sin discos/RAM/accesorios extra incluidos, sin packs de varias unidades, no un accesorio para él,
no un modelo distinto). Si hay varios válidos, el de precio más bajo. Si ninguno lo es, null.
Cómo leer el producto: el modelo y la generación deben coincidir exactamente (M1 no es M2 ni M6; DS923+ no es DS920+).
"hdd" = disco duro interno de sobremesa/NAS (3,5"); solo si pone "hdd externo" vale uno externo USB.
"ram ddrX" = módulos de sobremesa (DIMM/UDIMM); solo si pone "sodimm" valen de portátil. La capacidad es el total.
"ssd sata"/"ssd nvme" = SSD interno de ese tipo y capacidad.
Responde SOLO con JSON: {"i": número o null}"""


def pick(settings, name, results):
    if llm.available(settings):
        lines = "\n".join(f"{n}. {r['title'][:150]} — {r['price']} €" for n, r in enumerate(results))
        try:
            # Modelo "listo": se usa pocas veces al mes y el rápido confundía modelos (M1 → M6)
            i = llm.ask_json(settings, settings["llm_model_smart"], PICK_SYSTEM, f"Producto: {name}\n\n{lines}", 50).get("i")
        except ValueError:
            return None  # respondió sin JSON (p. ej. "null"): ninguno coincide
        return results[int(i)] if isinstance(i, int) and 0 <= i < len(results) else None
    # Sin IA: el más barato cuyo título contenga todas las palabras y no parezca accesorio
    acc = core.parse_words(settings["accessory_words"])
    ok = [r for r in results if core.title_matches(r["title"], name) and not core.accessory_reason(r["title"], name, acc)]
    return min(ok, key=lambda r: r["price"]) if ok else None


def lookup(settings, name, query=None):
    """(precio, título, url) del producto nuevo en Amazon, o None si no está."""
    results = search(settings, query or name)
    hit = pick(settings, name, results) if results else None
    log.info("[amazon] %s: %s", name, f"{hit['price']} € — {hit['title'][:60]}" if hit else "no encontrado")
    return hit


def refresh(con_table, key_col, key_val, name, settings, query=None):
    hit = lookup(settings, name, query)
    with core.db() as con:
        con.execute(f"UPDATE {con_table} SET amazon_price=?, amazon_title=?, amazon_url=?, amazon_ts=? WHERE {key_col}=?",
                    (hit["price"] if hit else None, hit["title"] if hit else None, hit["url"] if hit else None,
                     int(time.time()), key_val))
    return hit


def stale(row):
    return not row["amazon_ts"] or row["amazon_ts"] < time.time() - TTL
