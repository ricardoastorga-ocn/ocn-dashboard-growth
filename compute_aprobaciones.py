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


MES_NOMBRE = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
              "septiembre", "octubre", "noviembre", "diciembre"]


def fmt_short(d):
    return f"{d.day} {MESES[d.month - 1]}"


def monday_of(d):
    return d - datetime.timedelta(days=d.weekday())


def business_days_between(start, end):
    n = 0
    d = start
    while d <= end:
        if d.weekday() < 5:
            n += 1
        d += datetime.timedelta(days=1)
    return n


def add_months(d, delta):
    m = d.month - 1 + delta
    y = d.year + m // 12
    m = m % 12 + 1
    return datetime.date(y, m, 1)


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

    # ---------- semanal (lunes-domingo) ----------
    # Verificado 7-sep-2026 a peticion de Ricardo ("revisa que este actualizada semanal de
    # lunes a domingo"): monday_of() SI corta lunes-domingo correcto (confirmado contra
    # calendario real: 3-ago-2026 es lunes, 31-ago-2026 es lunes, etc.). La semana que cae al
    # FINAL del rango (la mas reciente, con datos hasta el corte del CSV) se marca `es_parcial`
    # en vez de eliminarse -- ya trae varios dias reales, no leerla como caida real de volumen.
    # La semana del INICIO del rango (27-jul-2 ago, historicamente solo 1 dia real, 1-ago) se
    # elimina por completo del listado (no solo se marca) -- pedido explicito de Ricardo
    # 15-sep-2026, "quita el primer periodo... que no tienen info".
    weekly = {}
    for r in rows:
        wk_start = monday_of(r["_date"])
        c = weekly.setdefault(wk_start, {"aprobado": 0, "rechazado": 0, "pendiente": 0})
        c[RESULT_KEY[r["resultado"]]] += 1
    # La primera semana (27-jul-2-ago) se descarta por completo -- no solo se marca parcial --
    # porque solo tiene 1 dia real (1-ago) y casi no aporta volumen (pedido de Ricardo
    # 15-sep-2026: "quita el primer periodo... que no tienen info"). La ULTIMA semana parcial
    # SÍ se conserva (marcada `es_parcial`) porque ya trae varios dias reales de datos, a
    # diferencia de la primera.
    weekly_list = []
    for wk_start in sorted(weekly):
        if wk_start < date_min:
            continue
        wk_end = wk_start + datetime.timedelta(days=6)
        c = weekly[wk_start]
        res = c["aprobado"] + c["rechazado"]
        es_parcial = wk_end > date_max
        weekly_list.append({
            "label": f"{fmt_short(wk_start)}–{fmt_short(wk_end)}" + (" (parcial)" if es_parcial else ""),
            "aprobado": c["aprobado"], "rechazado": c["rechazado"], "pendiente": c["pendiente"],
            "total": c["aprobado"] + c["rechazado"] + c["pendiente"],
            "pct_aprobacion": round(c["aprobado"] / res * 100, 1) if res else 0.0,
            "es_parcial": es_parcial,
        })

    # ---------- comparativo mes actual vs. mes anterior, mismos dias habiles ----------
    # Reemplaza el donut "Aprobacion por ciudad" y el foco Tijuana/Mexicali -- Ricardo pidio
    # en su lugar un comparativo directo de 2 columnas (mes en curso vs. el mes anterior,
    # cortando ambos al mismo numero de dias habiles desde el dia 1) para saber cuanto se
    # ha aprobado/rechazado en lo que va del mes vs. el mismo avance del mes previo
    # (15-sep-2026). Generico a proposito (no hardcodea "agosto"/"septiembre") para que
    # el proximo mes que Ricardo traiga un CSV nuevo, el comparativo avance solo.
    mes_actual_inicio = datetime.date(date_max.year, date_max.month, 1)
    mes_dias_habiles = business_days_between(mes_actual_inicio, date_max)

    mes_anterior_inicio = add_months(mes_actual_inicio, -1)
    d = mes_anterior_inicio
    count = 0
    while count < mes_dias_habiles:
        if d.weekday() < 5:
            count += 1
        if count == mes_dias_habiles:
            break
        d += datetime.timedelta(days=1)
    mes_anterior_fin = d

    def summarize_window(start, end):
        subset = [r for r in rows if start <= r["_date"] <= end]
        ap = sum(1 for r in subset if r["resultado"] == "APROBADO")
        rc = sum(1 for r in subset if r["resultado"] == "RECHAZADO")
        pe = sum(1 for r in subset if r["resultado"] == "PENDIENTE")
        res = ap + rc
        return {"total": ap + rc + pe, "aprobado": ap, "rechazado": rc, "pendiente": pe,
                "pct_aprobacion": round(ap / res * 100, 1) if res else 0.0}

    month_compare = [
        {"mes": MES_NOMBRE[mes_anterior_inicio.month - 1].capitalize(),
         "rango": f"{fmt_short(mes_anterior_inicio)}–{fmt_short(mes_anterior_fin)}",
         "dias_habiles": mes_dias_habiles,
         **summarize_window(mes_anterior_inicio, mes_anterior_fin)},
        {"mes": MES_NOMBRE[mes_actual_inicio.month - 1].capitalize(),
         "rango": f"{fmt_short(mes_actual_inicio)}–{fmt_short(date_max)}",
         "dias_habiles": mes_dias_habiles,
         **summarize_window(mes_actual_inicio, date_max)},
    ]

    # ---------- solicitudes y aprobadas del mes, por asesor ----------
    # Para el ranking de entregas por asesor/team leader del dashboard (refresh_data.py hace
    # el cruce contra el roster Bernardo/Paulina) -- pedido de Ricardo 15-sep-2026. "asesor"
    # en este CSV viene como email (nombre.apellido@onecarnow.com); se deriva un nombre
    # "Nombre Apellido" para poder cruzarlo por nombre+inicial de apellido contra el roster.
    # Cuentas que no son personas (colas de Contact Center, "SIN ASESOR") se excluyen.
    NO_PERSONA_PREFIXES = ("tlconcentra", "agconcentra", "ageconcentra")

    def asesor_display(email):
        local = (email or "").split("@")[0].strip()
        if not local or local.lower().startswith(NO_PERSONA_PREFIXES):
            return None
        parts = [p for p in local.split(".") if p]
        if len(parts) < 2:
            return None
        return " ".join(p.capitalize() for p in parts)

    agente_mes_counts = {}
    for r in rows:
        if not (mes_actual_inicio <= r["_date"] <= date_max):
            continue
        disp = asesor_display(r.get("asesor"))
        if not disp:
            continue
        c = agente_mes_counts.setdefault(disp, {"solicitudes": 0, "aprobadas": 0})
        c["solicitudes"] += 1
        if r["resultado"] == "APROBADO":
            c["aprobadas"] += 1
    aprob_by_agente_mes = [{"asesor": a, **c} for a, c in sorted(agente_mes_counts.items())]

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
        "aprob_month_compare": month_compare,
        "aprob_by_agente_mes": aprob_by_agente_mes,
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
          f"semanas={len(weekly_list)} (ultima parcial, primera semana sin cobertura ya excluida)")
    print("comparativo mismos dias habiles:", month_compare)


if __name__ == "__main__":
    main()
