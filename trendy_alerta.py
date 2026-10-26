#!/usr/bin/env python3
"""
Alerta de calendario de adviento - Trendy Shop (VTEX)

Vigila maquillajetrendyshop.com y avisa por Telegram cuando aparezca un
producto o un texto relacionado con "adviento" / "calendario", y cuando un
producto que estaba agotado vuelva a estar disponible.

USO RAPIDO
  1) Crea config.env junto a este archivo (ver abajo).
  2) python3 trendy_alerta.py --test             -> manda un mensaje de prueba a Telegram
  3) python3 trendy_alerta.py --probe adviento   -> muestra que devuelve la tienda (verifica que funciona)
  4) python3 trendy_alerta.py                    -> queda vigilando (Ctrl+C para parar)

config.env (una variable por linea, sin comillas):
  TELEGRAM_TOKEN=123456:ABC...        (se obtiene con @BotFather)
  TELEGRAM_CHAT_ID=123456789          (se obtiene abriendo https://api.telegram.org/bot<TOKEN>/getUpdates
                                       despues de escribirle cualquier cosa al bot)
  INTERVALO_SEGUNDOS=90               (opcional, minimo recomendado 60)

Solo usa la libreria estandar de Python 3.8+.
"""

import argparse
import json
import os
import random
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://www.maquillajetrendyshop.com"

# Terminos que se buscan en el buscador de la tienda
TERMINOS_BUSQUEDA = ["adviento", "calendario"]
# Palabras que deben aparecer en el nombre/enlace del producto para considerarlo relevante
PALABRAS_CLAVE = ["adviento", "advent", "calendario"]
# Paginas cuyo HTML se revisa en busca de esas palabras (banners, textos nuevos)
PAGINAS = [
    BASE + "/",
    BASE + "/nueva-coleccion",
    # Pagina del calendario 2025 (Nick 90's): si la reutilizan o la actualizan, avisa
    BASE + "/calendario-de-adviento-nick-90s",
]

AQUI = Path(__file__).resolve().parent
ARCHIVO_ESTADO = AQUI / "estado.json"
ARCHIVO_CONFIG = AQUI / "config.env"
UA = "Mozilla/5.0 (compatible; AlertaPersonal/1.0)"


# ---------------------------------------------------------------- utilidades
def normalizar(texto):
    texto = unicodedata.normalize("NFD", texto or "")
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return texto.lower()


def cargar_config():
    cfg = dict(os.environ)
    if ARCHIVO_CONFIG.exists():
        for linea in ARCHIVO_CONFIG.read_text(encoding="utf-8").splitlines():
            linea = linea.strip()
            if not linea or linea.startswith("#") or "=" not in linea:
                continue
            k, v = linea.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def log(msg):
    print(time.strftime("[%Y-%m-%d %H:%M:%S] ") + msg, flush=True)


def enviar_telegram(cfg, texto):
    token = cfg.get("TELEGRAM_TOKEN")
    chat = cfg.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log("(sin Telegram configurado) " + texto.replace("\n", " | "))
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": texto}).encode()
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except Exception as e:  # noqa: BLE001
        log(f"Error enviando a Telegram: {e}")
        return False


# ------------------------------------------------------------ consulta tienda
def _disponible(items):
    for it in items or []:
        for s in it.get("sellers", []) or []:
            oferta = s.get("commertialOffer", {}) or {}
            if (oferta.get("AvailableQuantity") or 0) > 0:
                return True
    return False


def _normalizar_producto(p):
    link = p.get("link") or ""
    if link.startswith("/"):
        link = BASE + link
    return {
        "id": str(p.get("productId") or p.get("productName")),
        "nombre": p.get("productName") or "(sin nombre)",
        "link": link,
        "disponible": _disponible(p.get("items")),
    }


def buscar_legacy(termino):
    url = (
        f"{BASE}/api/catalog_system/pub/products/search"
        f"?ft={urllib.parse.quote(termino)}&_from=0&_to=29"
    )
    data = json.loads(http_get(url))
    return [_normalizar_producto(p) for p in data]


def buscar_inteligente(termino):
    url = (
        f"{BASE}/api/io/_v/api/intelligent-search/product_search"
        f"?query={urllib.parse.quote(termino)}&count=30&locale=es-CO"
    )
    data = json.loads(http_get(url))
    return [_normalizar_producto(p) for p in data.get("products", [])]


def buscar(termino):
    """Prueba la API clasica de VTEX y, si falla, la busqueda inteligente."""
    errores = []
    for nombre, fn in (("catalogo", buscar_legacy), ("inteligente", buscar_inteligente)):
        try:
            return fn(termino), nombre
        except Exception as e:  # noqa: BLE001
            errores.append(f"{nombre}: {e}")
    raise RuntimeError("; ".join(errores))


def es_relevante(prod):
    texto = normalizar(prod["nombre"] + " " + prod["link"])
    return any(k in texto for k in PALABRAS_CLAVE)


def fragmentos_en_pagina(url):
    """Devuelve el conjunto de fragmentos de texto alrededor de las palabras clave."""
    html = http_get(url)
    texto = normalizar(re.sub(r"<[^>]+>", " ", html))
    texto = re.sub(r"\s+", " ", texto)
    frags = set()
    for k in PALABRAS_CLAVE:
        for m in re.finditer(re.escape(k), texto):
            a, b = max(0, m.start() - 40), min(len(texto), m.end() + 40)
            frags.add(f"{url} :: {texto[a:b]}")
    return frags


# ------------------------------------------------------------------ estado
def cargar_estado():
    if ARCHIVO_ESTADO.exists():
        try:
            return json.loads(ARCHIVO_ESTADO.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {
        "productos": {},
        "fragmentos": [],
        "base_paginas": False,
        "ultimo_latido": 0,
        "fallos": 0,
        "avisado_fallo": False,
    }


def guardar_estado(estado):
    ARCHIVO_ESTADO.write_text(
        json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ------------------------------------------------------------------ ciclo
def revisar(cfg, estado):
    avisos = []
    # En la primera revision se registran en silencio los productos que ya existen
    # pero no estan disponibles (p. ej. el calendario del año pasado), para no dar falsas alarmas.
    primera_vez = not estado["base_paginas"]

    # 1) Productos
    for termino in TERMINOS_BUSQUEDA:
        productos, fuente = buscar(termino)
        for p in filter(es_relevante, productos):
            previo = estado["productos"].get(p["id"])
            if previo is None:
                if p["disponible"]:
                    avisos.append(f"🎄 YA ESTA EN VENTA: {p['nombre']}\n{p['link']}")
                elif not primera_vez:
                    avisos.append(
                        f"👀 Aparecio en la tienda (aun agotado/no disponible): "
                        f"{p['nombre']}\n{p['link']}"
                    )
            elif p["disponible"] and not previo.get("disponible"):
                avisos.append(f"🎄 YA ESTA DISPONIBLE: {p['nombre']}\n{p['link']}")
            estado["productos"][p["id"]] = {
                "nombre": p["nombre"],
                "disponible": p["disponible"],
            }

    # 2) Textos nuevos en paginas (banners, anuncios)
    actuales = set()
    for url in PAGINAS:
        try:
            actuales |= fragmentos_en_pagina(url)
        except Exception as e:  # noqa: BLE001
            log(f"No pude leer {url}: {e}")
    conocidos = set(estado["fragmentos"])
    if estado["base_paginas"]:
        nuevos = actuales - conocidos
        if nuevos:
            avisos.append(
                "📣 Texto nuevo en la web de Trendy que menciona adviento/calendario. "
                "Revisa ya:\n" + "\n".join(PAGINAS)
            )
    estado["fragmentos"] = sorted(conocidos | actuales)
    estado["base_paginas"] = True

    for a in avisos:
        log(a.replace("\n", " | "))
        # Se envia 2 veces por si una notificacion se pierde
        enviar_telegram(cfg, a)
        time.sleep(2)
        enviar_telegram(cfg, a)


def bucle(cfg):
    intervalo = max(30, int(cfg.get("INTERVALO_SEGUNDOS", "90")))
    estado = cargar_estado()
    log(f"Vigilando Trendy cada ~{intervalo}s. Ctrl+C para parar.")
    enviar_telegram(cfg, "✅ Alerta de Trendy iniciada. Te aviso si aparece el calendario.")
    while True:
        try:
            revisar(cfg, estado)
            if estado["fallos"] >= 10 and estado["avisado_fallo"]:
                enviar_telegram(cfg, "✅ La vigilancia de Trendy se recupero.")
            estado["fallos"] = 0
            estado["avisado_fallo"] = False
        except Exception as e:  # noqa: BLE001
            estado["fallos"] += 1
            log(f"Fallo #{estado['fallos']}: {e}")
            if estado["fallos"] >= 10 and not estado["avisado_fallo"]:
                enviar_telegram(
                    cfg,
                    "⚠️ La vigilancia de Trendy lleva 10 fallos seguidos. "
                    "Puede que la tienda cambio algo o no hay internet.",
                )
                estado["avisado_fallo"] = True

        # Latido diario para saber que sigue vivo
        if time.time() - estado["ultimo_latido"] > 24 * 3600:
            if estado["ultimo_latido"]:
                enviar_telegram(cfg, "🟢 Sigo vigilando Trendy. Sin novedades.")
            estado["ultimo_latido"] = time.time()

        guardar_estado(estado)
        time.sleep(intervalo + random.uniform(0, 20))


# ------------------------------------------------------------------ CLI
def main():
    ap = argparse.ArgumentParser(description="Alerta de adviento Trendy")
    ap.add_argument("--test", action="store_true", help="envia mensaje de prueba")
    ap.add_argument("--probe", metavar="TERMINO", help="muestra resultados de la tienda")
    ap.add_argument("--una-vez", action="store_true", help="revisa una sola vez y sale")
    args = ap.parse_args()
    cfg = cargar_config()

    if args.test:
        ok = enviar_telegram(cfg, "Prueba: la alerta de Trendy esta conectada 🎄")
        print("Enviado." if ok else "No se pudo enviar. Revisa TOKEN y CHAT_ID.")
        return

    if args.probe:
        for nombre, fn in (("catalogo", buscar_legacy), ("inteligente", buscar_inteligente)):
            try:
                res = fn(args.probe)
                print(f"[{nombre}] OK, {len(res)} resultados")
                for p in res[:5]:
                    print(f"   - {p['nombre']} | disponible={p['disponible']} | {p['link']}")
            except Exception as e:  # noqa: BLE001
                print(f"[{nombre}] FALLO: {e}")
        return

    if args.una_vez:
        estado = cargar_estado()
        revisar(cfg, estado)
        guardar_estado(estado)
        return

    try:
        bucle(cfg)
    except KeyboardInterrupt:
        log("Detenido.")


if __name__ == "__main__":
    sys.exit(main())
