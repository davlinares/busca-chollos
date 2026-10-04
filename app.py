"""Interfaz web de Wallapop chollos."""
import logging
import time
from datetime import datetime

from flask import Flask, flash, redirect, render_template_string, request, url_for

import amazon
import core
import llm
import themes
import ui_themes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.FileHandler("/opt/wallapop-chollos/chollos.log"), logging.StreamHandler()])

app = Flask(__name__)
app.secret_key = "wallapop-chollos-lan"
core.init_db()
themes.init_db()
sched = core.Scheduler()
tworker = themes.ThemeWorker()


@app.template_filter("dt")
def fmt_dt(ts):
    return datetime.fromtimestamp(ts).strftime("%d/%m %H:%M") if ts else "—"


@app.template_filter("ago")
def fmt_ago(ts):
    if not ts:
        return "—"
    d = int(time.time()) - int(ts)
    if d < 0:
        d = -d
        return f"en {d // 60} min" if d >= 60 else f"en {d} s"
    if d < 3600:
        return f"hace {d // 60} min"
    if d < 86400:
        return f"hace {d // 3600} h"
    return f"hace {d // 86400} d"


BASE = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Wallapop Chollos</title>
<style>
:root{--bg:#f4f6f7;--card:#fff;--fg:#1d2b2a;--mut:#6b7b7a;--acc:#13c1ac;--acc2:#0b7a6f;--red:#d64541;--bd:#e0e6e5}
@media (prefers-color-scheme:dark){:root{--bg:#121716;--card:#1b2221;--fg:#e4ecea;--mut:#8fa09e;--bd:#2c3634;--acc2:#4fd8c6}}
*{box-sizing:border-box}body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}
header{background:var(--card);border-bottom:1px solid var(--bd);padding:10px 16px;display:flex;flex-wrap:wrap;gap:12px;align-items:center}
header h1{font-size:18px;margin:0;color:var(--acc2)}nav a{margin-right:14px;color:var(--fg);text-decoration:none;font-weight:500}
nav a.on{color:var(--acc2);border-bottom:2px solid var(--acc)}.st{margin-left:auto;font-size:13px;color:var(--mut)}
main{max-width:1200px;margin:0 auto;padding:16px}
.btn{background:var(--acc);color:#fff;border:0;border-radius:6px;padding:7px 12px;cursor:pointer;font-size:14px;text-decoration:none;display:inline-block}
.btn.sec{background:transparent;color:var(--fg);border:1px solid var(--bd)}.btn.red{background:var(--red)}.btn.sm{padding:4px 8px;font-size:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;overflow:hidden;display:flex;flex-direction:column}
.card img{width:100%;height:180px;object-fit:cover;background:var(--bd)}.card .b{padding:10px;flex:1;display:flex;flex-direction:column;gap:4px}
.card a.t{color:var(--fg);font-weight:600;text-decoration:none}.price{font-size:22px;font-weight:700}
.tag{display:inline-block;font-size:12px;padding:2px 7px;border-radius:10px;color:#fff;background:var(--red)}.tag.m{background:#8e44ad}
.mut{color:var(--mut);font-size:13px}table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--bd);border-radius:8px}
th,td{padding:7px 9px;border-bottom:1px solid var(--bd);text-align:left;font-size:14px;vertical-align:top}th{font-size:12px;text-transform:uppercase;color:var(--mut)}
.box{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:14px;margin-bottom:16px}
label{display:block;font-size:13px;color:var(--mut);margin:8px 0 3px}input,textarea,select{width:100%;padding:7px;border:1px solid var(--bd);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit}
.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}.flash{background:#fff3cd;color:#664d03;padding:8px 12px;border-radius:6px;margin-bottom:12px}
.wrap{overflow-x:auto}.chk{display:flex;gap:6px;align-items:center}.chk input{width:auto}
.filters{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px;align-items:center}.filters select{width:auto}
.src-w{background:#e35c21}.src-m{background:#19a86f}.src-e{background:#0064d2}
.sbadge{display:inline-block;font-size:11px;padding:2px 7px;border-radius:10px;color:#fff;margin-right:3px;vertical-align:middle}
.cnew{color:#1a7a5a;font-size:12px;font-weight:600}
</style></head><body>
<header><h1>🔥 Wallapop Chollos</h1><nav>
<a href="/" class="{{'on' if page=='deals'}}">Chollos</a><a href="/busquedas" class="{{'on' if page=='searches'}}">Búsquedas</a>
<a href="/temas" class="{{'on' if page=='themes'}}">Temas</a><a href="/precios" class="{{'on' if page=='prices'}}">Precios</a>
<a href="/ajustes" class="{{'on' if page=='settings'}}">Ajustes</a><a href="/registro" class="{{'on' if page=='log'}}">Registro</a></nav>
<div class="st">{% if st.running %}⏳ Buscando: {{st.current}}{% else %}Último ciclo {{st.last_cycle|ago}} · próximo {{st.next_cycle|ago}}{% endif %}
<form method="post" action="/run" style="display:inline"><button class="btn sm">Buscar ahora</button></form></div></header>
<main>{% for m in get_flashed_messages() %}<div class="flash">{{m}}</div>{% endfor %}
{% if st.last_error %}<div class="flash">⚠️ {{st.last_error}}</div>{% endif %}
{{ body|safe }}</main></body></html>"""


def page(name, tpl, **ctx):
    body = render_template_string(tpl, **ctx)
    return render_template_string(BASE, body=body, page=name, st=sched)


DEALS = """
<form class="filters" method="get">
<select name="s" onchange="this.form.submit()"><option value="">Todas las búsquedas y temas</option>
{% for s in searches %}<option value="s:{{s.id}}" {{'selected' if sel=='s:'~s.id}}>🔎 {{s.name}}</option>{% endfor %}
{% for t in themes %}<option value="t:{{t.id}}" {{'selected' if sel=='t:'~t.id}}>🧭 {{t.name}}</option>{% endfor %}</select>
<select name="k" onchange="this.form.submit()"><option value="">Todos los tipos</option>
<option value="mediana" {{'selected' if kind=='mediana'}}>Bajo la mediana (búsquedas)</option>
<option value="referencia" {{'selected' if kind=='referencia'}}>Bajo lo habitual (temas)</option>
<option value="bajada" {{'selected' if kind=='bajada'}}>Bajadas de precio</option></select>
<label class="chk"><input type="checkbox" name="d" value="1" {{'checked' if show_dis}} onchange="this.form.submit()"> ver descartados</label>
<span class="mut">{{deals|length}} chollos</span></form>
{% if not deals %}<div class="box">Aún no hay chollos. Los temas necesitan unas horas para construir su base de precios (ver <a href="/precios">Precios</a>).</div>{% endif %}
<div class="grid">{% for d in deals %}<div class="card" style="{{'opacity:.45' if d.dismissed or d.reserved}}">
<a href="{{d.url}}" target="_blank">{% if d.image %}<img src="{{d.image}}" loading="lazy">{% endif %}</a><div class="b">
<a class="t" href="{{d.url}}" target="_blank">{{d.title}}</a>
<div><span class="price">{{'%.0f'|format(d.price)}} €</span>
<span class="tag {{'m' if d.kind!='bajada'}}">{{tag(d)}}</span></div>
{% if d.reason %}<div style="font-size:13px">💬 {{d.reason}}</div>{% endif %}
{% if d.amazon_price %}<div style="font-size:13px"><a href="{{d.amazon_url}}" target="_blank" style="color:#e47911">🛒 Nuevo en Amazon: {{'%.0f'|format(d.amazon_price)}} €</a></div>{% endif %}
<div class="mut">
{%- set sc = d.get('source','wallapop') if d is mapping else 'wallapop' -%}
{%- set sc_cls = 'src-m' if sc=='milanuncios' else ('src-e' if sc=='ebay' else 'src-w') -%}
{%- set sc_lbl = 'Milanuncios' if sc=='milanuncios' else ('eBay' if sc=='ebay' else 'Wallapop') -%}
<span class="sbadge {{sc_cls}}">{{sc_lbl}}</span>
{{'🧭' if d.src=='t' else '🔎'}} {{d.search_name}} · {{d.city or ''}}
{%- if d.get('condition')=='new' %} · <span class="cnew">Nuevo</span>{% endif %}
{%- if d.reserved %} · RESERVADO{% endif %}</div>
<div class="mut">Detectado {{d.ts|ago}} {% if d.notified==1 %}· 📧{% endif %}</div>
<div style="margin-top:auto;padding-top:6px"><form method="post" action="/deal/{{d.src}}/{{d.id}}/dismiss">
<button class="btn sec sm">{{'Recuperar' if d.dismissed else 'Descartar'}}</button></form></div></div></div>{% endfor %}</div>"""


@app.route("/")
def deals():
    sel = request.args.get("s", "")
    kind = request.args.get("k", "")
    show_dis = request.args.get("d") == "1"
    src, _, sid = sel.partition(":")
    rows = []
    with core.db() as con:
        if src in ("", "s") and kind != "referencia":
            q = ("SELECT d.*, i.title, i.url, i.image, i.city, i.reserved, i.source, i.condition, s.name AS search_name, "
                 "s.amazon_price, s.amazon_url FROM deals d "
                 "JOIN items i ON i.id=d.item_id AND i.search_id=d.search_id JOIN searches s ON s.id=d.search_id "
                 "WHERE i.excluded=0")
            args = []
            if sid:
                q += " AND d.search_id=?"
                args.append(int(sid))
            if kind:
                q += " AND d.kind=?"
                args.append(kind)
            if not show_dis:
                q += " AND d.dismissed=0"
            rows += [dict(r, src="s") for r in con.execute(q + " ORDER BY d.ts DESC LIMIT 300", args)]
        if src in ("", "t") and kind != "mediana":
            q = ("SELECT d.*, d.product_key AS grp, i.title, i.url, i.image, i.city, i.reserved, 'wallapop' AS source, 'used' AS condition, th.name AS search_name, "
                 "p.amazon_price, p.amazon_url FROM t_deals d JOIN t_items i ON i.id=d.item_id "
                 "JOIN themes th ON th.id=d.theme_id LEFT JOIN products p ON p.key=d.product_key "
                 "WHERE i.excluded_reason IS NULL")
            args = []
            if sid:
                q += " AND d.theme_id=?"
                args.append(int(sid))
            if kind:
                q += " AND d.kind=?"
                args.append(kind)
            if not show_dis:
                q += " AND d.dismissed=0"
            rows += [dict(r, src="t") for r in con.execute(q + " ORDER BY d.ts DESC LIMIT 300", args)]
        searches = con.execute("SELECT id,name FROM searches ORDER BY name").fetchall()
        theme_list = con.execute("SELECT id,name FROM themes ORDER BY name").fetchall()
    best = {}
    for d in rows:  # una tarjeta por anuncio, con su mayor descuento
        k = (d["src"], d["item_id"])
        if k not in best or d["pct"] > best[k]["pct"]:
            best[k] = d
    rows = sorted(best.values(), key=lambda d: (-d["ts"], -d["pct"]))
    return page("deals", DEALS, deals=rows[:300], searches=searches, themes=theme_list, sel=sel, kind=kind,
                show_dis=show_dis, tag=core.deal_tag)


@app.post("/deal/<src>/<int:did>/dismiss")
def dismiss(src, did):
    table = "t_deals" if src == "t" else "deals"
    with core.db() as con:
        con.execute(f"UPDATE {table} SET dismissed=1-dismissed WHERE id=?", (did,))
    return redirect(request.referrer or "/")


SEARCHES = """
<div class="box"><h3 style="margin-top:0">{{'Editar' if edit else 'Nueva'}} búsqueda</h3>
<form method="post" action="/busquedas/guardar"><input type="hidden" name="id" value="{{edit.id if edit}}">
<div class="row"><div><label>Nombre</label><input name="name" required value="{{edit.name if edit}}" placeholder="iPhone 13"></div>
<div><label>Palabras clave (lo que escribirías en Wallapop)</label><input name="keywords" required value="{{edit.keywords if edit}}" placeholder="iphone 13"></div></div>
<div class="row"><div><label>Precio mínimo €</label><input name="min_price" type="number" step="1" value="{{edit.min_price|int if edit and edit.min_price}}"></div>
<div><label>Precio máximo €</label><input name="max_price" type="number" step="1" value="{{edit.max_price|int if edit and edit.max_price}}"></div>
<div><label>Distancia km (vacío = toda España)</label><input name="distance_km" type="number" value="{{edit.distance_km if edit and edit.distance_km}}"></div>
<div><label>Páginas por búsqueda (40 anuncios c/u)</label><input name="max_pages" type="number" min="1" max="10" value="{{edit.max_pages if edit else 3}}"></div></div>
<div class="row"><div><label>% por debajo de la mediana</label><input name="below_median_pct" type="number" step="1" value="{{edit.below_median_pct|int if edit else 40}}"></div>
<div><label>% de bajada de precio</label><input name="drop_pct" type="number" step="1" value="{{edit.drop_pct|int if edit else 15}}"></div></div>
<label>Excluir si el título contiene (solo esta búsqueda, separadas por comas; p.ej. variantes que no quieres)</label>
<input name="exclude_words" value="{{edit.exclude_words if edit}}" placeholder="mini, pro max, 64gb">
<label class="chk"><input type="checkbox" name="title_must_match" value="1" {{'checked' if not edit or edit.title_must_match}}> El título debe contener todas las palabras clave (quita accesorios y anuncios que no son)</label>
<label class="chk"><input type="checkbox" name="enabled" value="1" {{'checked' if not edit or edit.enabled}}> Activa</label>
<p><button class="btn">Guardar</button> {% if edit %}<a class="btn sec" href="/busquedas">Cancelar</a>{% endif %}</p></form></div>
<div class="wrap"><table><tr><th>Búsqueda</th><th>Filtros</th><th>Umbrales</th><th>Última</th><th>Válidos</th><th>Mediana</th><th>Chollos</th><th></th></tr>
{% for s in searches %}<tr style="{{'opacity:.5' if not s.enabled}}"><td><a href="/busqueda/{{s.id}}"><b>{{s.name}}</b></a><div class="mut">"{{s.keywords}}"</div></td>
<td class="mut">{{s.min_price|int if s.min_price else 0}}–{{s.max_price|int if s.max_price else '∞'}} € · {{(s.distance_km|string + ' km') if s.distance_km else 'España'}}</td>
<td class="mut">-{{s.below_median_pct|int}}% med · -{{s.drop_pct|int}}% bajada</td>
<td class="mut">{{s.last_run|ago}}</td><td>{{s.last_count or 0}}</td><td>{{('%.0f €'|format(s.last_median)) if s.last_median else '—'}}</td><td>{{s.n_deals}}</td>
<td style="white-space:nowrap"><form method="post" action="/run" style="display:inline"><input type="hidden" name="sid" value="{{s.id}}"><button class="btn sm">▶</button></form>
<a class="btn sec sm" href="/busquedas?edit={{s.id}}">Editar</a>
<form method="post" action="/busquedas/{{s.id}}/borrar" style="display:inline" onsubmit="return confirm('¿Borrar búsqueda y su histórico?')"><button class="btn red sm">✕</button></form></td></tr>
{% else %}<tr><td colspan="8" class="mut">Sin búsquedas todavía.</td></tr>{% endfor %}</table></div>"""


@app.route("/busquedas")
def searches():
    with core.db() as con:
        rows = con.execute("SELECT s.*, (SELECT COUNT(*) FROM deals d WHERE d.search_id=s.id) n_deals "
                           "FROM searches s ORDER BY s.name").fetchall()
        eid = request.args.get("edit", type=int)
        edit = con.execute("SELECT * FROM searches WHERE id=?", (eid,)).fetchone() if eid else None
    return page("searches", SEARCHES, searches=rows, edit=edit)


def num(v, cast=float):
    v = (v or "").strip()
    return cast(v) if v else None


@app.post("/busquedas/guardar")
def save_search():
    f = request.form
    vals = (f["name"].strip(), f["keywords"].strip(), num(f.get("min_price")), num(f.get("max_price")),
            num(f.get("distance_km"), int), num(f.get("below_median_pct")) or 40, num(f.get("drop_pct")) or 15,
            f.get("exclude_words", "").strip(), 1 if f.get("title_must_match") else 0,
            num(f.get("max_pages"), int) or 3, 1 if f.get("enabled") else 0)
    with core.db() as con:
        if f.get("id"):
            old = con.execute("SELECT keywords FROM searches WHERE id=?", (int(f["id"]),)).fetchone()
            if old and core.norm(old["keywords"]).strip() != core.norm(vals[1]).strip():
                # Otras palabras clave = otros anuncios: el histórico viejo falsearía la mediana
                for t in ("deals", "price_history", "items"):
                    con.execute(f"DELETE FROM {t} WHERE search_id=?", (int(f["id"]),))
            con.execute("UPDATE searches SET name=?,keywords=?,min_price=?,max_price=?,distance_km=?,below_median_pct=?,"
                        "drop_pct=?,exclude_words=?,title_must_match=?,max_pages=?,enabled=? WHERE id=?",
                        (*vals, int(f["id"])))
            sid = int(f["id"])
        else:
            sid = con.execute("INSERT INTO searches(name,keywords,min_price,max_price,distance_km,below_median_pct,"
                              "drop_pct,exclude_words,title_must_match,max_pages,enabled,created_at) "
                              "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (*vals, int(time.time()))).lastrowid
    if not sched.running:
        sched.trigger(sid)
        flash("Guardada. Lanzando la búsqueda…")
    else:
        flash("Guardada. Se ejecutará en el próximo ciclo.")
    return redirect(url_for("searches"))


@app.post("/busquedas/<int:sid>/borrar")
def delete_search(sid):
    with core.db() as con:
        for t in ("deals", "price_history", "items", "runs"):
            con.execute(f"DELETE FROM {t} WHERE search_id=?", (sid,))
        con.execute("DELETE FROM searches WHERE id=?", (sid,))
    flash("Búsqueda borrada.")
    return redirect(url_for("searches"))


DETAIL = """
<p><a href="/busquedas">← Búsquedas</a></p>
<div class="box"><h2 style="margin:0">{{s.name}} <span class="mut">"{{s.keywords}}"</span></h2>
<p class="mut">Mediana actual: <b>{{('%.0f €'|format(s.last_median)) if s.last_median else 'sin datos suficientes'}}</b>
{% if s.last_median %}· umbral chollo ≤ {{'%.0f'|format(s.last_median*(1-s.below_median_pct/100))}} €{% endif %}
· {{valid}} anuncios válidos · {{excl}} excluidos</p></div>
<div class="filters"><a class="btn sm {{'' if not show_ex else 'sec'}}" href="?">Válidos</a><a class="btn sm {{'' if show_ex else 'sec'}}" href="?ex=1">Excluidos</a></div>
<div class="wrap"><table><tr><th></th><th>Anuncio</th><th>Precio</th><th>Histórico</th><th>Ciudad</th><th>Visto</th>{% if show_ex %}<th>Motivo</th>{% endif %}</tr>
{% for i in items %}<tr style="{{'opacity:.5' if i.reserved}}"><td>{% if i.image %}<img src="{{i.image}}" width="54" height="54" style="object-fit:cover;border-radius:4px" loading="lazy">{% endif %}</td>
<td><a href="{{i.url}}" target="_blank">{{i.title}}</a>{% if i.reserved %} <span class="mut">(reservado)</span>{% endif %}</td>
<td><b>{{'%.0f'|format(i.price)}} €</b>{% if s.last_median and not show_ex %}<div class="mut">{{'%+.0f'|format((i.price/s.last_median-1)*100)}}%</div>{% endif %}</td>
<td class="mut">{{hist.get(i.id, '')}}</td><td class="mut">{{i.city}}</td><td class="mut">{{i.last_seen|ago}}</td>
{% if show_ex %}<td class="mut">{{i.excluded_reason}}</td>{% endif %}</tr>{% endfor %}</table></div>"""


@app.route("/busqueda/<int:sid>")
def search_detail(sid):
    show_ex = request.args.get("ex") == "1"
    with core.db() as con:
        s = con.execute("SELECT * FROM searches WHERE id=?", (sid,)).fetchone()
        if not s:
            return redirect("/busquedas")
        items = con.execute("SELECT * FROM items WHERE search_id=? AND excluded=? ORDER BY last_seen DESC, price ASC "
                            "LIMIT 500", (sid, 1 if show_ex else 0)).fetchall()
        valid = con.execute("SELECT COUNT(*) FROM items WHERE search_id=? AND excluded=0", (sid,)).fetchone()[0]
        excl = con.execute("SELECT COUNT(*) FROM items WHERE search_id=? AND excluded=1", (sid,)).fetchone()[0]
        hist = {}
        for r in con.execute("SELECT item_id, GROUP_CONCAT(CAST(price AS INT), ' → ') h, COUNT(*) n FROM "
                             "(SELECT * FROM price_history WHERE search_id=? ORDER BY ts) GROUP BY item_id HAVING n>1",
                             (sid,)):
            hist[r["item_id"]] = r["h"] + " €"
    return page("searches", DETAIL, s=s, items=items, valid=valid, excl=excl, hist=hist, show_ex=show_ex)


SETTINGS = """
<form method="post"><div class="box"><h3 style="margin-top:0">General</h3>
<div class="row"><div><label>Cada cuántos minutos buscar</label><input name="interval_min" type="number" min="5" value="{{s.interval_min}}"></div>
<div><label>Latitud (centro de búsqueda)</label><input name="latitude" value="{{s.latitude}}"></div>
<div><label>Longitud</label><input name="longitude" value="{{s.longitude}}"></div></div>
<div class="row"><div><label>Mínimo de anuncios para calcular mediana</label><input name="min_sample" type="number" value="{{s.min_sample}}"></div>
<div><label>Días de anuncios para la mediana</label><input name="median_window_days" type="number" value="{{s.median_window_days}}"></div></div>
<label>Excluir si aparece en el título o la descripción (separadas por comas; ignora tildes/mayúsculas y negaciones tipo "sin roturas")</label>
<textarea name="exclude_words" rows="3">{{s.exclude_words}}</textarea>
<label>Excluir solo si aparece en el título (palabras que en la descripción suelen ser inocentes: "incluye funda", "por cambio de modelo")</label>
<textarea name="exclude_title_words" rows="3">{{s.exclude_title_words}}</textarea>
<label>Accesorios: excluir si aparecen en el título ANTES del producto ("Caja para Raspberry Pi 5" fuera; "Raspberry Pi 5 con caja" se queda)</label>
<textarea name="accessory_words" rows="3">{{s.accessory_words}}</textarea></div>
<div class="box"><h3 style="margin-top:0">Email</h3>
<p class="mut">Con Gmail: usa una <a href="https://myaccount.google.com/apppasswords" target="_blank">contraseña de aplicación</a> (no tu contraseña normal), servidor smtp.gmail.com, puerto 587, STARTTLS.</p>
<label class="chk"><input type="checkbox" name="email_enabled" value="1" {{'checked' if s.email_enabled=='1'}}> Enviar emails con los chollos nuevos</label>
<div class="row"><div><label>Cuándo enviar</label><select name="email_mode">
<option value="diario" {{'selected' if s.email_mode=='diario'}}>Un resumen al día</option>
<option value="inmediato" {{'selected' if s.email_mode=='inmediato'}}>En cuanto aparezcan</option></select></div>
<div><label>Hora del resumen diario (0-23)</label><input name="email_hour" type="number" min="0" max="23" value="{{s.email_hour}}"></div>
<div><label>Último resumen</label><input value="{{s.digest_last or '—'}}" disabled></div></div>
<div class="row"><div><label>Servidor SMTP</label><input name="smtp_host" value="{{s.smtp_host}}"></div>
<div><label>Puerto</label><input name="smtp_port" value="{{s.smtp_port}}"></div>
<div><label>Seguridad</label><select name="smtp_security">{% for o in ['starttls','ssl','none'] %}<option {{'selected' if s.smtp_security==o}}>{{o}}</option>{% endfor %}</select></div></div>
<div class="row"><div><label>Usuario SMTP</label><input name="smtp_user" value="{{s.smtp_user}}" autocomplete="off"></div>
<div><label>Contraseña SMTP {% if s.smtp_pass %}(guardada; déjala vacía para mantenerla){% endif %}</label><input name="smtp_pass" type="password" autocomplete="new-password"></div></div>
<div class="row"><div><label>Remitente (vacío = usuario SMTP)</label><input name="smtp_from" value="{{s.smtp_from}}"></div>
<div><label>Enviar a (varios separados por coma)</label><input name="email_to" value="{{s.email_to}}"></div>
<div><label>No enviar chollos con más de (horas)</label><input name="notify_max_age_h" type="number" value="{{s.notify_max_age_h}}"></div></div></div>
<div class="box"><h3 style="margin-top:0">IA (OpenRouter)</h3>
<p class="mut">Se usa en los temas: identifica anuncios que las reglas no reconocen, estima el precio de productos con pocos anuncios
y revisa cada candidato antes de avisarte. Gastado hoy: <b>{{'%.3f'|format(spent_today)}} $</b> · total: {{'%.3f'|format(s.llm_spent_total|float)}} $</p>
<label class="chk"><input type="checkbox" name="llm_enabled" value="1" {{'checked' if s.llm_enabled=='1'}}> Usar IA</label>
<div class="row"><div><label>Clave OpenRouter {% if s.openrouter_key %}(guardada, termina en …{{s.openrouter_key[-3:]}}; vacío = mantener){% endif %}</label>
<input name="openrouter_key" type="password" autocomplete="new-password"></div>
<div><label>Presupuesto diario máximo ($)</label><input name="llm_daily_budget" value="{{s.llm_daily_budget}}"></div></div>
<div class="row"><div><label>Modelo rápido (identificar en lote)</label><input name="llm_model_fast" value="{{s.llm_model_fast}}"></div>
<div><label>Modelo listo (estimar precios y revisar candidatos)</label><input name="llm_model_smart" value="{{s.llm_model_smart}}"></div></div></div>
<div class="box"><h3 style="margin-top:0">Amazon (SerpAPI)</h3>
<p class="mut">Busca el precio nuevo en Amazon.es de los productos con un candidato a chollo (se guarda 30 días) y se lo pasa a la IA al revisar.
El plan gratuito de SerpAPI son 250 búsquedas/mes <b>compartidas con el SEO Checker</b>. Usadas este mes por esta app: <b>{{serp_used}}</b> de {{s.amazon_monthly_limit}}.</p>
<label class="chk"><input type="checkbox" name="amazon_enabled" value="1" {{'checked' if s.amazon_enabled=='1'}}> Consultar Amazon</label>
<div class="row"><div><label>Clave SerpAPI {% if s.serpapi_key %}(guardada; vacío = mantener){% endif %}</label><input name="serpapi_key" type="password" autocomplete="new-password"></div>
<div><label>Máximo de búsquedas al mes</label><input name="amazon_monthly_limit" type="number" value="{{s.amazon_monthly_limit}}"></div></div></div>
<div class="box"><h3 style="margin-top:0">Plataformas adicionales</h3>
<p class="mut">Además de Wallapop, busca en Milanuncios y eBay con las mismas palabras clave. Cada resultado indica su plataforma de origen.</p>
<label class="chk"><input type="checkbox" name="milanuncios_enabled" value="1" {{'checked' if s.milanuncios_enabled=='1'}}> Buscar en Milanuncios</label>
<div class="box" style="margin-top:10px;background:var(--bg)"><label class="chk"><input type="checkbox" name="ebay_enabled" value="1" {{'checked' if s.ebay_enabled=='1'}}> Buscar en eBay España</label>
<p class="mut">Necesita una cuenta gratuita de desarrollador en <a href="https://developer.ebay.com" target="_blank">developer.ebay.com</a> → Create App → copia el App ID (Client ID) y Cert ID (Client Secret).</p>
<div class="row"><div><label>App ID (Client ID) {% if s.ebay_app_id %}(guardado, termina en …{{s.ebay_app_id[-6:]}}; vacío = mantener){% endif %}</label>
<input name="ebay_app_id" type="password" autocomplete="new-password"></div>
<div><label>Cert ID (Client Secret) {% if s.ebay_cert_id %}(guardado; vacío = mantener){% endif %}</label>
<input name="ebay_cert_id" type="password" autocomplete="new-password"></div></div></div></div>
<button class="btn">Guardar ajustes</button> <button class="btn sec" formaction="/ajustes/test">Guardar y enviar email de prueba</button>
<button class="btn sec" formaction="/ajustes/resumen">Guardar y enviar el resumen ahora</button></form>"""

SETTING_KEYS = ["interval_min", "latitude", "longitude", "min_sample", "median_window_days", "exclude_words", "exclude_title_words", "accessory_words",
                "smtp_host", "smtp_port", "smtp_security", "smtp_user", "smtp_from", "email_to", "notify_max_age_h"]


def save_settings_form():
    f = request.form
    vals = {k: f.get(k, "").strip() for k in SETTING_KEYS}
    vals["email_enabled"] = "1" if f.get("email_enabled") else "0"
    vals["llm_enabled"] = "1" if f.get("llm_enabled") else "0"
    vals["amazon_enabled"] = "1" if f.get("amazon_enabled") else "0"
    vals["milanuncios_enabled"] = "1" if f.get("milanuncios_enabled") else "0"
    vals["ebay_enabled"] = "1" if f.get("ebay_enabled") else "0"
    if f.get("ebay_app_id"):
        vals["ebay_app_id"] = f["ebay_app_id"].strip()
    if f.get("ebay_cert_id"):
        vals["ebay_cert_id"] = f["ebay_cert_id"].strip()
    for k in ("email_mode", "email_hour", "amazon_monthly_limit"):
        if f.get(k):
            vals[k] = f[k].strip()
    if f.get("serpapi_key"):
        vals["serpapi_key"] = f["serpapi_key"].strip()
    for k in ("llm_daily_budget", "llm_model_fast", "llm_model_smart"):
        if f.get(k):
            vals[k] = f[k].strip()
    if f.get("openrouter_key"):
        vals["openrouter_key"] = f["openrouter_key"].strip()
    if f.get("smtp_pass"):
        vals["smtp_pass"] = f["smtp_pass"]
    core.set_settings(vals)


@app.route("/ajustes", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        save_settings_form()
        sched.wake.set()  # aplica el nuevo intervalo
        flash("Ajustes guardados.")
        return redirect(url_for("settings"))
    s = core.get_settings()
    return page("settings", SETTINGS, s=s, spent_today=llm.spent_today(s), serp_used=amazon.used_this_month(s))


@app.post("/ajustes/resumen")
def send_digest_now():
    save_settings_form()
    try:
        n = core.notify(core.get_settings(), force=True)
        flash(f"Resumen enviado con {n} chollo(s)." if n else "No hay chollos pendientes de enviar.")
    except Exception as e:
        flash(f"Error enviando el resumen: {e}")
    return redirect(url_for("settings"))


@app.post("/ajustes/test")
def test_email():
    save_settings_form()
    try:
        core.send_email(core.get_settings(), "✅ Prueba de Wallapop Chollos",
                        "<p>Si ves esto, el envío de emails funciona.</p>")
        flash("Email de prueba enviado.")
    except Exception as e:
        flash(f"Error enviando email: {e}")
    return redirect(url_for("settings"))


LOG = """<div class="wrap"><table><tr><th>Fecha</th><th>Búsqueda</th><th>Anuncios</th><th>Válidos</th><th>Mediana</th><th>Chollos nuevos</th><th>Error</th></tr>
{% for r in runs %}<tr><td class="mut">{{r.ts|dt}}</td><td>{{r.name or '—'}}</td><td>{{r.fetched or ''}}</td><td>{{r.kept or ''}}</td>
<td>{{('%.0f €'|format(r.median)) if r.median else ''}}</td><td>{{r.deals if r.deals is not none else ''}}</td><td style="color:var(--red)">{{r.error or ''}}</td></tr>{% endfor %}</table></div>"""


@app.route("/registro")
def runlog():
    with core.db() as con:
        runs = con.execute("SELECT r.*, s.name FROM runs r LEFT JOIN searches s ON s.id=r.search_id "
                           "ORDER BY r.id DESC LIMIT 200").fetchall()
    return page("log", LOG, runs=runs)


@app.post("/run")
def run_now():
    if sched.running:
        flash("Ya hay una búsqueda en marcha.")
    else:
        sched.trigger(request.form.get("sid", type=int))
        flash("Búsqueda lanzada. Recarga en unos segundos.")
    return redirect(request.referrer or "/")


ui_themes.register(app, page, num, tworker)

if __name__ == "__main__":
    sched.start()
    core.Digest().start()
    tworker.start()
    from waitress import serve
    serve(app, host="0.0.0.0", port=8080, threads=4)
