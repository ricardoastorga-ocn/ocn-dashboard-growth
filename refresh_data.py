#!/usr/bin/env python3
"""
Refresca los datos del Dashboard - Growth (OCN) desde Google Sheets y escribe data.js.
Corre sin intervención humana (GitHub Actions) o a mano (`python3 refresh_data.py`).

Fuentes:
  - Back Office (GLOBAL OCN + SEGUIMIENTO ENTREGAS + Log Inventario Diario)
  - Presales-Inventory (Waitlist + Tabla Waitlist como cruce de verificación)
  - Fleet Backlog (RAW DATA, columnas LISTA_TRABAJO/UBICACION_ACTUAL/TALLER_ESTATUS/
    GEST_FECHA_COMPROMISO_ENTREGA) -- SOLO LECTURA, nunca se escribe nada en ese Sheet.

Credenciales via variables de entorno (GitHub Secrets en Actions, o exportadas a mano):
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN
"""
import os
import sys
import json
import re
import datetime
import unicodedata
import urllib.request
import urllib.parse
import collections

BO_ID = "1hMTlrcklmQQpDiNmrav4gZM_ZWmmCniGlJEUgLhIzCY"
PI_ID = "1hmIkvqU342xgN3APYt5dKJbQfM4H1ZmFXWacMsAmiwQ"
FLEET_ID = "1yrz2kBYLSfrpOqNL450Xxs6KavnQdb4RIPcjwaifhtw"

CITY_ORDER = ["Tijuana", "CDMX / Edo Mex", "Monterrey", "Mexicali", "Guadalajara",
              "Queretaro", "Merida", "Puebla", "Saltillo"]
TIER_KEYS = ["0-30", "31-60", "61-90", "90+"]

# Roster fijo de identidades de agente (validado por cruce exacto contra Avance de Marcación
# el 27-ago-2026 -- ver memoria del proyecto). Los que cruzan exacto quedan con nombre limpio;
# el resto se deja verbatim con "@" tal cual viene de la fuente. Si aparece un agente nuevo que
# no está en este set, se agrega automáticamente como "@<nombre tal cual>" -- no se adivina
# ningún cruce parcial nuevo.
KNOWN_AGENT_KEYS = [
    "Adolfo Jaimes", "Mayte Urrutia", "@Daniela Fav", "Ana Rodriguez", "Jrego Nolasco",
    "@Jess Martínez", "Karen Garcia", "Oscar Alvarez", "@Joel Flores Lopez", "Ishell Miranda",
    "@Michelle Ruiz", "Aaron Sanchez", "Angelica Torres", "@Jeremy Habner", "Diana Moreno",
    "Fernando Medina", "Monserrat Rivera", "Imanol Cortez", "Edwin Hernandez", "@Ivette",
    "Antonio Cruz", "@Rafa León", "@Araceli Olvera", "Mirna Cruz", "@Fernando Velazquez",
    "@Mariam Bangoura", "Enrique Jimenez", "@hector vera", "@Carlos Mejía", "@Yael Muñoz",
    "Ricardo Salinas",
]

# Histórico cerrado (ago-2025 a jul-2026), pestaña GLOBAL OCN -- no cambia dia a dia.
# Si un mes se cierra y se consolida a GLOBAL OCN, agregar su fila aqui a mano una vez.
MONTHS_CLOSED = ["Ago 25", "Sep 25", "Oct 25", "Nov 25", "Dic 25", "Ene 26", "Feb 26",
                  "Mar 26", "Abr 26", "May 26", "Jun 26", "Jul 26"]
MIX_CLOSED = [
    {"nuevo": 272, "seminuevo": 37}, {"nuevo": 318, "seminuevo": 22}, {"nuevo": 348, "seminuevo": 42},
    {"nuevo": 420, "seminuevo": 21}, {"nuevo": 374, "seminuevo": 72}, {"nuevo": 421, "seminuevo": 121},
    {"nuevo": 371, "seminuevo": 129}, {"nuevo": 453, "seminuevo": 110}, {"nuevo": 431, "seminuevo": 116},
    {"nuevo": 125, "seminuevo": 237}, {"nuevo": 250, "seminuevo": 327}, {"nuevo": 14, "seminuevo": 298},
]
MODELO_CLOSED = [
    {"byd": 132, "mg5": 116, "mg3": 56, "aion": 0, "king": 0, "tiggo": 4, "otros": 1},
    {"byd": 152, "mg5": 114, "mg3": 55, "aion": 0, "king": 0, "tiggo": 2, "otros": 17},
    {"byd": 168, "mg5": 134, "mg3": 81, "aion": 0, "king": 0, "tiggo": 2, "otros": 5},
    {"byd": 216, "mg5": 150, "mg3": 72, "aion": 0, "king": 0, "tiggo": 3, "otros": 0},
    {"byd": 229, "mg5": 137, "mg3": 76, "aion": 0, "king": 0, "tiggo": 2, "otros": 2},
    {"byd": 269, "mg5": 177, "mg3": 68, "aion": 24, "king": 0, "tiggo": 3, "otros": 1},
    {"byd": 193, "mg5": 133, "mg3": 57, "aion": 104, "king": 0, "tiggo": 9, "otros": 4},
    {"byd": 215, "mg5": 169, "mg3": 41, "aion": 130, "king": 0, "tiggo": 4, "otros": 4},
    {"byd": 256, "mg5": 83, "mg3": 18, "aion": 83, "king": 102, "tiggo": 1, "otros": 4},
    {"byd": 181, "mg5": 101, "mg3": 22, "aion": 37, "king": 8, "tiggo": 7, "otros": 6},
    {"byd": 193, "mg5": 122, "mg3": 44, "aion": 6, "king": 197, "tiggo": 9, "otros": 6},
    {"byd": 131, "mg5": 120, "mg3": 34, "aion": 8, "king": 9, "tiggo": 6, "otros": 4},
]

MODELO_KEYS = ["byd", "mg5", "mg3", "aion", "king", "tiggo", "otros"]
MONTH_LABELS_ES = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]


def get_access_token():
    client_id = os.environ["GOOGLE_CLIENT_ID"]
    client_secret = os.environ["GOOGLE_CLIENT_SECRET"]
    refresh_token = os.environ["GOOGLE_REFRESH_TOKEN"]
    data = urllib.parse.urlencode({
        "client_id": client_id, "client_secret": client_secret,
        "refresh_token": refresh_token, "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data, method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())["access_token"]


def sheets_get(token, sheet_id, rng):
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{urllib.parse.quote(rng)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read()).get("values", [])


class ColumnasFaltantesError(Exception):
    """Se lanza cuando una pestaña ya no trae una columna que el script necesita por nombre.

    Este reporte mide el pulso operativo del día -- NUNCA debe publicar en silencio con datos
    incompletos porque una columna se movió o se renombró en la fuente (ya pasó una vez el
    28-ago-2026 con GEST_FECHA_COMPROMISO_ENTREGA en el Sheet de Fleet: se corrió el rango de
    30 columnas y esa columna cayó justo fuera, y el script no avisó, solo publicó ceros).
    Mejor que el workflow de GitHub Actions truene visiblemente (run en rojo) a que el
    dashboard se vea sano con un dato roto adentro.
    """


def validar_columnas(nombre_fuente, header_row, columnas_requeridas):
    faltantes = [c for c in columnas_requeridas if c not in header_row]
    if faltantes:
        raise ColumnasFaltantesError(
            f"'{nombre_fuente}' ya no trae la(s) columna(s) {faltantes} -- probablemente se "
            f"reordenaron/renombraron en el Sheet. Revisar el encabezado real de esa pestaña "
            f"antes de confiar en el resto de este refresh."
        )


def sheets_append(token, sheet_id, rng, row):
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/"
           f"{urllib.parse.quote(rng)}:append?valueInputOption=USER_ENTERED")
    body = json.dumps({"values": [row]}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def norm_ascii(s):
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def norm_city(s):
    return re.sub(r"[^A-Z]", "", norm_ascii(s).upper())


def norm_name(s):
    s = s.strip()
    if s.startswith("@"):
        s = s[1:]
    return re.sub(r"\s+", " ", norm_ascii(s)).strip().upper()


def parse_date_multi(s, formats):
    s = s.strip()
    for fmt in formats:
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def tier_of(days):
    if days <= 30:
        return "0-30"
    if days <= 60:
        return "31-60"
    if days <= 90:
        return "61-90"
    return "90+"


def business_days_between(d1, d2_inclusive):
    """Cuenta dias lun-vie entre d1 y d2 (ambos incluidos)."""
    n = 0
    cur = d1
    while cur <= d2_inclusive:
        if cur.weekday() < 5:
            n += 1
        cur += datetime.timedelta(days=1)
    return n


def month_workdays(year, month):
    import calendar
    last_day = calendar.monthrange(year, month)[1]
    return business_days_between(datetime.date(year, month, 1), datetime.date(year, month, last_day))


def main():
    token = get_access_token()
    today = datetime.date.today()

    # ---------- Back Office: SEGUIMIENTO ENTREGAS (mes en curso) ----------
    # Rango con margen generoso (43 columnas reales al momento de escribir esto, BZ=78) -- ver
    # ColumnasFaltantesError para por qué el margen y la validación importan aquí.
    seg = sheets_get(token, BO_ID, "'SEGUIMIENTO ENTREGAS'!A1:BZ1000")
    header, rows = seg[0], seg[1:]
    validar_columnas("SEGUIMIENTO ENTREGAS", header,
                      ["Estatus BO", "Ciudad Base", "Modelo", "Nuevo / Semi", "F / Entrega"])
    idx = {h: i for i, h in enumerate(header)}

    def get(r, col):
        i = idx.get(col)
        if i is None or len(r) <= i:
            return ""
        return r[i]

    CITY_MAP = {"TIJUANA": "Tijuana", "CDMX / EDO MEX": "CDMX / Edo Mex", "MONTERREY": "Monterrey",
                "MEXICALI": "Mexicali", "GUADALAJARA": "Guadalajara", "QUERETARO": "Queretaro",
                "MERIDA": "Merida", "PUEBLA": "Puebla", "SALTILLO": "Saltillo", "": ""}
    STAGE_MAP = {"ENTREGADO": "entregado", "LISTO / ENTREGA": "listo", "ENTREGA AGENDADA": "agendada",
                 "CONTRATO GENERAD": "contrato", "C / ENVIADO": "cenv", "LIGA P / ENVIADA": "liga",
                 "ISSUE": "issue", "": None}
    MODEL_MAP = {"BYD DOLPHIN EV": "byd", "MG 5 EXITE": "mg5", "MG 5": "mg5", "MG 5 STYLE": "mg5",
                 "MG 3": "mg3", "AION": "aion", "BYD KING": "king", "CHIREY TIGGO 2 PRO": "tiggo"}

    etapas_count = collections.Counter()
    etapas_ciudades = collections.defaultdict(collections.Counter)
    modelo_mtd = collections.Counter()
    nuevo_semi_mtd = collections.Counter()
    unmapped_status = collections.Counter()

    def parse_fe(s):
        return parse_date_multi(s, ["%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d"])

    entregado_by_day = collections.defaultdict(collections.Counter)
    agendada_by_day = collections.Counter()
    DIAS_KEYS = ["cdmx", "mty", "tij", "qro", "gdl", "mxl", "otros"]
    CITY_TO_DIASKEY = {"CDMX / Edo Mex": "cdmx", "Monterrey": "mty", "Tijuana": "tij",
                       "Queretaro": "qro", "Guadalajara": "gdl", "Mexicali": "mxl",
                       "Merida": "otros", "Puebla": "otros", "Saltillo": "otros"}

    for r in rows:
        raw_status = get(r, "Estatus BO").strip()
        stage = STAGE_MAP.get(raw_status, "__UNMAPPED__")
        city_raw = get(r, "Ciudad Base").strip()
        city = CITY_MAP.get(city_raw, city_raw)
        if stage == "__UNMAPPED__":
            unmapped_status[raw_status] += 1
            continue
        if stage is not None:
            etapas_count[stage] += 1
            if city:
                etapas_ciudades[stage][city] += 1
            if stage == "entregado":
                nuevo_semi_mtd[get(r, "Nuevo / Semi").strip()] += 1
                modelo_raw = get(r, "Modelo").strip()
                mkey = MODEL_MAP.get(modelo_raw, "otros")
                modelo_mtd[mkey] += 1

        fe = parse_fe(get(r, "F / Entrega"))
        if fe and fe.month == today.month and fe.year == today.year:
            diaskey = CITY_TO_DIASKEY.get(city, "otros")
            if raw_status == "ENTREGADO":
                entregado_by_day[fe.day][diaskey] += 1
            elif stage is not None:
                agendada_by_day[fe.day] += 1

    if unmapped_status:
        print("WARNING: Estatus BO sin mapear:", dict(unmapped_status), file=sys.stderr)

    etapas_total = sum(etapas_count.values())
    entregado_mtd = etapas_count.get("entregado", 0)

    dias_present = sorted(set(list(entregado_by_day.keys()) + list(agendada_by_day.keys())))
    dias_labels = [str(d) for d in dias_present]
    entregados_dia = [{k: entregado_by_day[d].get(k, 0) for k in DIAS_KEYS} for d in dias_present]
    agendadas_dia = [agendada_by_day.get(d, 0) for d in dias_present]

    # ---------- Forecast del mes en curso ----------
    yesterday = today - datetime.timedelta(days=1)
    workdays_elapsed = business_days_between(datetime.date(today.year, today.month, 1), yesterday) \
        if yesterday.month == today.month else 0
    actual_elapsed = entregado_mtd - sum(entregado_by_day.get(today.day, {}).values())
    workdays_total = month_workdays(today.year, today.month)
    rate = (actual_elapsed / workdays_elapsed) if workdays_elapsed > 0 else 0
    forecast_total = round(rate * workdays_total)
    mtd_nuevo = nuevo_semi_mtd.get("NUEVO", 0)
    mtd_semi = nuevo_semi_mtd.get("SEMINUEVO", 0)
    mtd_total = mtd_nuevo + mtd_semi
    forecast_nuevo = round(forecast_total * (mtd_nuevo / mtd_total)) if mtd_total else 0
    forecast_semi = forecast_total - forecast_nuevo

    month_label = f"{MONTH_LABELS_ES[today.month-1]} {str(today.year)[2:]}"
    months = MONTHS_CLOSED + [month_label]
    mix = MIX_CLOSED + [{"nuevo": mtd_nuevo, "seminuevo": mtd_semi}]
    modelo = MODELO_CLOSED + [{k: modelo_mtd.get(k, 0) for k in MODELO_KEYS}]

    ciudad_listo = sorted(
        [{"ciudad": c, "value": v} for c, v in etapas_ciudades.get("listo", {}).items()],
        key=lambda d: -d["value"])

    # ---------- Log Inventario Diario: leer, agregar hoy si falta, releer ----------
    log_rng = "'Log Inventario Diario'!A1:J1000"
    log_rows = sheets_get(token, BO_ID, log_rng)
    log_header, log_data = log_rows[0], log_rows[1:]
    today_iso = today.isoformat()
    already_logged = any(row and row[0] == today_iso for row in log_data)
    if not already_logged:
        listo_by_city = etapas_ciudades.get("listo", {})
        new_row = [
            today_iso, str(sum(listo_by_city.values())),
            str(listo_by_city.get("Tijuana", 0)), str(listo_by_city.get("Mexicali", 0)),
            str(listo_by_city.get("Monterrey", 0)), str(listo_by_city.get("Guadalajara", 0)),
            str(listo_by_city.get("Queretaro", 0)), str(listo_by_city.get("CDMX / Edo Mex", 0)),
            str(listo_by_city.get("Merida", 0)), str(listo_by_city.get("Saltillo", 0)),
        ]
        sheets_append(token, BO_ID, "'Log Inventario Diario'!A1:J1", new_row)
        log_data.append(new_row)

    inv_log = []
    for row in log_data[-14:]:  # ultimas 2 semanas
        if not row or not row[0]:
            continue
        d = datetime.datetime.strptime(row[0], "%Y-%m-%d").date()
        inv_log.append({
            "fecha": f"{d.day}-{MONTH_LABELS_ES[d.month-1].lower()}",
            "total": int(row[1] or 0), "tij": int(row[2] or 0),
            "mxl": int(row[3] or 0), "mty": int(row[4] or 0),
        })

    # ---------- Presales-Inventory: Waitlist (raw) ----------
    # Rango con margen generoso (20 columnas reales al momento de escribir esto, AZ=52) -- mismo
    # motivo que SEGUIMIENTO ENTREGAS arriba: antes este rango terminaba justo en la última
    # columna real (T), sin margen para que la pestaña crezca sin romper el pull.
    wl = sheets_get(token, PI_ID, "'Waitlist'!A1:AZ5000")
    wheader, wrows = wl[0], wl[1:]
    validar_columnas("Waitlist", wheader,
                      ["Fecha de solicitud", "agente_sales", "City", "Vehicle",
                       "Estado de Auto", "Estatus", "Fecha Entrega"])
    widx = {h: i for i, h in enumerate(wheader)}

    def wget(r, col):
        i = widx.get(col)
        if i is None or len(r) <= i:
            return ""
        return r[i]

    active = [r for r in wrows if wget(r, "Estatus").strip() == "EN ESPERA DE INVENTARIO ADMIN COMPLETO"]

    tier_totals = collections.Counter()
    city_tier = {c: collections.Counter() for c in CITY_ORDER}
    city_lookup = {norm_city(c): c for c in CITY_ORDER}
    known_norm_map = {norm_name(k): k for k in KNOWN_AGENT_KEYS}
    agent_tier = collections.defaultdict(collections.Counter)
    max_wait_days = 0

    for r in active:
        dt = parse_date_multi(wget(r, "Fecha de solicitud"), ["%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y"])
        if dt is None:
            continue
        wait_days = (today - dt).days
        max_wait_days = max(max_wait_days, wait_days)
        t = tier_of(wait_days)
        tier_totals[t] += 1

        city_raw = wget(r, "City").strip()
        city = city_lookup.get(norm_city(city_raw))
        if city:
            city_tier[city][t] += 1

        agent_raw = wget(r, "agente_sales").strip()
        an = norm_name(agent_raw)
        key = known_norm_map.get(an, ("@" + agent_raw) if agent_raw else "@(vacio)")
        agent_tier[key][t] += 1

    tiers = [{"key": k, "label": f"{k} días", "value": tier_totals.get(k, 0)} for k in TIER_KEYS]
    city_tier_out = sorted(
        [{"ciudad": c, "vals": [city_tier[c].get(t, 0) for t in TIER_KEYS],
          "total": sum(city_tier[c].get(t, 0) for t in TIER_KEYS)} for c in CITY_ORDER],
        key=lambda d: -d["total"])
    agent_tier_out = sorted(
        [{"key": k, "vals": [v.get(t, 0) for t in TIER_KEYS],
          "total": sum(v.get(t, 0) for t in TIER_KEYS)} for k, v in agent_tier.items()],
        key=lambda d: -d["total"])

    DECLINE_DEFS = [("driver1", "Agenda declinada por driver", "Agenda declinada por driver"),
                    ("tl", "Declinado por TL", "Declinado por TL"),
                    ("driver2", "Declinado por driver", "Declinado por driver"),
                    ("perdido", "Perdido", "PERDIDO"),
                    ("rechazado", "Rechazado", "Rechazado")]
    status_counts = collections.Counter(wget(r, "Estatus").strip() for r in wrows)
    decline = [{"key": k, "label": label, "value": status_counts.get(raw, 0)}
               for k, label, raw in DECLINE_DEFS]

    # ---------- Tabla Waitlist (pivot): cruce de verificación, no se usa para render ----------
    tabla = sheets_get(token, PI_ID, "'Tabla Waitlist'!A1:Z200")
    pivot_total = None
    for row in tabla:
        if row and row[0] == "Suma total":
            try:
                pivot_total = int(row[-1])
            except (ValueError, IndexError):
                pivot_total = None
            break
    active_total = len(tier_totals and active) or sum(tier_totals.values())
    if pivot_total is not None and abs(pivot_total - sum(tier_totals.values())) > 5:
        print(f"WARNING: Tabla Waitlist pivot total ({pivot_total}) difiere de raw Waitlist "
              f"({sum(tier_totals.values())}) por más de 5 -- revisar manualmente.", file=sys.stderr)

    # ---------- WAITLIST (gap por ciudad) ----------
    # Usa los mismos city_tier recien calculados como "espera", y "listo" de ETAPAS_CIUDADES.
    listo_by_city = etapas_ciudades.get("listo", {})
    waitlist_gap = sorted([
        {"ciudad": c, "espera": sum(city_tier[c].values()), "listo": listo_by_city.get(c, 0),
         "gap": sum(city_tier[c].values()) - listo_by_city.get(c, 0)}
        for c in CITY_ORDER
    ], key=lambda d: -d["gap"])

    # ---------- Fleet Backlog (RAW DATA) -- SOLO LECTURA ----------
    # Universo = LISTA_TRABAJO == "Backlog Fleet" (lo que Fleet está trabajando y eventualmente
    # se libera a Ventas como inventario), excluyendo TALLER_ESTATUS == "DESFLOTE" (esas nunca
    # llegan a ser nuestro inventario, van a venta de desflote aparte).
    # Rango ancho a propósito (no solo AP:BO) -- el Sheet de Fleet ha reordenado columnas antes
    # (GEST_FECHA_COMPROMISO_ENTREGA se movió de BO a BQ el 28-ago-2026 sin avisar) y como el
    # cruce de columnas de aquí en adelante es siempre por NOMBRE de encabezado (no por índice
    # fijo), un rango de sobra evita que una columna nueva quede fuera del pull sin que se note.
    fleet_block = sheets_get(token, FLEET_ID, "'RAW DATA'!A1:EN8354")
    f_header, f_rows = fleet_block[0], fleet_block[1:]
    validar_columnas("Fleet Backlog / RAW DATA", f_header,
                      ["LISTA_TRABAJO", "UBICACION_ACTUAL", "TALLER_ESTATUS", "GEST_FECHA_COMPROMISO_ENTREGA"])
    f_idx = {h: i for i, h in enumerate(f_header)}

    def fget(r, col):
        i = f_idx.get(col)
        if i is None or len(r) <= i:
            return ""
        return r[i]

    fleet_backlog_all = [r for r in f_rows if fget(r, "LISTA_TRABAJO").strip() == "Backlog Fleet"]
    fleet_desflote_n = sum(1 for r in fleet_backlog_all if fget(r, "TALLER_ESTATUS").strip() == "DESFLOTE")
    fleet_backlog = [r for r in fleet_backlog_all if fget(r, "TALLER_ESTATUS").strip() != "DESFLOTE"]

    FLEET_STAGE_KEYS = ["POR INGRESAR", "EN DIAGNOSTICO", "EN REPARACION", "ENTREGADO", "SIN_ESTATUS", "DESFLOTE"]

    def fleet_map_city(loc):
        l = norm_ascii(loc).upper()
        if "CDMX" in l or "REVOLUCI" in l:
            return "CDMX / Edo Mex"
        if "GDL" in l or "GUADALAJARA" in l:
            return "Guadalajara"
        if "TIJ" in l or "JOYITA" in l:
            return "Tijuana"
        if "MTY" in l or "MONTERREY" in l:
            return "Monterrey"
        if "QUER" in l or "QRO" in l:
            return "Queretaro"
        if "MEXICALI" in l:
            return "Mexicali"
        if "MERIDA" in l or "NEXA" in l:
            return "Merida"
        if "PUE" in l:
            return "Puebla"
        if "SALTILLO" in l:
            return "Saltillo"
        return "Sin identificar"

    # ---- Chart 1: por etapa de taller, por ciudad (incluye DESFLOTE visible, universo = 144) ----
    fleet_city_stage_counts = {c: collections.Counter() for c in CITY_ORDER + ["Sin identificar"]}
    for r in fleet_backlog_all:
        city = fleet_map_city(fget(r, "UBICACION_ACTUAL").strip())
        stage = fget(r, "TALLER_ESTATUS").strip() or "SIN_ESTATUS"
        fleet_city_stage_counts[city][stage] += 1

    fleet_city_stage = sorted(
        [{"ciudad": c, "vals": [fleet_city_stage_counts[c].get(k, 0) for k in FLEET_STAGE_KEYS],
          "total": sum(fleet_city_stage_counts[c].values())} for c in CITY_ORDER + ["Sin identificar"]],
        key=lambda d: -d["total"])
    fleet_city_stage = [d for d in fleet_city_stage if d["total"] > 0]

    # ---- Chart 2: volumen esperado por día, por ciudad (excluye DESFLOTE y "sin fecha") ----
    def fleet_first_date(s):
        s = s.strip()
        if not s:
            return None
        part = s.split("|")[0].strip()
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", part)
        if m:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return None

    FLEET_DAY_KEYS = ["tij", "cdmx", "gdl", "qro", "mty", "otros"]
    FLEET_DAY_CITY_MAP = {"Tijuana": "tij", "CDMX / Edo Mex": "cdmx", "Guadalajara": "gdl",
                           "Queretaro": "qro", "Monterrey": "mty"}

    fleet_sin_fecha_n = 0
    day_buckets = collections.defaultdict(lambda: collections.Counter())
    for r in fleet_backlog:
        fd = fleet_first_date(fget(r, "GEST_FECHA_COMPROMISO_ENTREGA"))
        if fd is None:
            fleet_sin_fecha_n += 1
            continue
        city = fleet_map_city(fget(r, "UBICACION_ACTUAL").strip())
        key = FLEET_DAY_CITY_MAP.get(city, "otros")
        day_buckets[fd][key] += 1

    fleet_dias_labels = [f"{d.day}-{MONTH_LABELS_ES[d.month-1].lower()}" for d in sorted(day_buckets.keys())]
    fleet_by_day = [{k: day_buckets[d].get(k, 0) for k in FLEET_DAY_KEYS} for d in sorted(day_buckets.keys())]
    fleet_vencido_dias = sum(1 for d in day_buckets if d < today)
    fleet_vencido_unidades = sum(sum(day_buckets[d].values()) for d in day_buckets if d < today)

    data = {
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "months": months,
        "mix": mix,
        "modelo": modelo,
        "modelo_keys": MODELO_KEYS,
        "etapas_total": etapas_total,
        "etapas": [{"key": k, "value": etapas_count.get(k, 0)}
                   for k in ["prep", "listo", "agendada", "contrato", "cenv", "liga", "entregado", "issue"]],
        "etapas_ciudades": {k: dict(v) for k, v in etapas_ciudades.items()},
        "entregado_target_pct": 95,
        "ciudad_listo": ciudad_listo,
        "waitlist_gap": waitlist_gap,
        "dias_labels": dias_labels,
        "entregados_dia": entregados_dia,
        "agendadas_dia": agendadas_dia,
        "inv_log": inv_log,
        "tiers": tiers,
        "tiers_total": sum(tier_totals.values()),
        "max_wait_days": max_wait_days,
        "city_tier": city_tier_out,
        "agent_tier": agent_tier_out,
        "decline": decline,
        "decline_total": sum(d["value"] for d in decline),
        "forecast": {
            "workdays_elapsed": workdays_elapsed, "actual_elapsed": actual_elapsed,
            "workdays_total": workdays_total, "rate": round(rate, 2),
            "total": forecast_total, "nuevo": forecast_nuevo, "seminuevo": forecast_semi,
        },
        "corte": {"fecha": today.isoformat(), "mes_label": month_label},
        "fleet_total": len(fleet_backlog),
        "fleet_desflote_n": fleet_desflote_n,
        "fleet_city_stage": fleet_city_stage,
        "fleet_dias_labels": fleet_dias_labels,
        "fleet_by_day": fleet_by_day,
        "fleet_sin_fecha_n": fleet_sin_fecha_n,
        "fleet_vencido_dias": fleet_vencido_dias,
        "fleet_vencido_unidades": fleet_vencido_unidades,
    }

    out_path = os.path.join(os.path.dirname(__file__), "data.js")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("window.__DASHBOARD_DATA__ = ")
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write(";\n")

    print(f"OK -- data.js escrito. Entregado MTD={entregado_mtd}, waitlist activo={sum(tier_totals.values())}, "
          f"etapas_total={etapas_total}, forecast={forecast_total}")


if __name__ == "__main__":
    main()
