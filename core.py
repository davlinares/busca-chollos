"""Wallapop chollos: scraper, detección de gangas, histórico y envío de emails."""
import json
import logging
import os
import random
import re
import smtplib
import sqlite3
import statistics
import threading
import time
import unicodedata
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

import requests

DB_PATH = os.environ.get("CHOLLOS_DB", os.path.join(os.path.dirname(__file__), "chollos.db"))
API = "https://api.wallapop.com/api/v3/search"
HEADERS = {
    "Accept": "application/json",
    "X-DeviceOS": "0",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "es-ES,es;q=0.9",
    "Origin": "https://es.wallapop.com",
    "Referer": "https://es.wallapop.com/",
}

log = logging.getLogger("chollos")

DEFAULT_SETTINGS = {
    "interval_min": "30",
    "latitude": "40.4168",
    "longitude": "-3.7038",
    "min_sample": "8",
    "median_window_days": "7",
    # Se buscan en título y descripción (salvo si van negadas: "sin roturas", "nada roto")
    "exclude_words": "roto, rota, rotos, rotas, piezas, para piezas, no funciona, no le funciona, no funcionan, "
                     "no enciende, no va, averiado, averiada, averia, para reparar, despiece, "
                     "bloqueado, bloqueada, defectuoso, defectuosa, sin placa, tarado",
    # Solo en el título: en la descripción suelen ser inocentes ("incluye funda", "por cambio de modelo")
    "exclude_title_words": "busco, compro, cambio, intercambio, funda, fundas, carcasa, protector, repuesto, "
                           "solo caja, caja vacia, alquilo, alquiler, piezas, pieza, icloud",
    # Accesorio si aparecen ANTES del producto en el título ("Adaptador corriente Raspberry Pi 5")
    "accessory_words": "adaptador, cargador, fuente, alimentador, transformador, cable, caja, carcasa, case, "
                       "funda, soporte, disipador, ventilador, protector, cristal, templado, correa, dock, "
                       "base, hub, hat, mando, bateria, pantalla, teclado, tapa, pegatina, vinilo, manual, libro, caddy, bandeja",
    "smtp_host": "smtp.gmail.com",
    "smtp_port": "587",
    "smtp_security": "starttls",
    "smtp_user": "",
    "smtp_pass": "",
    "smtp_from": "",
    "email_to": "",
    "email_enabled": "1",
    "notify_max_age_h": "24",
    "email_mode": "diario",   # diario = un resumen al día; inmediato = en cuanto aparecen
    "email_hour": "6",
    "digest_last": "",
    "milanuncios_enabled": "1",
    "ebay_enabled": "0",
    "ebay_app_id": "",
    "ebay_cert_id": "",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS searches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  keywords TEXT NOT NULL,
  min_price REAL, max_price REAL, distance_km INTEGER,
  below_median_pct REAL DEFAULT 40,
  drop_pct REAL DEFAULT 15,
  exclude_words TEXT DEFAULT '',
  title_must_match INTEGER DEFAULT 1,
  max_pages INTEGER DEFAULT 3,
  enabled INTEGER DEFAULT 1,
  created_at INTEGER, last_run INTEGER, last_count INTEGER, last_median REAL
);
CREATE TABLE IF NOT EXISTS items (
  id TEXT, search_id INTEGER,
  title TEXT, description TEXT,
  price REAL, first_price REAL, max_price REAL,
  url TEXT, image TEXT, city TEXT,
  reserved INTEGER DEFAULT 0, excluded INTEGER DEFAULT 0, excluded_reason TEXT,
  created_at INTEGER, first_seen INTEGER, last_seen INTEGER,
  source TEXT DEFAULT 'wallapop', condition TEXT DEFAULT 'used',
  PRIMARY KEY (id, search_id)
);
CREATE TABLE IF NOT EXISTS price_history (item_id TEXT, search_id INTEGER, price REAL, ts INTEGER);
CREATE INDEX IF NOT EXISTS ph_idx ON price_history(item_id, search_id);
CREATE TABLE IF NOT EXISTS deals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id TEXT, search_id INTEGER, kind TEXT,
  price REAL, ref_price REAL, pct REAL, ts INTEGER,
  notified INTEGER DEFAULT 0, dismissed INTEGER DEFAULT 0,
  UNIQUE(item_id, search_id, kind, price)
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, search_id INTEGER, ts INTEGER,
  fetched INTEGER, kept INTEGER, median REAL, deals INTEGER, error TEXT
);
"""


def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db():
    with db() as con:
        con.executescript(SCHEMA)
        cols = [r["name"] for r in con.execute("PRAGMA table_info(deals)")]
        if "grp" not in cols:
            con.execute("ALTER TABLE deals ADD COLUMN grp TEXT")  # variante usada como referencia
        icols = [r["name"] for r in con.execute("PRAGMA table_info(items)")]
        if "source" not in icols:
            con.execute("ALTER TABLE items ADD COLUMN source TEXT DEFAULT 'wallapop'")
        if "condition" not in icols:
            con.execute("ALTER TABLE items ADD COLUMN condition TEXT DEFAULT 'used'")
        for k, v in DEFAULT_SETTINGS.items():
            con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
        # Al activar el resumen diario, el primero sale mañana (no uno nada más desplegar)
        con.execute("UPDATE settings SET value=? WHERE key='digest_last' AND value=''", (time.strftime("%Y-%m-%d"),))


def get_settings():
    with db() as con:
        return {r["key"]: r["value"] for r in con.execute("SELECT key,value FROM settings")}


def set_settings(values):
    with db() as con:
        for k, v in values.items():
            con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, v))


# ---------------------------------------------------------------- texto

def norm(s):
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def parse_words(text):
    return [norm(w).strip() for w in re.split(r"[,\n]", text or "") if w.strip()]


NEGATIONS = re.compile(r"(sin|ni|nada|nunca|ningun|ninguna|libre de|no esta|no tiene|no ha)\s+(\w+\s+){0,2}$")


def find_word(text, w, allow_negated=False):
    for m in re.finditer(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", text):
        if allow_negated or not NEGATIONS.search(text[max(0, m.start() - 30):m.start()]):
            return True
    return False


def excluded_reason(title, description, words, title_words):
    t = norm(title)
    for w in title_words:
        if find_word(t, w, allow_negated=True):
            return f"{w} (título)"
    text = norm(f"{title} . {description}")
    for w in words:
        if find_word(text, w):
            return w
    return None


def keyword_tokens(keywords):
    return [tok for tok in re.split(r"[^a-z0-9]+", norm(keywords)) if tok]


def token_pos(title_norm, tok):
    """Posición del token como palabra completa; los números pueden ir pegados a letras ("pi5")
    pero no formar parte de otro número ("15", "3.5")."""
    m = re.search(token_pattern(tok), title_norm)
    return m.start() if m else -1


def token_pattern(tok):
    if tok.isdigit():
        # "pi5" vale; "15", "3.5" o "5v" no
        return r"(?<![0-9.,])" + tok + r"(?![0-9a-z])"
    # "2tb" vale para "tb"; "iphone13" vale para "iphone"
    return r"(?<![a-z])" + re.escape(tok) + r"(?![a-z])"


PARA = re.compile(r"(?<![a-z])(para|compatible con|compatible|for)\s+((el|la|tu|un|una|los|las)\s+)?$")


def title_matches(title, keywords):
    t = norm(title)
    return all(token_pos(t, tok) >= 0 for tok in keyword_tokens(keywords))


def accessory_reason(title, keywords, acc_words):
    """Accesorio si una palabra tipo "adaptador", "caja para"... aparece ANTES del producto buscado
    ("Caja para Raspberry Pi 5"), pero no después ("Raspberry Pi 5 con caja y fuente")."""
    t = norm(title)
    tokens = keyword_tokens(keywords)
    # "Disipador para Raspberry Pi 5", "Cooler compatible con iPhone 13"
    for tok in tokens:
        for m in re.finditer(token_pattern(tok), t):
            if PARA.search(t[:m.start()]):
                return "accesorio: para …"
    positions = [p for p in (token_pos(t, tok) for tok in tokens) if p >= 0]
    if not positions:
        return None
    first = min(positions)
    for w in acc_words:
        m = re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", t[:first])
        if m:
            return f"accesorio: {w}"
    return None


VARIANT_WORDS = ["pro", "max", "mini", "plus", "ultra", "lite", "fe", "air", "slim", "oled"]
CAPACITY = re.compile(r"(?<![0-9.,])(\d+)\s?(gb|tb)(?![a-z])")


def variant(title):
    """(modelo, capacidad) sacados del título: ("pro max", "256gb"), ("", "8gb")..."""
    t = norm(title)
    words = " ".join(w for w in VARIANT_WORDS if re.search(r"(?<![a-z])" + w + r"(?![a-z])", t))
    cap = CAPACITY.search(t)
    return words, (cap.group(1) + cap.group(2)) if cap else ""


def reference_median(item_variant, pool, min_group, global_median):
    """Mediana de los anuncios de la misma variante. Si el anuncio indica capacidad pero no hay
    bastantes de esa capacidad, no se compara (un 1GB frente a la mediana de los 8GB engaña)."""
    words, cap = item_variant
    same_model = [p for (w, c), p in pool if w == words]
    if cap:
        same = [p for (w, c), p in pool if w == words and c == cap]
        if len(same) >= min_group:
            return statistics.median(same), f"{words} {cap}".strip()
        return None, None
    if len(same_model) >= min_group:
        return statistics.median(same_model), words or None
    # Un "mini" o "pro" con pocos anuncios tampoco se compara con la mediana general
    return (global_median, None) if not words else (None, None)


def classify(search, title, desc, price, words, title_words, acc_words):
    """Devuelve el motivo de exclusión o None si el anuncio es válido."""
    reason = excluded_reason(title, desc, words, title_words)
    if not reason and search["title_must_match"] and not title_matches(title, search["keywords"]):
        reason = "título no coincide"
    if not reason:
        reason = accessory_reason(title, search["keywords"], acc_words)
    if not reason and ((search["min_price"] and price < search["min_price"]) or
                       (search["max_price"] and price > search["max_price"])):
        reason = "fuera del rango de precio"
    return reason


# ---------------------------------------------------------------- Wallapop

def fetch(search, settings):
    """Dos pasadas: "newest" solo devuelve lo publicado en las últimas horas (lo más fresco),
    y por relevancia da el catálogo amplio que necesita la mediana."""
    base = {
        "keywords": search["keywords"],
        "latitude": settings["latitude"],
        "longitude": settings["longitude"],
        "source": "search_box",
    }
    if search["min_price"]:
        base["min_sale_price"] = int(search["min_price"])
    if search["max_price"]:
        base["max_sale_price"] = int(search["max_price"])
    if search["distance_km"]:
        base["distance_in_km"] = int(search["distance_km"])
    pages = max(1, int(search["max_pages"] or 1))
    items = fetch_pages({**base, "order_by": "newest"}, 2)
    time.sleep(random.uniform(2, 5))
    return items + fetch_pages(base, pages)


_api_lock = threading.Lock()
_api_last = [0.0]
MIN_GAP = 3.0  # segundos mínimos entre peticiones a Wallapop, sumando búsquedas y temas


def api_get(params):
    with _api_lock:
        wait = _api_last[0] + MIN_GAP - time.time()
        if wait > 0:
            time.sleep(wait)
        try:
            return requests.get(API, params=params, headers=HEADERS, timeout=20)
        finally:
            _api_last[0] = time.time()


def fetch_pages(params, pages):
    items = []
    for page in range(pages):
        for attempt in range(3):
            r = api_get(params)
            if r.status_code == 429:
                time.sleep(30 * (attempt + 1))
                continue
            r.raise_for_status()
            break
        else:
            raise RuntimeError("Wallapop devolvió 429 (demasiadas peticiones)")
        data = r.json()
        items += data["data"]["section"]["payload"].get("items", [])
        nxt = (data.get("meta") or {}).get("next_page")
        if not nxt:
            break
        params = {"next_page": nxt}
        time.sleep(random.uniform(2, 5))
    return items


# ---------------------------------------------------------------- análisis

SOURCE_LABELS = {
    "wallapop": ("🟠", "Wallapop", "#e35c21"),
    "milanuncios": ("🟢", "Milanuncios", "#19a86f"),
    "ebay": ("🔵", "eBay", "#0064d2"),
}


def normalize_wallapop(it):
    iid = str(it["id"])
    imgs = it.get("images") or []
    return {
        "id": iid,
        "title": it.get("title", ""),
        "description": it.get("description", ""),
        "price": float(it["price"]["amount"]),
        "url": f"https://es.wallapop.com/item/{it.get('web_slug', iid)}",
        "image": imgs[0]["urls"].get("medium") if imgs else "",
        "city": (it.get("location") or {}).get("city", ""),
        "reserved": 1 if (it.get("reserved") or {}).get("flag") else 0,
        "source": "wallapop",
        "condition": "used",
        "created_at": it.get("created_at"),
    }


def process_search(search, settings):
    now = int(time.time())
    raw_wp = fetch(search, settings)
    items_norm = [normalize_wallapop(it) for it in raw_wp]

    if settings.get("milanuncios_enabled") == "1":
        import milanuncios as _ma
        try:
            items_norm += _ma.fetch(search["keywords"])
        except Exception:
            log.exception("Error en Milanuncios para %s", search["name"])

    if settings.get("ebay_enabled") == "1" and settings.get("ebay_app_id"):
        import ebay as _eb
        try:
            items_norm += _eb.fetch(search["keywords"], settings["ebay_app_id"], settings.get("ebay_cert_id", ""))
        except Exception:
            log.exception("Error en eBay para %s", search["name"])

    words = parse_words(settings["exclude_words"])
    title_words = parse_words(settings["exclude_title_words"]) + parse_words(search["exclude_words"])
    acc_words = parse_words(settings["accessory_words"])
    seen_ids, kept, new_deals = [], 0, 0

    with db() as con:
        for it in items_norm:
            iid = it["id"]
            if iid in seen_ids:
                continue
            seen_ids.append(iid)
            price = it["price"]
            title = it["title"]
            desc = it["description"]
            source = it["source"]
            condition = it["condition"]
            reason = classify(search, title, desc, price, words, title_words, acc_words)
            image = it["image"]
            url = it["url"]
            city = it["city"]
            reserved = it["reserved"]
            if not reason:
                kept += 1

            old = con.execute("SELECT * FROM items WHERE id=? AND search_id=?", (iid, search["id"])).fetchone()
            if old is None:
                con.execute(
                    "INSERT INTO items(id,search_id,title,description,price,first_price,max_price,url,image,city,"
                    "reserved,excluded,excluded_reason,created_at,first_seen,last_seen,source,condition) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (iid, search["id"], title, desc, price, price, price, url, image, city, reserved,
                     1 if reason else 0, reason, it.get("created_at"), now, now, source, condition))
                con.execute("INSERT INTO price_history VALUES(?,?,?,?)", (iid, search["id"], price, now))
            else:
                if price != old["price"]:
                    con.execute("INSERT INTO price_history VALUES(?,?,?,?)", (iid, search["id"], price, now))
                    # Bajada de precio respecto al máximo que hemos visto
                    if price < old["price"] and not reason and not reserved:
                        ref = old["max_price"] or old["price"]
                        pct = (ref - price) / ref * 100 if ref else 0
                        if pct >= (search["drop_pct"] or 0):
                            cur = con.execute(
                                "INSERT OR IGNORE INTO deals(item_id,search_id,kind,price,ref_price,pct,ts) "
                                "VALUES(?,?,?,?,?,?,?)", (iid, search["id"], "bajada", price, ref, pct, now))
                            new_deals += cur.rowcount
                con.execute(
                    "UPDATE items SET title=?,description=?,price=?,max_price=MAX(max_price,?),url=?,image=?,"
                    "city=?,reserved=?,excluded=?,excluded_reason=?,last_seen=?,source=?,condition=? WHERE id=? AND search_id=?",
                    (title, desc, price, price, url, image, city, reserved, 1 if reason else 0, reason, now,
                     source, condition, iid, search["id"]))

        # Reaplica las reglas actuales a los anuncios guardados que no han salido en esta pasada
        # (si cambias filtros o palabras, los antiguos dejan de contar para la mediana)
        qmarks = ",".join("?" * len(seen_ids)) or "''"
        for r in con.execute(f"SELECT id,title,description,price,excluded_reason FROM items "
                             f"WHERE search_id=? AND id NOT IN ({qmarks})", (search["id"], *seen_ids)).fetchall():
            reason = classify(search, r["title"], r["description"], r["price"], words, title_words, acc_words)
            if reason != r["excluded_reason"]:
                con.execute("UPDATE items SET excluded=?,excluded_reason=? WHERE id=? AND search_id=?",
                            (1 if reason else 0, reason, r["id"], search["id"]))

        # Mediana sobre los anuncios válidos vistos en la ventana reciente
        window = now - int(settings["median_window_days"]) * 86400
        valid = con.execute(
            "SELECT title,price FROM items WHERE search_id=? AND excluded=0 AND reserved=0 AND price>0 AND last_seen>=?",
            (search["id"], window)).fetchall()
        min_sample = int(settings["min_sample"])
        median = statistics.median([r["price"] for r in valid]) if len(valid) >= min_sample else None
        pool = [(variant(r["title"]), r["price"]) for r in valid]
        min_group = max(3, min_sample // 2)

        for r in con.execute(
                f"SELECT id,title,price FROM items WHERE search_id=? AND excluded=0 AND reserved=0 AND price>0 "
                f"AND id IN ({qmarks})", (search["id"], *seen_ids)).fetchall():
            ref, group = reference_median(variant(r["title"]), pool, min_group, median)
            if not ref or r["price"] > ref * (1 - (search["below_median_pct"] or 0) / 100):
                continue
            pct = (ref - r["price"]) / ref * 100
            cur = con.execute(
                "INSERT OR IGNORE INTO deals(item_id,search_id,kind,price,ref_price,pct,ts,grp) "
                "VALUES(?,?,?,?,?,?,?,?)", (r["id"], search["id"], "mediana", r["price"], ref, pct, now, group))
            new_deals += cur.rowcount

        con.execute("UPDATE searches SET last_run=?,last_count=?,last_median=? WHERE id=?",
                    (now, kept, median, search["id"]))
        con.execute("INSERT INTO runs(search_id,ts,fetched,kept,median,deals) VALUES(?,?,?,?,?,?)",
                    (search["id"], now, len(seen_ids), kept, median, new_deals))
    log.info("[%s] %d anuncios, %d válidos, mediana=%s, %d chollos nuevos",
             search["name"], len(seen_ids), kept, median, new_deals)
    return new_deals


# ---------------------------------------------------------------- email

def pending_deals(con, max_age_h):
    since = int(time.time()) - max_age_h * 3600
    # Los más antiguos que la ventana no se envían (evita avalanchas al configurar el SMTP tarde)
    con.execute("UPDATE deals SET notified=2 WHERE notified=0 AND ts<?", (since,))
    con.execute("UPDATE t_deals SET notified=2 WHERE notified=0 AND ts<?", (since,))
    rows = [dict(r, src="s") for r in con.execute(
        "SELECT d.*, i.title, i.url, i.image, i.city, i.source, i.condition, s.name AS search_name, s.amazon_price, s.amazon_url FROM deals d "
        "JOIN items i ON i.id=d.item_id AND i.search_id=d.search_id "
        "JOIN searches s ON s.id=d.search_id "
        "WHERE d.notified=0 AND d.dismissed=0 AND i.reserved=0 AND i.excluded=0")]
    rows += [dict(r, src="t") for r in con.execute(
        "SELECT d.*, d.product_key AS grp, i.title, i.url, i.image, i.city, 'wallapop' AS source, 'used' AS condition, th.name AS search_name, "
        "p.amazon_price, p.amazon_url FROM t_deals d "
        "JOIN t_items i ON i.id=d.item_id JOIN themes th ON th.id=d.theme_id "
        "LEFT JOIN products p ON p.key=d.product_key "
        "WHERE d.notified=0 AND d.dismissed=0 AND i.reserved=0 AND i.excluded_reason IS NULL")]
    # Un anuncio puede tener varios registros (bajadas sucesivas, mediana y bajada): se envía una vez,
    # con el mayor descuento, y se marcan todos como enviados
    best = {}
    for d in rows:
        k = (d["src"], d["item_id"])
        if k not in best or d["pct"] > best[k]["pct"]:
            best[k] = d
    for d in rows:
        best[(d["src"], d["item_id"])].setdefault("all_ids", []).append(d["id"])
    return sorted(best.values(), key=lambda d: -d["pct"])


def deal_tag(d):
    if d["kind"] == "mediana":
        return f"-{d['pct']:.0f}% vs mediana{' ' + d['grp'] if d['grp'] else ''} ({d['ref_price']:.0f} €)"
    if d["kind"] == "referencia":
        return f"-{d['pct']:.0f}% vs lo habitual para {d['grp']} ({d['ref_price']:.0f} €, {d.get('basis') or ''})"
    return f"Rebajado {d['pct']:.0f}% (antes {d['ref_price']:.0f} €)"


def render_email(deals):
    rows = []
    for d in deals:
        tag = deal_tag(d)
        src = d.get("source") or "wallapop"
        _emoji, label, color = SOURCE_LABELS.get(src, ("🟠", "Wallapop", "#e35c21"))
        badge = (f'<span style="display:inline-block;font-size:11px;padding:1px 6px;border-radius:8px;'
                 f'color:#fff;background:{color};margin-right:4px">{label}</span>')
        cond = d.get("condition") or "used"
        cond_txt = ' · <span style="color:#1a7a5a">Nuevo</span>' if cond == "new" else ""
        img = f'<img src="{escape(d["image"])}" width="110" style="border-radius:6px">' if d["image"] else ""
        rows.append(
            f'<tr><td style="padding:8px">{img}</td><td style="padding:8px;font-family:sans-serif">'
            f'<a href="{escape(d["url"])}" style="font-size:15px;font-weight:bold;color:#0b7a6f">{escape(d["title"])}</a><br>'
            f'<span style="font-size:20px;font-weight:bold">{d["price"]:.0f} €</span> '
            f'<span style="color:#c0392b">{tag}</span><br>'
            + (f'<span style="color:#333">💬 {escape(d["reason"])}</span><br>' if d.get("reason") else "")
            + (f'<a href="{escape(d["amazon_url"] or "")}" style="color:#e47911">🛒 Nuevo en Amazon: {d["amazon_price"]:.0f} €</a><br>'
               if d.get("amazon_price") else "")
            + f'<span style="color:#777">{badge}{escape(d["search_name"])} · {escape(d["city"] or "")}{cond_txt}</span></td></tr>')
    return ('<div style="font-family:sans-serif"><h2>🔥 Chollos detectados</h2>'
            f'<p style="color:#777">{len(deals)} chollo(s) de las últimas 24 horas, de más a menos rebajado. '
            'Los que se vendieron o reservaron entretanto ya no aparecen.</p><table>'
            + "".join(rows) + "</table></div>")


def send_email(settings, subject, html):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings["smtp_from"] or settings["smtp_user"]
    msg["To"] = settings["email_to"]
    msg.attach(MIMEText(html, "html", "utf-8"))
    port = int(settings["smtp_port"])
    if settings["smtp_security"] == "ssl":
        srv = smtplib.SMTP_SSL(settings["smtp_host"], port, timeout=30)
    else:
        srv = smtplib.SMTP(settings["smtp_host"], port, timeout=30)
        if settings["smtp_security"] == "starttls":
            srv.starttls()
    with srv:
        if settings["smtp_user"]:
            srv.login(settings["smtp_user"], settings["smtp_pass"])
        srv.sendmail(msg["From"], [a.strip() for a in settings["email_to"].split(",") if a.strip()],
                     msg.as_string())


def email_configured(s):
    return s["email_enabled"] == "1" and s["smtp_host"] and s["email_to"]


_notify_lock = threading.Lock()


def notify(settings, force=False):
    """Envía los chollos pendientes. En modo diario solo lo hace el resumen (force=True)."""
    if not email_configured(settings):
        return 0
    daily = settings.get("email_mode") == "diario"
    if daily and not force:
        return 0
    with _notify_lock, db() as con:
        deals = pending_deals(con, 26 if daily else int(settings["notify_max_age_h"]))
        if not deals:
            return 0
        subject = (f"🔥 Resumen diario: {len(deals)} chollo(s)" if daily
                   else f"🔥 {len(deals)} chollo(s) detectados")
        send_email(settings, subject, render_email(deals))
        con.executemany("UPDATE deals SET notified=1 WHERE id=?",
                        [(i,) for d in deals if d["src"] == "s" for i in d["all_ids"]])
        con.executemany("UPDATE t_deals SET notified=1 WHERE id=?",
                        [(i,) for d in deals if d["src"] == "t" for i in d["all_ids"]])
    log.info("Email enviado con %d chollos", len(deals))
    return len(deals)


def search_amazon(search, settings):
    import amazon  # evita la importación circular
    try:
        if amazon.available(settings) and amazon.stale(search):
            amazon.refresh("searches", "id", search["id"], search["name"], settings)
    except Exception:
        log.exception("Error consultando Amazon para %s", search["name"])


class Digest:
    """Envía el resumen diario a la hora configurada (o en cuanto se pueda, si a esa hora estaba parado)."""

    def __init__(self):
        self.last_error = None

    def loop(self):
        while True:
            try:
                s = get_settings()
                today = time.strftime("%Y-%m-%d")
                if (s.get("email_mode") == "diario" and email_configured(s) and s.get("digest_last") != today
                        and time.localtime().tm_hour >= int(s.get("email_hour") or 6)):
                    n = notify(s, force=True)
                    set_settings({"digest_last": today})
                    log.info("Resumen diario: %d chollos", n)
                    self.last_error = None
            except Exception as e:
                log.exception("Error en el resumen diario")
                self.last_error = str(e)
            time.sleep(60)

    def start(self):
        threading.Thread(target=self.loop, daemon=True).start()


# ---------------------------------------------------------------- planificador

class Scheduler:
    def __init__(self):
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.only = None
        self.running = False
        self.current = None
        self.last_cycle = None
        self.next_cycle = None
        self.last_error = None

    def trigger(self, search_id=None):
        self.only = search_id
        self.wake.set()

    def cycle(self, only=None):
        with self.lock:
            self.running = True
            settings = get_settings()
            try:
                with db() as con:
                    q = "SELECT * FROM searches WHERE enabled=1" if only is None else "SELECT * FROM searches WHERE id=?"
                    searches = con.execute(q, () if only is None else (only,)).fetchall()
                for i, s in enumerate(searches):
                    self.current = s["name"]
                    try:
                        if process_search(s, settings):
                            search_amazon(s, settings)
                    except Exception as e:
                        log.exception("Error en búsqueda %s", s["name"])
                        with db() as con:
                            con.execute("INSERT INTO runs(search_id,ts,error) VALUES(?,?,?)",
                                        (s["id"], int(time.time()), str(e)[:500]))
                    if i < len(searches) - 1:
                        time.sleep(random.uniform(4, 9))
                self.current = "enviando email"
                try:
                    notify(settings)
                    self.last_error = None
                except Exception as e:
                    log.exception("Error enviando email")
                    self.last_error = f"Email: {e}"
            finally:
                self.running = False
                self.current = None
                self.last_cycle = int(time.time())

    def loop(self):
        while True:
            interval = int(get_settings().get("interval_min", 30)) * 60
            self.next_cycle = int(time.time()) + interval
            only = self.only
            self.only = None
            try:
                self.cycle(only)
            except Exception:
                log.exception("Error en ciclo")
            self.next_cycle = int(time.time()) + interval
            self.wake.wait(interval)
            self.wake.clear()

    def start(self):
        threading.Thread(target=self.loop, daemon=True).start()
