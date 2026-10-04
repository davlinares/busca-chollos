"""Actualiza el precio de Amazon de los productos con chollos visibles."""
import logging

import amazon
import core
import themes

logging.basicConfig(level=logging.WARNING)
import sys

# Sin argumentos no hace nada: cada consulta gasta cupo de SerpAPI, así que hay que pedirlas explícitamente
keys = sys.argv[1:] if sys.argv[1:] != ["--todos"] else [r[0] for r in core.db().execute(
    "SELECT DISTINCT d.product_key FROM t_deals d JOIN t_items i ON i.id=d.item_id "
    "WHERE d.dismissed=0 AND i.excluded_reason IS NULL AND i.reserved=0")]
for k in keys:
    s = core.get_settings()
    if not amazon.available(s):
        print("cupo mensual agotado, paro aquí")
        break
    try:
        hit = amazon.refresh("products", "key", k, k, s, themes.reference_query(k))
        print(f"{k:40} " + (f"{hit['price']:>8.2f} €  {hit['title'][:60]}" if hit else "   —   no está en Amazon"))
    except Exception as e:
        print(f"{k:40}  ERROR {e}")
print("consultas usadas este mes:", amazon.used_this_month(core.get_settings()))
