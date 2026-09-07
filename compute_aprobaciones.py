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
    "/Users/rich/Desktop/APROBADAS AGOSTO.csv",
    "/Users/rich/Desktop/APROBADAS SEP.csv",
]

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

    # ---------- semanal (lunes-domingo) ----------
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
        weekly_list.append({
            "label": f"{fmt_short(wk_start)}–{fmt_short(wk_end)}",
            "aprobado": c["aprobado"], "rechazado": c["rechazado"], "pendiente": c["pendiente"],
            "total": c["aprobado"] + c["rechazado"] + c["pendiente"],
            "pct_aprobacion": round(c["aprobado"] / res * 100, 1) if res else 0.0,
        })

    # ---------- por ciudad (todo el periodo) ----------
    by_city = {}
    for r in rows:
        c = by_city.setdefault(r["_city"], {"aprobado": 0, "rechazado": 0, "pendiente": 0})
        c[RESULT_KEY[r["resultado"]]] += 1
    city_list = []
    for c in CITY_ORDER + ["Otro"]:
        if c not in by_city:
            continue
        d = by_city[c]
        res = d["aprobado"] + d["rechazado"]
        city_list.append({
            "ciudad": c, "colorVar": CITY_COLOR.get(c, "--s-otros"),
            "total": d["aprobado"] + d["rechazado"] + d["pendiente"],
            "aprobado": d["aprobado"], "rechazado": d["rechazado"], "pendiente": d["pendiente"],
            "resueltas": res,
            "pct_aprobacion": round(d["aprobado"] / res * 100, 1) if res else 0.0,
        })

    # ---------- tendencia diaria por ciudad, promedio movil 7 dias ----------
    dates_sorted = sorted(set(r["_date"] for r in rows))
    date_min, date_max = dates_sorted[0], dates_sorted[-1]
    all_days = [date_min + datetime.timedelta(days=i) for i in range((date_max - date_min).days + 1)]

    daily_by_city = {"tij": {}, "cdmx": {}, "mty": {}, "qro": {}, "gdl": {}, "mxl": {}, "otros": {}}
    # Mismos 7 buckets ya establecidos en el resto del dashboard (DIAS_KEYS/FLEET_DAY_KEYS
    # en index.html) -- Tijuana y Mexicali van solas (foco explicito de Ricardo), el resto
    # de ciudades chicas (Merida/Puebla/Saltillo/Otro) se agrupan en "otros" para que la
    # comparacion siga siendo legible con 35+ dias en el eje.
    DAILY_BUCKETS = {
        "Tijuana": "tij", "CDMX / Edo Mex": "cdmx", "Monterrey": "mty",
        "Queretaro": "qro", "Guadalajara": "gdl", "Mexicali": "mxl",
        "Merida": "otros", "Puebla": "otros", "Saltillo": "otros", "Otro": "otros",
    }
    DAILY_KEYS = ["tij", "cdmx", "mty", "qro", "gdl", "mxl", "otros"]
    for r in rows:
        bucket = DAILY_BUCKETS.get(r["_city"])
        if bucket is None or r["resultado"] not in ("APROBADO", "RECHAZADO"):
            continue
        d = daily_by_city[bucket].setdefault(r["_date"], {"aprobado": 0, "rechazado": 0})
        d[RESULT_KEY[r["resultado"]]] += 1

    focus_daily = []
    for d in all_days:
        row = {"fecha": fmt_short(d)}
        # ventana expansiva los primeros dias (no hay 7 dias de historia real antes del
        # inicio de los datos) para no mostrar un 0% falso al arrancar la serie
        win_size = min(7, (d - date_min).days + 1)
        for key in DAILY_KEYS:
            window = [d - datetime.timedelta(days=k) for k in range(win_size)]
            apr = sum(daily_by_city[key].get(w, {}).get("aprobado", 0) for w in window)
            rec = sum(daily_by_city[key].get(w, {}).get("rechazado", 0) for w in window)
            res = apr + rec
            row[f"{key}_pct"] = round(apr / res * 100, 1) if res else 0.0
            row[f"{key}_n"] = res
        focus_daily.append(row)
    n_gap_days = sum(1 for row in focus_daily if row["tij_n"] == 0 or row["mxl_n"] == 0)

    out = {
        "aprob_kpis": {
            "total": total, "aprobado": aprobado, "rechazado": rechazado, "pendiente": pendiente,
            "pct_aprobacion": pct_aprobacion,
        },
        "aprob_weekly": weekly_list,
        "aprob_by_city": city_list,
        "aprob_daily_focus": focus_daily,
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
          f"dias con ventana-7d vacia (Tij o Mxl)={n_gap_days}")
    print("por ciudad:", [(c["ciudad"], c["pct_aprobacion"], c["total"]) for c in city_list])


if __name__ == "__main__":
    main()
