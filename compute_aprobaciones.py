#!/usr/bin/env python3
"""Aprobaciones -- desempeno de aprobacion de solicitudes, agregado a Dashboard Growth.

Fuente: CSVs manuales que Ricardo exporta desde el admin (no hay Sheet/API en vivo para
esto todavia) -- por eso este script se corre A MANO cada vez que Ricardo trae CSVs
nuevos, NO es parte del refresh automatico de GitHub Actions (que corre en la nube y no
tiene acceso a archivos locales del Desktop de Ricardo). Escribe aprobaciones_snapshot.json
(committed al repo); refresh_data.py lo lee y lo re-embebe en cada corrida automatica, asi
el resto del dashboard se sigue refrescando solo sin que esta seccion desaparezca ni truene.
"""
import csv
import datetime
import json
import os
import unicodedata

CSV_PATHS = [
    "/Users/rich/Desktop/tablero_preaprobaciones_ventas_lh.csv",
]
# 9-sep-2026: este archivo consolidado (1-ago a 9-sep, sin duplicados) reemplaza a los
# 2 CSVs incrementales anteriores (APROBADAS AGOSTO.csv / APROBADAS SEP.csv) -- mismo
# esquema de columnas exacto, Ricardo empezo a exportar un solo tablero acumulado en
# vez de cortes mensuales sueltos.

# mismo orden de ciudades ya establecido en refresh_data.py (CITY_ORDER)
CITY_ORDER = ["Tijuana", "CDMX / Edo Mex", "Monterrey", "Mexicali", "Guadalajara",
              "Queretaro", "Merida", "Puebla", "Saltillo"]

# mismos tokens de color ya usados en el resto del dashboard para estas ciudades
# (ver DIAS_COLOR / FLEET_DAY_COLOR en index.html)
CITY_COLOR = {
    "Tijuana": "--s-mg3", "CDMX / Edo Mex": "--s-byd", "Monterrey": "--s-mg5",
    "Mexicali": "--s-tiggo", "Guadalajara": "--s-king", "Queretaro": "--s-aion",
}

MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]

RESULT_KEY = {"APROBADO": "aprobado", "RECHAZADO": "rechazado", "PENDIENTE": "pendiente"}


def norm_ascii(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm_city(raw):
    s = norm_ascii(raw or "").upper().strip()
    s = s.replace("EDOMEX", "EDO MEX")
    for c in CITY_ORDER:
        cn = norm_ascii(c).upper()
        if s == cn or s.replace(" / ", " ") == cn.replace(" / ", " "):
            return c
    if "CDMX" in s or "EDO MEX" in s:
        return "CDMX / Edo Mex"
    return "Otro"


def clean_date(s):
    s = (s or "").strip()
    if s.lower() in ("", "null", "none", "nan"):
        return None
    return s


def fmt_short(d):
    return f"{d.day} {MESES[d.month - 1]}"


def monday_of(d):
    return d - datetime.timedelta(days=d.weekday())


def load_rows():
    """Dedup por url_solicitud -- si una solicitud aparece en ambos CSVs (8 casos, ~0.6%
    del total), se queda la version con fecha_envio_valuacion MAS RECIENTE (estado mas
    actual conocido). Confirmado que en esos 8 casos el resultado a veces cambia entre
    archivos (incl. algunos que retroceden de APROBADO a PENDIENTE) -- no es un bug de
    este script, es la fuente trayendo estados distintos por fecha de reenvio de
    valuacion; se documenta como caveat menor en vez de forzar una regla mas compleja."""
    by_url = {}
    for path in CSV_PATHS:
        with open(path, newline="", encoding="utf-8-sig") as f:
            r = csv.DictReader(f)
            for row in r:
                d = clean_date(row.get("fecha_envio_valuacion"))
                if d is None:
                    continue
                row["_fecha"] = d
                url = row["url_solicitud"]
                existing = by_url.get(url)
                if existing is None or d > existing["_fecha"]:
                    by_url[url] = row
    return list(by_url.values())


def main():
    rows = load_rows()
    for r in rows:
        r["_date"] = datetime.date.fromisoformat(r["_fecha"])
        r["_city"] = norm_city(r["ciudad"])

    total = len(rows)
    aprobado = sum(1 for r in rows if r["resultado"] == "APROBADO")
    rechazado = sum(1 for r in rows if r["resultado"] == "RECHAZADO")
    pendiente = sum(1 for r in rows if r["resultado"] == "PENDIENTE")
    resueltas = aprobado + rechazado
    pct_aprobacion = round(aprobado / resueltas * 100, 1) if resueltas else 0.0

    dates_sorted = sorted(set(r["_date"] for r in rows))
    date_min, date_max = dates_sorted[0], dates_sorted[-1]
    all_days = [date_min + datetime.timedelta(days=i) for i in range((date_max - date_min).days + 1)]
    midpoint = date_min + datetime.timedelta(days=(date_max - date_min).days // 2)

    # ---------- semanal (lunes-domingo) ----------
    # Verificado 7-sep-2026 a peticion de Ricardo ("revisa que este actualizada semanal de
    # lunes a domingo"): monday_of() SI corta lunes-domingo correcto (confirmado contra
    # calendario real: 3-ago-2026 es lunes, 31-ago-2026 es lunes, etc.). El hallazgo real
    # no era la logica de corte sino que la PRIMERA semana (27-jul-2 ago, solo 1 dia real,
    # 1-ago) y la ULTIMA (31 ago-6 sep, solo 5 de 7 dias reales, datos hasta el 4-sep) son
    # semanas PARCIALES -- se marcan explicitamente (`es_parcial`) para no leerlas como una
    # caida real de volumen cuando en realidad la semana todavia no termina de capturarse.
    weekly = {}
    for r in rows:
        wk_start = monday_of(r["_date"])
        c = weekly.setdefault(wk_start, {"aprobado": 0, "rechazado": 0, "pendiente": 0})
        c[RESULT_KEY[r["resultado"]]] += 1
    weekly_list = []
    for wk_start in sorted(weekly):
        wk_end = wk_start + datetime.timedelta(days=6)
        c = weekly[wk_start]
        res = c["aprobado"] + c["rechazado"]
        es_parcial = wk_start < date_min or wk_end > date_max
        weekly_list.append({
            "label": f"{fmt_short(wk_start)}–{fmt_short(wk_end)}" + (" (parcial)" if es_parcial else ""),
            "aprobado": c["aprobado"], "rechazado": c["rechazado"], "pendiente": c["pendiente"],
            "total": c["aprobado"] + c["rechazado"] + c["pendiente"],
            "pct_aprobacion": round(c["aprobado"] / res * 100, 1) if res else 0.0,
            "es_parcial": es_parcial,
        })

    # ---------- por ciudad (todo el periodo) + tendencia (1a mitad vs 2a mitad) ----------
    # El donut es una foto de todo el periodo -- no muestra si una ciudad va mejorando o
    # no. Se agrega delta_pp comparando el % de aprobacion de la 1a mitad del periodo vs.
    # la 2a mitad, para que "sigue sin moverse" (Ricardo) se pueda leer directo en la
    # leyenda sin necesitar una gráfica de tendencia aparte.
    by_city = {}
    by_city_half = {}
    for r in rows:
        c = by_city.setdefault(r["_city"], {"aprobado": 0, "rechazado": 0, "pendiente": 0})
        c[RESULT_KEY[r["resultado"]]] += 1
        if r["resultado"] in ("APROBADO", "RECHAZADO"):
            half = "h1" if r["_date"] <= midpoint else "h2"
            hc = by_city_half.setdefault(r["_city"], {"h1": {"aprobado": 0, "rechazado": 0}, "h2": {"aprobado": 0, "rechazado": 0}})
            hc[half][RESULT_KEY[r["resultado"]]] += 1
    city_list = []
    for c in CITY_ORDER + ["Otro"]:
        if c not in by_city:
            continue
        d = by_city[c]
        res = d["aprobado"] + d["rechazado"]
        h = by_city_half.get(c, {"h1": {"aprobado": 0, "rechazado": 0}, "h2": {"aprobado": 0, "rechazado": 0}})
        h1_res = h["h1"]["aprobado"] + h["h1"]["rechazado"]
        h2_res = h["h2"]["aprobado"] + h["h2"]["rechazado"]
        pct_h1 = round(h["h1"]["aprobado"] / h1_res * 100, 1) if h1_res else None
        pct_h2 = round(h["h2"]["aprobado"] / h2_res * 100, 1) if h2_res else None
        delta_pp = round(pct_h2 - pct_h1, 1) if (pct_h1 is not None and pct_h2 is not None) else None
        city_list.append({
            "ciudad": c, "colorVar": CITY_COLOR.get(c, "--s-otros"),
            "total": d["aprobado"] + d["rechazado"] + d["pendiente"],
            "aprobado": d["aprobado"], "rechazado": d["rechazado"], "pendiente": d["pendiente"],
            "resueltas": res,
            "pct_aprobacion": round(d["aprobado"] / res * 100, 1) if res else 0.0,
            "pct_h1": pct_h1, "pct_h2": pct_h2, "delta_pp": delta_pp,
        })

    # Solo 3 series -- Tijuana y Mexicali (foco explicito de Ricardo) + un "resto de la
    # red" agregado (CDMX/Edo Mex, Monterrey, Queretaro, Guadalajara, Merida, Puebla,
    # Saltillo, Otro combinados en un solo pool). Se probo primero con 7 lineas
    # individuales (una por ciudad) y Ricardo la rechazo por ilegible ("es muy mala la
    # grafica") -- con muchos dias en el eje, 5 lineas de referencia delgadas encima de
    # las 2 que importan es puro ruido visual. El agregado da la misma comparacion ("como
    # le va a Tijuana/Mexicali vs. el resto") en una sola serie de referencia limpia.
    # Ronda 4 de esta grafica (7-sep, tarde): Ricardo pidio verla como BARRAS semanales
    # (lunes-domingo, mismo corte que "Solicitudes por semana") en vez de linea diaria,
    # para que el comportamiento semana a semana sea claro -- ver Ronda correspondiente
    # en project_dashboard_growth_automation.md.
    FOCUS_BUCKETS = {
        "Tijuana": "tij", "Mexicali": "mxl",
        "CDMX / Edo Mex": "resto", "Monterrey": "resto", "Queretaro": "resto",
        "Guadalajara": "resto", "Merida": "resto", "Puebla": "resto",
        "Saltillo": "resto", "Otro": "resto",
    }
    FOCUS_KEYS = ["tij", "mxl", "resto"]
    weekly_focus_counts = {}
    for r in rows:
        bucket = FOCUS_BUCKETS.get(r["_city"])
        if bucket is None:
            continue
        wk_start = monday_of(r["_date"])
        c = weekly_focus_counts.setdefault(wk_start, {"tij": 0, "mxl": 0, "resto": 0})
        c[bucket] += 1
    weekly_focus = []
    for wk_start in sorted(weekly_focus_counts):
        wk_end = wk_start + datetime.timedelta(days=6)
        es_parcial = wk_start < date_min or wk_end > date_max
        c = weekly_focus_counts[wk_start]
        weekly_focus.append({
            "label": f"{fmt_short(wk_start)}–{fmt_short(wk_end)}" + (" (parcial)" if es_parcial else ""),
            "tij": c["tij"], "mxl": c["mxl"], "resto": c["resto"],
            "es_parcial": es_parcial,
        })

    # ---------- volumen diario por ciudad (todas, sin agrupar) ----------
    # A diferencia del % de aprobacion (una tasa independiente por ciudad, ruidosa con
    # muchas lineas), el VOLUMEN si tiene sentido apilado -- cada ciudad es una parte
    # real del total de solicitudes de ese dia, por eso aqui se muestran las 7 ciudades
    # completas (mismos buckets/colores ya establecidos, DIAS_KEYS) sin agrupar en
    # "resto". Cuenta TODAS las solicitudes del dia (Aprobado+Rechazado+Pendiente), sin
    # ventana movil -- es volumen crudo, no una tasa, no necesita el suavizado de 7 dias.
    VOLUME_BUCKETS = {
        "Tijuana": "tij", "CDMX / Edo Mex": "cdmx", "Monterrey": "mty",
        "Queretaro": "qro", "Guadalajara": "gdl", "Mexicali": "mxl",
        "Merida": "otros", "Puebla": "otros", "Saltillo": "otros", "Otro": "otros",
    }
    VOLUME_KEYS = ["tij", "cdmx", "mty", "qro", "gdl", "mxl", "otros"]
    volume_by_day = {d: {k: 0 for k in VOLUME_KEYS} for d in all_days}
    for r in rows:
        bucket = VOLUME_BUCKETS.get(r["_city"])
        if bucket is None:
            continue
        volume_by_day[r["_date"]][bucket] += 1
    daily_volume = [{"fecha": fmt_short(d), **volume_by_day[d]} for d in all_days]

    out = {
        "aprob_kpis": {
            "total": total, "aprobado": aprobado, "rechazado": rechazado, "pendiente": pendiente,
            "pct_aprobacion": pct_aprobacion,
        },
        "aprob_weekly": weekly_list,
        "aprob_by_city": city_list,
        "aprob_weekly_focus": weekly_focus,
        "aprob_daily_volume": daily_volume,
        "aprob_meta": {
            "fecha_min": fmt_short(date_min), "fecha_max": fmt_short(date_max),
            "generado_en": datetime.datetime.now().isoformat(),
        },
    }
    out_path = os.path.join(os.path.dirname(__file__), "aprobaciones_snapshot.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"OK -- aprobaciones_snapshot.json escrito. total={total} aprobado={aprobado} "
          f"rechazado={rechazado} pendiente={pendiente} pct_aprobacion={pct_aprobacion}%")
    print(f"rango: {fmt_short(date_min)} - {fmt_short(date_max)}  dias={len(all_days)}  "
          f"semanas={len(weekly_list)} (primera y ultima parciales)")
    print("por ciudad (pct_total, pct_h1->pct_h2, delta_pp):",
          [(c["ciudad"], c["pct_aprobacion"], c["pct_h1"], c["pct_h2"], c["delta_pp"]) for c in city_list])


if __name__ == "__main__":
    main()
