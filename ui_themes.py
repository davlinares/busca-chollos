"""Páginas web de temas y base de precios."""
import time

from flask import flash, redirect, request, url_for

import core
import themes

THEMES = """
<div class="box"><h3 style="margin-top:0">{{'Editar tema' if edit else 'Nuevo tema'}}</h3>
<p class="mut">Un tema recorre por turnos estas búsquedas (solo anuncios recién publicados), reconoce qué producto es cada
anuncio y lo compara con el precio habitual de ese producto, que obtiene buscándolo aparte.</p>
<form method="post" action="/temas/guardar"><input type="hidden" name="id" value="{{edit.id if edit}}">
<div class="row"><div><label>Nombre</label><input name="name" required value="{{edit.name if edit}}" placeholder="Homelab"></div>
<div><label>% por debajo de lo habitual</label><input name="below_pct" type="number" value="{{edit.below_pct|int if edit else 35}}"></div>
<div><label>% de bajada de precio</label><input name="drop_pct" type="number" value="{{edit.drop_pct|int if edit else 15}}"></div>
<div><label>Ahorro mínimo en €</label><input name="min_saving" type="number" value="{{(edit.min_saving or 0)|int if edit else 20}}"></div></div>
<label>Búsquedas de exploración (una por línea)</label><textarea name="keywords" rows="10" required>{{edit.keywords if edit}}</textarea>
<label class="chk"><input type="checkbox" name="enabled" value="1" {{'checked' if not edit or edit.enabled}}> Activo</label>
<p><button class="btn">Guardar</button> {% if edit %}<a class="btn sec" href="/temas">Cancelar</a>{% endif %}</p></form></div>
<p class="mut">Explorador: {{tw.status}} · último paso {{tw.last_tick|ago}} · {{tw.ia_status}}</p>
<div class="wrap"><table><tr><th>Tema</th><th>Búsquedas</th><th>Anuncios vistos</th><th>Identificados</th><th>Chollos</th><th>Último</th><th></th></tr>
{% for t in themes %}<tr style="{{'opacity:.5' if not t.enabled}}"><td><a href="/tema/{{t.id}}"><b>{{t.name}}</b></a></td>
<td class="mut">{{t.nkw}} · vuelta {{t.cursor // t.nkw if t.nkw else 0}}, va por la {{(t.cursor % t.nkw) + 1 if t.nkw else 0}}</td>
<td>{{t.seen}}</td><td>{{t.ident}} {% if t.seen %}<span class="mut">({{(100*t.ident/t.seen)|round|int}}%)</span>{% endif %}</td><td>{{t.ndeals}}</td>
<td class="mut">{{t.last_run|ago}}</td>
<td style="white-space:nowrap"><a class="btn sec sm" href="/temas?edit={{t.id}}">Editar</a>
<form method="post" action="/temas/{{t.id}}/borrar" style="display:inline" onsubmit="return confirm('¿Borrar el tema y sus anuncios?')"><button class="btn red sm">✕</button></form></td></tr>
{% endfor %}</table></div>
<form method="post" action="/temas/reclasificar" style="margin-top:12px"><button class="btn sec sm">Reclasificar todos los anuncios</button>
<span class="mut">(vuelve a aplicar identificación y filtros; útil tras cambiar palabras en Ajustes)</span></form>"""

THEME = """
<p><a href="/temas">← Temas</a></p><h2 style="margin-top:0">{{t.name}}</h2>
<div class="filters">{% for v,l in [('','Identificados'),('cand','Revisados por IA'),('sin','Sin identificar'),('ex','Excluidos')] %}
<a class="btn sm {{'' if f==v else 'sec'}}" href="?f={{v}}">{{l}}</a>{% endfor %}<span class="mut">{{items|length}} anuncios (últimos 300)</span></div>
<div class="wrap"><table><tr><th></th><th>Anuncio</th><th>Producto</th><th>Precio</th><th>Habitual</th><th>Búsqueda</th><th>Visto</th>{% if f in ('ex','cand') %}<th>Motivo</th>{% endif %}</tr>
{% for i in items %}<tr style="{{'opacity:.5' if i.reserved}}"><td>{% if i.image %}<img src="{{i.image}}" width="54" height="54" style="object-fit:cover;border-radius:4px" loading="lazy">{% endif %}</td>
<td><a href="{{i.url}}" target="_blank">{{i.title}}</a></td>
<td>{% if i.product_key %}<a href="/precios?q={{i.product_key|urlencode}}">{{i.product_key}}</a>{% if i.key_src=='ia' %} <span class="mut" title="identificado por IA">🤖</span>{% endif %}{% else %}<span class="mut">—</span>{% endif %}</td>
<td><b>{{'%.0f'|format(i.price)}} €</b></td>
<td>{% if i.median %}{{'%.0f'|format(i.median)}} € <span class="mut" style="color:{{'var(--red)' if i.price < i.median*0.8 else 'inherit'}}">{{'%+.0f'|format((i.price/i.median-1)*100)}}%</span>{% elif i.est_price %}~{{'%.0f'|format(i.est_price)}} € <span class="mut">IA</span>{% elif i.status %}<span class="mut">{{i.status}}</span>{% endif %}</td>
<td class="mut">{{i.keyword}}</td><td class="mut">{{i.first_seen|ago}}</td>
{% if f=='ex' %}<td class="mut">{{i.excluded_reason}}</td>{% endif %}
{% if f=='cand' %}<td>{% if i.llm_verdict %}<b style="color:{{'var(--acc2)' if i.llm_verdict in ('chollo','buen precio') else 'var(--red)'}}">{{i.llm_verdict}}</b>
<div class="mut">{{i.llm_reason}}</div>{% else %}<span class="mut">pendiente</span>{% endif %}</td>{% endif %}</tr>{% endfor %}</table></div>"""

PRICES = """
<form class="filters" method="get"><input name="q" value="{{q}}" placeholder="Buscar producto…" style="width:220px">
<select name="k" onchange="this.form.submit()"><option value="">Todos los tipos</option>
{% for k in kinds %}<option {{'selected' if k==kind}}>{{k}}</option>{% endfor %}</select><button class="btn sm">Filtrar</button>
<span class="mut">{{rows|length}} productos · {{nok}} con precio de referencia</span></form>
<div class="wrap"><table><tr><th>Producto</th><th>Tipo</th><th>Habitual (mediana)</th><th>Mínimo</th><th>Amazon (nuevo)</th><th>Anuncios</th><th>Estado</th><th>Valorado</th><th>Vistos en temas</th><th></th></tr>
{% for p in rows %}<tr><td><b>{{p.key}}</b>{% if p.source=='ia' %} <span title="producto nombrado por la IA">🤖</span>{% endif %}<div class="mut">busca "{{p.query}}"</div></td><td class="mut">{{p.kind}}</td>
<td>{% if p.median %}{{'%.0f €'|format(p.median)}}{% elif p.est_price %}~{{'%.0f'|format(p.est_price)}} € <span class="mut" title="{{p.est_note}}">IA ({{p.est_conf}})</span>{% else %}—{% endif %}</td><td class="mut">{{('%.0f €'|format(p.pmin)) if p.pmin else ''}}</td>
<td>{% if p.amazon_price %}<a href="{{p.amazon_url}}" target="_blank" title="{{p.amazon_title}}">{{'%.0f €'|format(p.amazon_price)}}</a>{% elif p.amazon_ts %}<span class="mut">no está</span>{% else %}<span class="mut">—</span>{% endif %}</td>
<td>{{p.n or 0}}</td><td class="mut">{{p.status}}</td><td class="mut">{{p.last_ref|ago}}</td><td>{{p.discovered}}</td>
<td><form method="post" action="/precios/revalorar"><input type="hidden" name="key" value="{{p.key}}"><button class="btn sec sm" title="Revalorar en el próximo turno">↻</button></form></td></tr>
{% endfor %}</table></div>"""


def register(app, page, num, tworker):
    @app.route("/temas")
    def themes_page():
        with core.db() as con:
            rows = []
            for t in con.execute("SELECT * FROM themes ORDER BY name").fetchall():
                d = dict(t)
                d["nkw"] = len([k for k in t["keywords"].splitlines() if k.strip()])
                d["seen"], d["ident"] = con.execute(
                    "SELECT COUNT(*), COUNT(product_key) FROM t_items WHERE theme_id=?", (t["id"],)).fetchone()
                d["ndeals"] = con.execute("SELECT COUNT(*) FROM t_deals WHERE theme_id=?", (t["id"],)).fetchone()[0]
                rows.append(d)
            eid = request.args.get("edit", type=int)
            edit = con.execute("SELECT * FROM themes WHERE id=?", (eid,)).fetchone() if eid else None
        return page("themes", THEMES, themes=rows, edit=edit, tw=tworker)

    @app.post("/temas/guardar")
    def save_theme():
        f = request.form
        vals = (f["name"].strip(), f["keywords"].strip(), num(f.get("below_pct")) or 35, num(f.get("drop_pct")) or 15,
                1 if f.get("enabled") else 0, num(f.get("min_saving")) or 0)
        with core.db() as con:
            if f.get("id"):
                con.execute("UPDATE themes SET name=?,keywords=?,below_pct=?,drop_pct=?,enabled=?,min_saving=? WHERE id=?",
                            (*vals, int(f["id"])))
                con.execute("UPDATE t_items SET eval_price=NULL WHERE theme_id=?", (int(f["id"]),))
            else:
                con.execute("INSERT INTO themes(name,keywords,below_pct,drop_pct,enabled,min_saving,created_at) "
                            "VALUES(?,?,?,?,?,?,?)",
                            (*vals, int(time.time())))
        flash("Tema guardado.")
        return redirect(url_for("themes_page"))

    @app.post("/temas/<int:tid>/borrar")
    def delete_theme(tid):
        with core.db() as con:
            con.execute("DELETE FROM t_deals WHERE theme_id=?", (tid,))
            con.execute("DELETE FROM t_items WHERE theme_id=?", (tid,))
            con.execute("DELETE FROM themes WHERE id=?", (tid,))
        flash("Tema borrado (la base de precios se conserva).")
        return redirect(url_for("themes_page"))

    @app.post("/temas/reclasificar")
    def reclassify():
        themes.reclassify_all(core.get_settings())
        flash("Anuncios reclasificados.")
        return redirect(request.referrer or url_for("themes_page"))

    @app.route("/tema/<int:tid>")
    def theme_detail(tid):
        f = request.args.get("f", "")
        cond = {"": "i.product_key IS NOT NULL AND i.excluded_reason IS NULL", "cand": "i.cand=1",
                "sin": "i.product_key IS NULL AND i.excluded_reason IS NULL",
                "ex": "i.excluded_reason IS NOT NULL"}.get(f, "1=1")
        with core.db() as con:
            t = con.execute("SELECT * FROM themes WHERE id=?", (tid,)).fetchone()
            if not t:
                return redirect("/temas")
            items = con.execute(f"SELECT i.*, p.median, p.status, p.est_price FROM t_items i LEFT JOIN products p ON p.key=i.product_key "
                                f"WHERE i.theme_id=? AND {cond} ORDER BY i.first_seen DESC LIMIT 300", (tid,)).fetchall()
        return page("themes", THEME, t=t, items=items, f=f)

    @app.route("/precios")
    def prices():
        q = request.args.get("q", "").strip()
        kind = request.args.get("k", "")
        sql, args = "SELECT * FROM products WHERE 1=1", []
        if q:
            sql += " AND key LIKE ?"
            args.append(f"%{core.norm(q)}%")
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        with core.db() as con:
            rows = con.execute(sql + " ORDER BY discovered DESC, key LIMIT 500", args).fetchall()
            kinds = [r[0] for r in con.execute("SELECT DISTINCT kind FROM products ORDER BY 1")]
        return page("prices", PRICES, rows=rows, q=q, kind=kind, kinds=kinds, nok=sum(1 for r in rows if r["median"]))

    @app.post("/precios/revalorar")
    def revalue():
        with core.db() as con:
            con.execute("UPDATE products SET next_ref=0 WHERE key=?", (request.form["key"],))
        flash("Se revalorará en los próximos turnos (si hay anuncios recientes de ese producto).")
        return redirect(request.referrer or "/precios")
