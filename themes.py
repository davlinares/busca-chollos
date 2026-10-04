"""Temas: descubre anuncios de un ámbito (p. ej. homelab) con búsquedas amplias, identifica el
producto concreto de cada uno y lo valora contra una base de precios propia que se construye
lanzando búsquedas de referencia de ese producto."""
import logging
import random
import re
import statistics
import threading
import time

import amazon
import core
import llm
from core import norm

log = logging.getLogger("chollos.temas")

SCHEMA = """
CREATE TABLE IF NOT EXISTS themes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, keywords TEXT NOT NULL,
  below_pct REAL DEFAULT 35, drop_pct REAL DEFAULT 15, enabled INTEGER DEFAULT 1,
  cursor INTEGER DEFAULT 0, last_run INTEGER, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS t_items (
  id TEXT PRIMARY KEY, theme_id INTEGER, keyword TEXT, title TEXT, description TEXT,
  price REAL, max_price REAL, url TEXT, image TEXT, city TEXT, reserved INTEGER DEFAULT 0,
  created_at INTEGER, first_seen INTEGER, last_seen INTEGER,
  product_key TEXT, kind TEXT, excluded_reason TEXT, eval_price REAL
);
CREATE INDEX IF NOT EXISTS t_items_key ON t_items(product_key);
CREATE TABLE IF NOT EXISTS products (
  key TEXT PRIMARY KEY, kind TEXT, query TEXT, median REAL, n INTEGER, pmin REAL,
  last_ref INTEGER, next_ref INTEGER DEFAULT 0, status TEXT DEFAULT 'pendiente', discovered INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS samples (
  key TEXT, item_id TEXT, price REAL, ts INTEGER, PRIMARY KEY (key, item_id)
);
CREATE TABLE IF NOT EXISTS t_deals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT, theme_id INTEGER, product_key TEXT, kind TEXT,
  price REAL, ref_price REAL, n INTEGER, pct REAL, ts INTEGER,
  notified INTEGER DEFAULT 0, dismissed INTEGER DEFAULT 0,
  UNIQUE(item_id, kind, price)
);
"""

HOMELAB_KEYWORDS = """synology
qnap
terramaster
nas
hp microserver
proliant
poweredge
servidor rack
thinkcentre tiny
lenovo m720q
lenovo m920q
optiplex micro
elitedesk mini
prodesk mini
intel nuc
mini pc
beelink
minisforum
raspberry pi
mikrotik
ubiquiti
unifi
switch gestionable
switch 2.5gb
switch 10gb
sfp+
tp-link omada
tarjeta red 10gb
disco duro nas
wd red
ironwolf
seagate exos
disco duro 4tb
disco duro 8tb
disco duro 12tb
ssd nvme
ssd 1tb
ssd 2tb
ram ddr4 ecc
memoria ram ddr4
memoria ram ddr5
sodimm ddr4
xeon
sai apc
armario rack
coral tpu
zigbee usb"""

REF_TTL = 3 * 86400          # cada cuánto se refresca el precio de referencia de un producto
REF_RETRY = 7 * 86400        # si hubo pocos datos, cuándo reintentar
SAMPLE_WINDOW = 30 * 86400   # anuncios que cuentan para la mediana de referencia
MIN_SAMPLES = 5


def init_db():
    with core.db() as con:
        con.executescript(SCHEMA)
        if not con.execute("SELECT 1 FROM themes").fetchone():
            con.execute("INSERT INTO themes(name,keywords,created_at) VALUES(?,?,?)",
                        ("Homelab", HOMELAB_KEYWORDS, int(time.time())))
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('theme_tick_s','45')")
        acc = con.execute("SELECT value FROM settings WHERE key='accessory_words'").fetchone()
        if acc and "caddy" not in acc[0]:
            con.execute("UPDATE settings SET value=? WHERE key='accessory_words'", (acc[0] + ", caddy, bandeja",))
    migrate()
    llm.init_db()
    amazon.init_db()


# ---------------------------------------------------------------- identificación de producto

def bucket(n, unit):
    """Agrupa capacidades comerciales equivalentes: 480/500/512GB → 512gb, 960GB/1000GB → 1tb."""
    gb = n * 1000 if unit == "tb" else n
    for lo, hi, label in [(200, 270, "256gb"), (440, 530, "512gb"), (900, 1100, "1tb"),
                          (1800, 2100, "2tb"), (3800, 4200, "4tb")]:
        if lo <= gb <= hi:
            return label
    return f"{n}{unit}"


CAP = re.compile(r"(?<![0-9.,])(\d+(?:[.,]\d+)?)\s?(gb|tb)(?![a-z])")
KIT = re.compile(r"(?<![0-9])(\d)\s?x\s?(\d+)\s?gb(?![a-z])")
LOT = re.compile(r"(?<![a-z])(lote|pack de|\d\s?x\s?\d+\s?tb|[2-9] discos|[2-9] unidades)(?![a-z])")


def capacity(t, unit=None, max_gb=None):
    for m in CAP.finditer(t):
        n = float(m[1].replace(",", "."))
        if unit and m[2] != unit:
            continue
        if max_gb and (n * 1000 if m[2] == "tb" else n) > max_gb:
            continue
        return (int(n) if n == int(n) else n), m[2]
    return None


def _has(pattern, t):
    return re.search(pattern, t) is not None


def _device_rules():
    """(tipo, regex, requisito, función que construye la clave). El requisito evita falsos
    positivos de modelos cortos ("ds220" solo cuenta si pone synology o va pegado)."""
    def rpi(m, t):
        gen = re.sub(r"\s+", " ", m[1]).strip()
        # "modelo B" es el de siempre; solo distinguen B+ y A+
        key = f"raspberry pi {gen}" + (f" {m[2]}" if m[2] and m[2] != "b" else "")
        if gen in ("4", "5"):
            c = capacity(t, "gb", 16)
            if c:
                key += f" {c[0]}gb"
        return key

    def apc(m, t):
        series = {"back ups": "back-ups", "backups": "back-ups", "smart ups": "smart-ups",
                  "smartups": "smart-ups"}.get(m[1].replace("-", " ").strip(), m[1])
        return f"apc {series} {m[2]}"

    def elite(m, t):
        form = {"dm": "mini", "desktop mini": "mini"}.get(m[4] or "", m[4] or "")
        return f"hp {m[1]} {m[2]} g{m[3]}" + (f" {form}" if form else "")

    def optiplex(m, t):
        form = {"mff": "micro", "usff": "micro"}.get(m[2] or "", m[2] or "")
        return f"dell optiplex {m[1]}" + (f" {form}" if form else "")

    def nuc(m, t):
        cpu = re.search(r"(?<![a-z0-9])i([3579])(?![0-9])", t)
        return f"intel nuc {m[1]}" + (f" i{cpu[1]}" if cpu else "")

    return [
        ("nas", r"(?<![a-z0-9])(ds|rs|dva)\s?-?(\d{3,4})(\+|j|play|xs\+?|slim)?(?![a-z0-9])",
         r"synology|(?<![a-z])(ds|rs)-?\d{3}", lambda m, t: f"synology {m[1]}{m[2]}{m[3] or ''}"),
        ("nas", r"(?<![a-z0-9])(ts|tvs|tbs|tr)-?\s?(\d{3,4}[a-z]{0,4})(?![a-z0-9])",
         r"qnap", lambda m, t: f"qnap {m[1]}-{m[2]}"),
        ("nas", r"(?<![a-z0-9])([ftu]\d)-(\d{3})(?![0-9])", r"terramaster", lambda m, t: f"terramaster {m[1]}-{m[2]}"),
        ("sbc", r"(?:raspberry\s?pi|rpi)\s?(zero\s?2\s?w?|zero\s?w?|400|500|[2-5])(?![0-9])\s?(?:model(?:o)?\s?)?(b\+|b|a\+)?(?![a-z])",
         None, rpi),
        ("minipc", r"(?<![a-z0-9])m(\d{2,4})([qn])(?![a-z0-9])", r"lenovo|thinkcentre|tiny",
         lambda m, t: f"lenovo m{m[1]}{m[2]}"),
        ("minipc", r"optiplex\s?(\d{4}|\d{3})\s?(micro|mff|usff|sff|mt|tower)?", None, optiplex),
        ("minipc", r"(elitedesk|prodesk)\s?(\d{3})\s?g(\d)\s?(desktop mini|mini|dm|sff|tower|mt)?", None, elite),
        ("minipc", r"(?<![a-z])nuc\s?(\d{1,2})(?![0-9])", None, nuc),
        ("minipc", r"beelink\s?((?:ser|eq|gtr|gti|mini\s?s|me\s?mini|u)\s?\d{0,2}[a-z]{0,3}\d{0,2})", None,
         lambda m, t: "beelink " + m[1].replace(" ", "")),
        ("minipc", r"minisforum\s?([a-z]{2,3}\s?\d{2,4}[a-z]{0,3})", None, lambda m, t: "minisforum " + m[1].replace(" ", "")),
        ("servidor", r"microserver\s?(gen\s?\d+|n\d{2}l)", None, lambda m, t: "hp microserver " + m[1].replace(" ", "")),
        ("servidor", r"poweredge\s?([rt]\d{2,3}\w{0,2})", None, lambda m, t: f"dell poweredge {m[1]}"),
        ("red", r"(?<![a-z0-9])(rb\d{3,4}[\w+-]*|crs\d{3}[\w+-]*|css\d{3}[\w+-]*|ccr\d{4}[\w-]*|hap\s?(?:ac\s?[23]|ax\s?[23]|ac|lite)|hex\s?(?:s|poe|lite)?)(?![a-z0-9])",
         r"mikrotik|(?<![a-z])(rb|crs|css|ccr)\d{3}", lambda m, t: "mikrotik " + re.sub(r"\s+", " ", m[1])),
        ("red", r"(?<![a-z0-9])(usw-[\w-]+|uap-[\w-]+|u[67]-[\w-]+|udm[\w-]*|udr|ucg-[\w-]+|us-\d+[\w-]*|usg[\w-]*|er-?x|edgerouter\s?\w+|nanostation\s?\w*)(?![a-z0-9])",
         r"ubiquiti|unifi|edgerouter|nanostation|(?<![a-z])(usw|uap|udm|ucg)", lambda m, t: "ubiquiti " + m[1]),
        ("red", r"(?<![a-z0-9])(tl-sg\d+\w*|tl-sx\d+\w*|sg\d{4}\w*|tl-er\d+\w*|er\d{3,4}\w*|eap\d{3}\w*|oc\d{3})(?![a-z0-9])",
         r"tp-?link|omada|(?<![a-z])tl-", lambda m, t: "tp-link " + m[1]),
        ("red", r"(?<![a-z0-9])(gs\d{3}\w*|xs\d{3}\w*|ms\d{3}\w*)(?![a-z0-9])", r"netgear", lambda m, t: "netgear " + m[1]),
        ("sai", r"(back-?\s?ups|smart-?\s?ups|br|bx|bv|smt|smc|sua|bk|be)\s?-?(\d{3,4})", r"(?<![a-z])apc(?![a-z])", apc),
        ("otros", r"coral\s?(usb|tpu|m\.?2|pcie)", None, lambda m, t: "google coral " + ("usb" if m[1] in ("usb", "tpu") else m[1])),
    ]


DEVICE_RULES = [(k, re.compile(p), re.compile(r) if r else None, f) for k, p, r, f in _device_rules()]

BRANDS = r"samsung|crucial|kingston|wd|western digital|seagate|toshiba|sandisk|corsair|g\.?skill|hynix|micron|lexar|hgst|hikvision|adata|patriot|teamgroup|intel|sabrent|pny|transcend"
DEVICE_WORDS = r"(?<![a-z])(pc|portatil|servidor|nas|ordenador|torre|sobremesa|placa|mini|kit|equipo|synology|qnap|consola|macbook|imac|tablet|movil)(?![a-z])"


def _component_start(t, pattern):
    """Posición de la palabra de componente si el título "va de" ese componente: aparece al
    principio (o tras una marca) y no hay antes un equipo completo ("Mini PC ... 16GB DDR4")."""
    m = re.search(pattern, t)
    if not m:
        return None
    head = t[:m.start()]
    if re.search(DEVICE_WORDS, head):
        return None
    if m.start() <= 25 or re.match(r"\s*(" + BRANDS + r")(?![a-z])", t):
        return m.start()
    return None


def identify(title):
    """(clave, tipo, posición) del producto del título, o None si no se reconoce."""
    t = norm(title)
    for kind, rx, req, build in DEVICE_RULES:
        m = rx.search(t)
        if m and (req is None or req.search(t)):
            return re.sub(r"\s+", " ", build(m, t)).strip(), kind, m.start()

    # Componentes: valoración por capacidad (€/TB, €/GB)
    pos = _component_start(t, r"(?<![a-z])(ram|ddr[345]|memoria|sodimm|so-dimm|rdimm|udimm)(?![a-z])")
    gen = re.search(r"ddr([345])", t)
    if pos is not None and gen:
        kit = KIT.search(t)
        total = int(kit[1]) * int(kit[2]) if kit else (capacity(t, "gb", 512) or (None,))[0]
        if total:
            key = f"ram ddr{gen[1]} {total}gb"
            if _has(r"(?<![a-z])(ecc|rdimm|registered|reg)(?![a-z])", t):
                key += " ecc"
            if _has(r"so-?dimm|portatil", t):
                key += " sodimm"
            return key, "ram", pos

    pos = _component_start(t, r"(?<![a-z])(ssd|nvme|m\.2)(?![a-z])")
    c = capacity(t)
    if pos is not None and c:
        kind = "nvme" if _has(r"nvme|m\.2|pcie", t) else "sata"
        return f"ssd {kind} {bucket(*c)}", "ssd", pos

    pos = _component_start(t, r"(?<![a-z])(disco|hdd|ironwolf|wd red|red plus|red pro|exos|skyhawk|wd purple|barracuda|ultrastar|n300|wd gold)(?![a-z])")
    c = capacity(t, "tb")
    if pos is not None and c and not _has(r"(?<![a-z])(ssd|nvme)(?![a-z])", t):
        ext = " externo" if _has(r"extern|usb|portatil|portable|my passport|my book|expansion", t) else ""
        return f"hdd{ext} {bucket(*c)}", "hdd", pos

    cpu_ctx = re.match(r"\s*(procesador|cpu|intel|amd|xeon|ryzen|core)", t)
    if cpu_ctx:
        for rx, fmt in [(r"xeon\s?(e[357]-?\s?\d{4}\w*(?:\s?v\d)?|(?:silver|gold|platinum|bronze)\s?\d{4}\w*|w-\d{4}\w*)",
                         lambda m: "intel xeon " + m[1].replace(" ", "")),
                        (r"(?<![a-z0-9])i([3579])-?\s?(\d{4,5}[a-z]{0,2})(?![a-z0-9])", lambda m: f"intel core i{m[1]}-{m[2]}"),
                        (r"ryzen\s?([3579])\s?(\d{4}[a-z0-9]{0,3})", lambda m: f"amd ryzen {m[1]} {m[2]}")]:
            m = re.search(rx, t)
            if m:
                return fmt(m), "cpu", m.start()
    return None


def reference_query(key):
    """Qué buscar en Wallapop para valorar un producto."""
    q = re.sub(r"^(hdd externo) ", "disco duro externo ", key)
    q = re.sub(r"^hdd ", "disco duro ", q)
    q = q.replace(" sodimm", " sodimm").replace("ram ddr", "memoria ram ddr")
    return q


def item_reason(title, desc, price, pos, settings, kind=None):
    """Mismos filtros que las búsquedas + accesorios delante del producto + lotes."""
    reason = core.excluded_reason(title, desc, core.parse_words(settings["exclude_words"]),
                                  core.parse_words(settings["exclude_title_words"]))
    if reason:
        return reason
    t = norm(title)
    head = t[:pos]
    if core.PARA.search(head) or re.search(r"(?<![a-z])(para|compatible)(?![a-z])", head):
        return "accesorio: para …"
    for w in core.parse_words(settings["accessory_words"]):
        if re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", head):
            return f"accesorio: {w}"
    if kind in ("hdd", "ssd", "ram") and LOT.search(t):
        return "lote"
    if price < 5:
        return "precio simbólico"
    return None


# ---------------------------------------------------------------- lógica

MIGRATIONS = {
    "t_items": {"key_src": "TEXT", "llm_checked": "INTEGER DEFAULT 0", "cand": "INTEGER DEFAULT 0",
                "cand_ref": "REAL", "cand_basis": "TEXT", "reviewed_price": "REAL",
                "llm_verdict": "TEXT", "llm_reason": "TEXT", "llm_value": "REAL"},
    "products": {"source": "TEXT DEFAULT 'reglas'", "est_price": "REAL", "est_low": "REAL", "est_high": "REAL",
                 "est_conf": "TEXT", "est_note": "TEXT", "est_ts": "INTEGER"},
    "samples": {"title": "TEXT"},
    "t_deals": {"reason": "TEXT", "basis": "TEXT"},
    "themes": {"min_saving": "REAL DEFAULT 20"},
}


def migrate():
    with core.db() as con:
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
            for col, decl in cols.items():
                if col not in have:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def parse_item(it):
    imgs = it.get("images") or []
    return {
        "id": it["id"],
        "title": it.get("title", ""),
        "description": it.get("description", ""),
        "price": float(it["price"]["amount"]),
        "url": f"https://es.wallapop.com/item/{it.get('web_slug', it['id'])}",
        "image": imgs[0]["urls"].get("medium") if imgs else "",
        "city": (it.get("location") or {}).get("city", ""),
        "reserved": 1 if (it.get("reserved") or {}).get("flag") else 0,
        "created_at": it.get("created_at"),
    }


def add_sample(con, key, item_id, price, now, title=None):
    con.execute("INSERT INTO samples(key,item_id,price,ts,title) VALUES(?,?,?,?,?) "
                "ON CONFLICT(key,item_id) DO UPDATE SET price=excluded.price, ts=excluded.ts",
                (key, item_id, price, now, title))


def ensure_product(con, key, kind, source="reglas"):
    con.execute("INSERT OR IGNORE INTO products(key,kind,query,source) VALUES(?,?,?,?)",
                (key, kind, reference_query(key), source))
    con.execute("UPDATE products SET discovered=discovered+1 WHERE key=?", (key,))


def recompute(con, key, now):
    prices = [r[0] for r in con.execute("SELECT price FROM samples WHERE key=? AND ts>=?", (key, now - SAMPLE_WINDOW))]
    if len(prices) >= MIN_SAMPLES:
        con.execute("UPDATE products SET median=?,n=?,pmin=?,status='ok' WHERE key=?",
                    (statistics.median(prices), len(prices), min(prices), key))
    else:
        con.execute("UPDATE products SET median=NULL,n=?,status='pocos datos' WHERE key=?", (len(prices), key))


def discover(theme, settings):
    """Una palabra del tema (en rotación): anuncios recién publicados."""
    kws = [k.strip() for k in theme["keywords"].splitlines() if k.strip()]
    if not kws:
        return 0
    idx = (theme["cursor"] or 0) % len(kws)
    kw = kws[idx]
    raw = core.fetch_pages({"keywords": kw, "latitude": settings["latitude"], "longitude": settings["longitude"],
                            "source": "search_box", "order_by": "newest"}, 1)
    now = int(time.time())
    new = 0
    with core.db() as con:
        for it in raw:
            d = parse_item(it)
            old = con.execute("SELECT * FROM t_items WHERE id=?", (d["id"],)).fetchone()
            ident = identify(d["title"])
            if ident:
                key, kind, pos, src = *ident, "reglas"
            elif old is not None and old["key_src"] == "ia":
                key, kind, pos, src = old["product_key"], old["kind"], 0, "ia"   # ya lo identificó la IA
            else:
                key, kind, pos, src = None, None, 0, None
            reason = item_reason(d["title"], d["description"], d["price"], pos, settings, kind) if key else None
            if old is not None and old["excluded_reason"] and old["excluded_reason"].endswith("(IA)"):
                reason = old["excluded_reason"]
            if old is None:
                new += 1
                con.execute(
                    "INSERT INTO t_items(id,theme_id,keyword,title,description,price,max_price,url,image,city,reserved,"
                    "created_at,first_seen,last_seen,product_key,kind,excluded_reason,key_src) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (d["id"], theme["id"], kw, d["title"], d["description"], d["price"], d["price"], d["url"], d["image"],
                     d["city"], d["reserved"], d["created_at"], now, now, key, kind, reason, src))
                if key:
                    ensure_product(con, key, kind)
            else:
                if key and d["price"] < old["price"] and not reason:
                    ref = old["max_price"] or old["price"]
                    pct = (ref - d["price"]) / ref * 100
                    if pct >= (theme["drop_pct"] or 15) and ref - d["price"] >= (theme["min_saving"] or 0):
                        con.execute("INSERT OR IGNORE INTO t_deals(item_id,theme_id,product_key,kind,price,ref_price,pct,ts) "
                                    "VALUES(?,?,?,?,?,?,?,?)", (d["id"], theme["id"], key, "bajada", d["price"], ref, pct, now))
                con.execute("UPDATE t_items SET title=?,description=?,price=?,max_price=MAX(max_price,?),image=?,reserved=?,"
                            "last_seen=?,product_key=?,kind=?,excluded_reason=?,key_src=? WHERE id=?",
                            (d["title"], d["description"], d["price"], d["price"], d["image"], d["reserved"], now,
                             key, kind, reason, src, d["id"]))
            if key and not reason and not d["reserved"]:
                add_sample(con, key, d["id"], d["price"], now, d["title"])
        con.execute("UPDATE themes SET cursor=?, last_run=? WHERE id=?", (idx + 1, now, theme["id"]))
    log.info("[tema %s] '%s': %d anuncios, %d nuevos", theme["name"], kw, len(raw), new)
    return new


def next_reference(con, now):
    """Producto que más urge valorar: el que tiene anuncios frescos sin evaluar y referencia caducada."""
    return con.execute(
        "SELECT p.* FROM products p WHERE p.next_ref<=? AND EXISTS (SELECT 1 FROM t_items i WHERE "
        "i.product_key=p.key AND i.excluded_reason IS NULL AND i.last_seen>=?) ORDER BY p.last_ref IS NOT NULL, "
        "p.discovered DESC LIMIT 1", (now, now - 2 * 86400)).fetchone()


def reference(product, settings):
    """Busca el producto concreto (por relevancia) y guarda los precios de los que coinciden.
    Los productos de las reglas se comparan con las reglas; los que nombró la IA, exigiendo que el
    título contenga todas las palabras de la clave."""
    raw = core.fetch_pages({"keywords": product["query"], "latitude": settings["latitude"],
                            "longitude": settings["longitude"], "source": "search_box"}, 2)
    now = int(time.time())
    acc_words = core.parse_words(settings["accessory_words"])
    matched = 0
    with core.db() as con:
        for it in raw:
            d = parse_item(it)
            if d["reserved"]:
                continue
            if product["source"] == "ia":
                if not core.title_matches(d["title"], product["key"]) or \
                        core.accessory_reason(d["title"], product["key"], acc_words):
                    continue
                pos = 0
            else:
                ident = identify(d["title"])
                if not ident or ident[0] != product["key"]:
                    continue
                pos = ident[2]
            if item_reason(d["title"], d["description"], d["price"], pos, settings, product["kind"]):
                continue
            matched += 1
            add_sample(con, product["key"], d["id"], d["price"], now, d["title"])
        recompute(con, product["key"], now)
        p = con.execute("SELECT * FROM products WHERE key=?", (product["key"],)).fetchone()
        con.execute("UPDATE products SET last_ref=?, next_ref=? WHERE key=?",
                    (now, now + (REF_TTL if p["status"] == "ok" else REF_RETRY), product["key"]))
    log.info("[referencia] %s: %d/%d coinciden, mediana=%s (n=%s)", product["key"], matched, len(raw), p["median"], p["n"])


def reference_price(p):
    """(precio habitual, de dónde sale) o (None, None). Mediana real si hay datos; si no, la
    estimación de la IA cuando no es de confianza baja."""
    if p["status"] == "ok" and p["median"]:
        return p["median"], f"mediana de {p['n']} anuncios"
    if p["est_price"] and p["est_conf"] in ("alta", "media"):
        return p["est_price"], f"estimación IA, confianza {p['est_conf']}"
    return None, None


def evaluate(con, now):
    """Marca como candidatos los anuncios frescos que están por debajo de lo habitual."""
    n = 0
    rows = con.execute(
        "SELECT i.*, p.status, p.median, p.n, p.est_price, p.est_conf, t.below_pct, t.min_saving FROM t_items i "
        "JOIN products p ON p.key=i.product_key JOIN themes t ON t.id=i.theme_id "
        "WHERE i.excluded_reason IS NULL AND i.reserved=0 AND i.last_seen>=? "
        "AND (i.eval_price IS NULL OR i.eval_price<>i.price)", (now - 2 * 86400,)).fetchall()
    for r in rows:
        ref, basis = reference_price(r)
        if not ref:
            continue  # sin referencia todavía: se reevalúa cuando la haya
        con.execute("UPDATE t_items SET eval_price=? WHERE id=?", (r["price"], r["id"]))
        if r["price"] > ref * (1 - r["below_pct"] / 100) or ref - r["price"] < (r["min_saving"] or 0):
            continue  # poco rebajado, o se ahorran pocos euros (un 60% de 10 € no merece aviso)
        if r["price"] < ref * 0.15:
            con.execute("UPDATE t_items SET excluded_reason='precio sospechoso' WHERE id=?", (r["id"],))
            continue
        con.execute("UPDATE t_items SET cand=1, cand_ref=?, cand_basis=? WHERE id=?", (ref, basis, r["id"]))
        n += 1
    return n


def make_deal(con, item, reason, now):
    pct = (item["cand_ref"] - item["price"]) / item["cand_ref"] * 100
    cur = con.execute(
        "INSERT OR IGNORE INTO t_deals(item_id,theme_id,product_key,kind,price,ref_price,pct,ts,reason,basis) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)", (item["id"], item["theme_id"], item["product_key"], "referencia",
                                         item["price"], item["cand_ref"], pct, now, reason, item["cand_basis"]))
    return cur.rowcount


# ---------------------------------------------------------------- pasos de IA

def llm_identify(settings):
    """Lote de anuncios recientes que las reglas no reconocieron."""
    now = int(time.time())
    with core.db() as con:
        items = con.execute("SELECT i.*, t.name AS theme_name FROM t_items i JOIN themes t ON t.id=i.theme_id "
                            "WHERE i.product_key IS NULL AND i.excluded_reason IS NULL AND i.llm_checked=0 "
                            "AND i.first_seen>=? ORDER BY i.first_seen DESC LIMIT 25", (now - 2 * 86400,)).fetchall()
        known = [r[0] for r in con.execute("SELECT key FROM products ORDER BY discovered DESC LIMIT 150")]
    if not items:
        return 0
    by_theme = {}
    for it in items:
        by_theme.setdefault(it["theme_name"], []).append(it)
    for theme_name, batch in by_theme.items():
        res = llm.identify_batch(settings, theme_name, batch, known)
        with core.db() as con:
            for n, it in enumerate(batch):
                o = res.get(n) or {}
                key = norm(o.get("key") or "").strip() or None
                kind = (o.get("kind") or "otros").strip().lower()
                if o.get("rel") is False:
                    reason = "no relacionado (IA)"
                elif o.get("acc"):
                    reason = "accesorio (IA)"
                elif not key:
                    reason = None
                else:
                    reason = item_reason(it["title"], it["description"], it["price"], 0, settings, kind)
                con.execute("UPDATE t_items SET llm_checked=1, product_key=?, kind=?, key_src=?, excluded_reason=? "
                            "WHERE id=?", (key if reason is None or not reason.endswith("(IA)") else None,
                                           kind, "ia" if key else None, reason, it["id"]))
                if key and not reason:
                    ensure_product(con, key, kind, "ia")
                    if not it["reserved"]:
                        add_sample(con, key, it["id"], it["price"], now, it["title"])
    log.info("[IA] identificados %d anuncios", len(items))
    return len(items)


def llm_estimate(settings):
    """Estima el valor de un producto con anuncios frescos pero sin datos suficientes."""
    now = int(time.time())
    with core.db() as con:
        p = con.execute(
            "SELECT p.* FROM products p WHERE p.status='pocos datos' AND (p.est_ts IS NULL OR p.est_ts<?) "
            "AND EXISTS (SELECT 1 FROM t_items i WHERE i.product_key=p.key AND i.excluded_reason IS NULL "
            "AND i.last_seen>=?) ORDER BY p.discovered DESC LIMIT 1", (now - 14 * 86400, now - 2 * 86400)).fetchone()
        if not p:
            return False
        samples = [(r["title"] or p["key"], r["price"]) for r in
                   con.execute("SELECT title, price FROM samples WHERE key=? ORDER BY ts DESC LIMIT 10", (p["key"],))]
    est = llm.estimate_price(settings, p["key"], samples)
    with core.db() as con:
        con.execute("UPDATE products SET est_price=?, est_low=?, est_high=?, est_conf=?, est_note=?, est_ts=? WHERE key=?",
                    (est.get("price"), est.get("low"), est.get("high"), str(est.get("confidence", "baja")).lower(),
                     est.get("note"), now, p["key"]))
        con.execute("UPDATE t_items SET eval_price=NULL WHERE product_key=?", (p["key"],))
    log.info("[IA] estimación %s: %s € (%s)", p["key"], est.get("price"), est.get("confidence"))
    return True


def llm_review(settings, limit=4):
    """Revisa los candidatos pendientes: solo los aprobados se convierten en chollo."""
    now = int(time.time())
    with core.db() as con:
        cands = con.execute("SELECT i.*, t.name AS theme_name FROM t_items i JOIN themes t ON t.id=i.theme_id "
                            "WHERE i.cand=1 AND i.excluded_reason IS NULL AND (i.reviewed_price IS NULL OR "
                            "i.reviewed_price<>i.price) ORDER BY i.first_seen DESC LIMIT ?", (limit,)).fetchall()
    new = 0
    for c in cands:
        amz = amazon_for(c["product_key"], settings)
        v = llm.review(settings, c["theme_name"], c, c["cand_ref"], c["cand_basis"], amz)
        with core.db() as con:
            con.execute("UPDATE t_items SET reviewed_price=?, llm_verdict=?, llm_reason=?, llm_value=? WHERE id=?",
                        (c["price"], v.get("verdict"), v.get("reason"), v.get("value"), c["id"]))
            if v.get("ok"):
                new += make_deal(con, c, v.get("reason"), now)
        log.info("[IA] revisión %s (%s €): %s — %s", c["product_key"], c["price"], v.get("verdict"), v.get("reason"))
    return new


def amazon_for(key, settings):
    """Precio nuevo en Amazon del producto (lo busca si no lo tenemos o tiene más de 30 días)."""
    with core.db() as con:
        p = con.execute("SELECT * FROM products WHERE key=?", (key,)).fetchone()
    if p is None:
        return None
    if amazon.stale(p) and amazon.available(settings):
        try:
            amazon.refresh("products", "key", key, key, settings, reference_query(key))
            with core.db() as con:
                p = con.execute("SELECT * FROM products WHERE key=?", (key,)).fetchone()
        except Exception:
            log.exception("Error consultando Amazon para %s", key)
    return {"price": p["amazon_price"], "title": p["amazon_title"] or key} if p["amazon_price"] else None


def promote_unreviewed(con, now):
    """Sin IA disponible: los candidatos con mediana real pasan directamente (las estimaciones no)."""
    new = 0
    for c in con.execute("SELECT * FROM t_items WHERE cand=1 AND excluded_reason IS NULL AND reviewed_price IS NULL "
                         "AND cand_basis LIKE 'mediana%'").fetchall():
        new += make_deal(con, c, "sin revisar por IA", now)
        con.execute("UPDATE t_items SET reviewed_price=? WHERE id=?", (c["price"], c["id"]))
    return new


def reclassify_all(settings):
    """Reaplica identificación y filtros a todo lo guardado (tras cambiar reglas o palabras)."""
    with core.db() as con:
        for r in con.execute("SELECT * FROM t_items").fetchall():
            ident = identify(r["title"])
            if ident:
                key, kind, pos, src = *ident, "reglas"
            elif r["key_src"] == "ia":
                key, kind, pos, src = r["product_key"], r["kind"], 0, "ia"
            else:
                key, kind, pos, src = None, None, 0, None
            if r["excluded_reason"] and r["excluded_reason"].endswith("(IA)"):
                continue
            reason = item_reason(r["title"], r["description"], r["price"], pos, settings, kind) if key else None
            con.execute("UPDATE t_items SET product_key=?,kind=?,key_src=?,excluded_reason=?,eval_price=NULL WHERE id=?",
                        (key, kind, src, reason, r["id"]))
            if key:
                con.execute("INSERT OR IGNORE INTO products(key,kind,query,source) VALUES(?,?,?,?)",
                            (key, kind, reference_query(key), src))


class ThemeWorker:
    """Hilo propio. Cada ~45 s: un paso contra Wallapop (alternando explorar y valorar) y, aparte,
    los pasos de IA (identificar, estimar, revisar), que no cuentan contra Wallapop."""

    def __init__(self):
        self.status = "arrancando"
        self.ia_status = ""
        self.last_tick = None
        self.turn = 0

    def tick(self):
        settings = core.get_settings()
        now = int(time.time())
        with core.db() as con:
            themes = con.execute("SELECT * FROM themes WHERE enabled=1 ORDER BY id").fetchall()
            product = next_reference(con, now)
        self.turn += 1
        if product and (self.turn % 2 == 0 or not themes):
            self.status = f"valorando {product['key']}"
            reference(product, settings)
        elif themes:
            theme = themes[(self.turn // 2) % len(themes)]
            self.status = f"explorando {theme['name']}"
            discover(theme, settings)

        new = 0
        if llm.available(settings):
            try:
                self.ia_status = "IA: identificando"
                llm_identify(settings)
                self.ia_status = "IA: estimando precios"
                llm_estimate(settings)
                with core.db() as con:
                    evaluate(con, now)
                self.ia_status = "IA: revisando candidatos"
                new = llm_review(settings)
                self.ia_status = f"IA activa · gastado hoy {llm.spent_today(core.get_settings()):.3f} $"
            except llm.BudgetExceeded:
                self.ia_status = "IA en pausa: presupuesto diario agotado"
                with core.db() as con:
                    evaluate(con, now)
                    new = promote_unreviewed(con, now)
            except Exception as e:
                log.exception("Error de IA")
                self.ia_status = f"IA con error: {str(e)[:120]}"
        else:
            self.ia_status = "IA desactivada (sin clave de OpenRouter)"
            with core.db() as con:
                evaluate(con, now)
                new = promote_unreviewed(con, now)
        if new:
            log.info("[temas] %d chollos nuevos", new)
            core.notify(settings)
        self.status = "en espera"
        self.last_tick = int(time.time())

    def loop(self):
        while True:
            try:
                self.tick()
            except Exception as e:
                log.exception("Error en temas")
                self.status = f"error: {e}"
            time.sleep(int(core.get_settings().get("theme_tick_s", 45)) + random.uniform(0, 15))

    def start(self):
        threading.Thread(target=self.loop, daemon=True).start()
