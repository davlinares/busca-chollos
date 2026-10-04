# 🔥 Wallapop Chollos

Bot de búsqueda de chollos en **Wallapop**, **Milanuncios** y **eBay España**. Monitoriza búsquedas personalizadas, detecta precios por debajo de la mediana del mercado y bajadas de precio, y te avisa por email.

![Python](https://img.shields.io/badge/python-3.10+-blue) ![Flask](https://img.shields.io/badge/flask-3.x-green) ![License](https://img.shields.io/badge/license-MIT-orange)

## Características

- **Multifuente**: busca en Wallapop, Milanuncios y eBay España simultáneamente
- **Detección de chollos**: precio ≤ mediana − X% (configurable por búsqueda)
- **Bajadas de precio**: alerta cuando un anuncio baja desde su máximo histórico
- **Temas automáticos**: rotación de keywords de homelab/tecnología con análisis por IA
- **IA opcional**: OpenRouter (Gemini Flash / Claude Haiku) identifica productos y revisa candidatos
- **Amazon**: consulta el precio nuevo en Amazon.es vía SerpAPI para comparar
- **Interfaz web** local: gestión de búsquedas, histórico de precios, chollos en tarjetas
- **Email configurable**: resumen diario o aviso inmediato (Gmail/SMTP)
- **Badge de plataforma**: 🟠 Wallapop · 🟢 Milanuncios · 🔵 eBay

## Capturas

_Web en `http://localhost:8080`_

## Requisitos

- Python 3.10+
- Pip packages: `pip install flask waitress requests`
- Acceso a internet (Wallapop y Milanuncios no requieren cuenta)
- Opcional: cuenta en [developer.ebay.com](https://developer.ebay.com) (gratis) para eBay
- Opcional: clave [OpenRouter](https://openrouter.ai) para IA (~0.30 $/día)
- Opcional: clave [SerpAPI](https://serpapi.com) para precios Amazon (250 búsquedas/mes gratis)

## Instalación rápida

```bash
git clone https://github.com/davlinares/busca-chollos
cd wallapop-chollos
pip install -r requirements.txt
python app.py
```

Abre `http://localhost:8080` en el navegador.

## Instalación como servicio (Linux/systemd)

```bash
sudo cp wallapop-chollos.service /etc/systemd/system/
sudo systemctl enable --now wallapop-chollos
```

Ajusta la ruta en el `.service` si instalas en un directorio distinto a `/opt/wallapop-chollos`.

## Configuración

Toda la configuración se hace desde la **interfaz web** (`/ajustes`):

| Sección | Descripción |
|---|---|
| General | Intervalo de búsqueda, coordenadas, palabras excluidas |
| Email | SMTP (Gmail funciona con contraseña de aplicación) |
| IA | Clave OpenRouter, modelos, presupuesto diario |
| Amazon | Clave SerpAPI, límite mensual |
| Plataformas | Activar Milanuncios / eBay (App ID + Cert ID) |

La base de datos (`chollos.db`) se crea automáticamente al primer arranque. La ruta puede cambiarse con la variable de entorno `CHOLLOS_DB`.

## Estructura del proyecto

```
app.py              # Interfaz web (Flask + waitress)
core.py             # Scraper Wallapop, detección de chollos, email
milanuncios.py      # Scraper Milanuncios (HTML + __INITIAL_PROPS__)
ebay.py             # Cliente eBay Browse API (OAuth2)
themes.py           # Temas automáticos con rotación de keywords
ui_themes.py        # Interfaz web de temas y precios
llm.py              # Integración OpenRouter (identificación + revisión IA)
amazon.py           # Consulta precios Amazon.es vía SerpAPI
amazon_refresh.py   # Utilidad para refrescar precios Amazon manualmente
requirements.txt
wallapop-chollos.service  # Ejemplo systemd
```

## Plataformas soportadas

| Plataforma | Método | Credenciales |
|---|---|---|
| Wallapop | API oficial (sin auth) | No necesita |
| Milanuncios | Scraping HTML (`__INITIAL_PROPS__`) | No necesita |
| eBay España | Browse API oficial | App ID + Cert ID (gratis) |

## Notas

- Wallapop y Milanuncios no tienen API pública oficial; el scraping puede romperse si cambian su web.
- El bot respeta pausas entre peticiones para no sobrecargar los servidores.
- Los temas están pensados para equipos de homelab/tecnología pero son completamente configurables.

## Licencia

MIT
