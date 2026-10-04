"""IA vía OpenRouter: identificar productos que las reglas no reconocen, estimar su valor cuando
hay pocos anuncios y revisar cada candidato a chollo antes de avisar."""
import json
import logging
import re
import time

import requests

import core

log = logging.getLogger("chollos.ia")
URL = "https://openrouter.ai/api/v1/chat/completions"

DEFAULTS = {
    "llm_enabled": "1",
    "openrouter_key": "",
    "llm_model_fast": "google/gemini-2.5-flash-lite",
    "llm_model_smart": "anthropic/claude-haiku-4.5",
    "llm_daily_budget": "0.30",
    "llm_spent_day": "",
    "llm_spent_today": "0",
    "llm_spent_total": "0",
}


class BudgetExceeded(Exception):
    pass


def init_db():
    with core.db() as con:
        for k, v in DEFAULTS.items():
            con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))


def available(settings):
    return settings.get("llm_enabled") == "1" and bool(settings.get("openrouter_key"))


def _track(cost):
    s = core.get_settings()
    today = time.strftime("%Y-%m-%d")
    spent = float(s["llm_spent_today"]) if s["llm_spent_day"] == today else 0.0
    core.set_settings({"llm_spent_day": today, "llm_spent_today": f"{spent + cost:.6f}",
                       "llm_spent_total": f"{float(s['llm_spent_total']) + cost:.6f}"})


def spent_today(settings):
    return float(settings["llm_spent_today"]) if settings["llm_spent_day"] == time.strftime("%Y-%m-%d") else 0.0


def ask_json(settings, model, system, user, max_tokens=1500):
    """Llama al modelo y devuelve el JSON de la respuesta. Respeta el presupuesto diario."""
    if spent_today(settings) >= float(settings["llm_daily_budget"]):
        raise BudgetExceeded()
    r = requests.post(URL, timeout=90, headers={
        "Authorization": f"Bearer {settings['openrouter_key']}",
        "HTTP-Referer": "http://192.168.1.208:8080", "X-Title": "Wallapop Chollos"}, json={
        "model": model, "max_tokens": max_tokens, "temperature": 0,
        "usage": {"include": True},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
    r.raise_for_status()
    data = r.json()
    _track(float((data.get("usage") or {}).get("cost") or 0))
    text = data["choices"][0]["message"]["content"] or ""
    m = re.search(r"[\[{].*[\]}]", text, re.S)
    if not m:
        raise ValueError(f"respuesta sin JSON: {text[:200]}")
    return json.loads(m.group(0))


# ---------------------------------------------------------------- identificación

IDENTIFY_SYSTEM = """Clasificas anuncios de Wallapop (España) para un buscador de chollos del tema indicado.
Para cada anuncio decide:
- "rel": true si es un producto del tema que alguien del tema compraría (equipo, componente o periférico relevante); false si no tiene que ver (ropa, libros, muebles, coches...) o es un servicio/licencia/"busco".
- "key": si rel=true, clave canónica del producto concreto, en minúsculas y sin tildes: marca + modelo (+ capacidad si cambia el precio). Si no se puede saber el modelo, pon lo más concreto posible que sirva para compararlo con anuncios iguales ("wd my cloud ex2 ultra", "synology nas 2 bahias"). Si es demasiado vago para valorarlo ("lote de cables", "servidor"), key=null.
- "kind": una palabra: nas, minipc, servidor, red, sai, sbc, hdd, ssd, ram, cpu, gpu, placa, otros.
- "acc": true si es un accesorio o repuesto de un producto (caja, fuente, cable, caddy) y no el producto.
Formato de claves ya usado (reutiliza estas si es el mismo producto): {keys}
Responde SOLO con un array JSON: [{{"i":0,"rel":true,"key":"...","kind":"...","acc":false}}, ...]"""


def identify_batch(settings, theme_name, items, known_keys):
    system = IDENTIFY_SYSTEM.format(keys=", ".join(known_keys[:150]) or "(ninguna todavía)")
    lines = "\n".join(f"{n}. [{r['price']:.0f} €] {r['title']} — {(r['description'] or '')[:160]}"
                      for n, r in enumerate(items))
    out = ask_json(settings, settings["llm_model_fast"], system, f"Tema: {theme_name}\n\n{lines}", 3000)
    return {int(o["i"]): o for o in out if isinstance(o, dict) and "i" in o}


# ---------------------------------------------------------------- estimación de precio

ESTIMATE_SYSTEM = """Eres un experto en el mercado de segunda mano en España (Wallapop, eBay.es, Milanuncios).
Estima el precio HABITUAL de venta de segunda mano, en buen estado y funcionando, del producto indicado.
Te doy los pocos anuncios reales que tenemos, pero son solo una pista: pueden ser gangas, incluir extras o estar
inflados. Básate sobre todo en tu conocimiento del mercado; no copies el precio de un anuncio.
Responde SOLO con JSON: {"price": número en euros, "low": número, "high": número, "confidence": "alta"|"media"|"baja", "note": "máx 15 palabras"}"""


def estimate_price(settings, key, samples):
    ads = "\n".join(f"- {t} — {p:.0f} €" for t, p in samples[:10]) or "(ninguno)"
    out = ask_json(settings, settings["llm_model_smart"], ESTIMATE_SYSTEM,
                   f"Producto: {key}\nAnuncios vistos:\n{ads}", 300)
    return out


# ---------------------------------------------------------------- revisión de candidatos

REVIEW_SYSTEM = """Revisas posibles chollos de Wallapop (España) para alguien del tema indicado. Lee el anuncio y decide si merece un aviso.
Criterios:
- ¿Es de verdad el producto indicado, completo y funcionando? (no accesorio, no piezas, no "busco", no sin disco/placa si eso cambia el valor, no bloqueado)
- ¿El precio es realmente bajo para lo que incluye? Ten en cuenta extras (discos, RAM, cargador) o carencias.
- ¿Señales de estafa? (precio absurdo, "solo envío", "pago por adelantado", texto copiado, 1 €).
- Si hay precio nuevo en Amazon: un usado a más del ~75% del precio nuevo no es chollo, aunque esté bajo la mediana de segunda mano
  (y si la mediana de segunda mano supera el precio nuevo, desconfía de esa mediana). Menciona el precio nuevo en la frase si ayuda.
Responde SOLO con JSON: {"ok": true|false, "verdict": "chollo"|"buen precio"|"precio normal"|"sospechoso"|"defectuoso"|"no es el producto", "value": precio justo estimado en euros, "reason": "una frase corta en español para el aviso (qué incluye, por qué es buen precio o qué falla)"}
ok=true solo para "chollo" o "buen precio"."""


def review(settings, theme_name, item, ref, basis, amazon=None):
    amz = (f"Precio NUEVO en Amazon.es: {amazon['price']:.0f} € («{amazon['title'][:120]}»)\n" if amazon
           else "Precio nuevo en Amazon.es: no encontrado\n")
    user = (f"Tema: {theme_name}\nProducto identificado: {item['product_key']}\n"
            f"Precio habitual de segunda mano: {ref:.0f} € ({basis})\n{amz}\n"
            f"Anuncio: {item['title']}\nPrecio: {item['price']:.0f} €\nCiudad: {item['city']}\n"
            f"Descripción: {(item['description'] or '')[:1500]}")
    return ask_json(settings, settings["llm_model_smart"], REVIEW_SYSTEM, user, 400)
