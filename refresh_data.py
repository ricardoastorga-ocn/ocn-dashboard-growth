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
import time
import datetime
import unicodedata
import urllib.request
import urllib.parse
import urllib.error
import collections
from zoneinfo import ZoneInfo

MX_TZ = ZoneInfo("America/Mexico_City")

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
                  "Mar 26", "Abr 26", "May 26", "Jun 26", "Jul 26", "Ago 26"]
MIX_CLOSED = [
    {"nuevo": 272, "seminuevo": 37}, {"nuevo": 318, "seminuevo": 22}, {"nuevo": 348, "seminuevo": 42},
    {"nuevo": 420, "seminuevo": 21}, {"nuevo": 374, "seminuevo": 72}, {"nuevo": 421, "seminuevo": 121},
    {"nuevo": 371, "seminuevo": 129}, {"nuevo": 453, "seminuevo": 110}, {"nuevo": 431, "seminuevo": 116},
    {"nuevo": 125, "seminuevo": 237}, {"nuevo": 250, "seminuevo": 327}, {"nuevo": 14, "seminuevo": 298},
    {"nuevo": 21, "seminuevo": 239},
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
    {"byd": 123, "mg5": 88, "mg3": 25, "aion": 5, "king": 6, "tiggo": 11, "otros": 2},
]

MODELO_KEYS = ["byd", "mg5", "mg3", "aion", "king", "tiggo", "otros"]
MONTH_LABELS_ES = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]


# Google (OAuth token endpoint y Sheets API) a veces regresa errores transitorios (503
# Service Unavailable, 429 rate limit, timeouts de red) que no tienen nada que ver con el
# código -- se resuelven solos en segundos. Sin retry, uno de estos tumbaba la corrida COMPLETA
# del día (ej. 3-sep-2026 8am: un solo 503 en la primera llamada mató todo el refresh, incluido
# el Log Inventario Diario, y el dashboard se quedó sin actualizar hasta la siguiente corrida
# programada 3 horas después). Reintenta con backoff SOLO errores transitorios (5xx/429/red);
# un error real (401, 403, 404, ColumnasFaltantesError, etc.) sigue tronando de inmediato --
# nunca hay que esconder un error de verdad detrás de un retry.
def _retry_transient(fn, intentos=4, espera_base=2):
    for intento in range(1, intentos + 1):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            transitorio = e.code >= 500 or e.code == 429
            if not transitorio or intento == intentos:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if intento == intentos:
                raise
        time.sleep(espera_base * (2 ** (intento - 1)))


def get_access_token():
    client_id = os.environ["GOOGLE_CLIENT_ID"]
    client_secret = os.environ["GOOGLE_CLIENT_SECRET"]
    refresh_token = os.environ["GOOGLE_REFRESH_TOKEN"]
    data = urllib.parse.urlencode({
        "client_id": client_id, "client_secret": client_secret,
        "refresh_token": refresh_token, "grant_type": "refresh_token",
    }).encode()

    def _do():
        req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data, method="POST")
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())["access_token"]
    return _retry_transient(_do)


def sheets_get(token, sheet_id, rng):
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{urllib.parse.quote(rng)}"

    def _do():
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read()).get("values", [])
    return _retry_transient(_do)


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

    def _do():
        req = urllib.request.Request(url, data=body, headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    return _retry_transient(_do)


def sheets_update(token, sheet_id, rng, row):
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/"
           f"{urllib.parse.quote(rng)}?valueInputOption=USER_ENTERED")
    body = json.dumps({"values": [row]}).encode()

    def _do():
        req = urllib.request.Request(url, data=body, headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json"}, method="PUT")
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    return _retry_transient(_do)


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
    # GitHub Actions corre en UTC -- "hoy" tiene que calcularse en hora de CDMX (America/Mexico_City,
    # UTC-6/-5) o el dia calendario salta ~6 horas antes de tiempo (ej. 6:51pm CDMX del 31-ago ya
    # cuenta como 1-sep en UTC), rompiendo cualquier corte de "mes en curso", dias vencidos, etc.
    # Bug real detectado por Ricardo 31-ago-2026 -- ver project_dashboard_growth_automation.md.
    today = datetime.datetime.now(MX_TZ).date()

    # ---------- Back Office: SEGUIMIENTO ENTREGAS (mes en curso) ----------
    # Rango con margen generoso (43 columnas reales al momento de escribir esto, BZ=78) -- ver
    # ColumnasFaltantesError para por qué el margen y la validación importan aquí.
    seg = sheets_get(token, BO_ID, "'SEGUIMIENTO ENTREGAS'!A1:BZ1000")
    header, rows = seg[0], seg[1:]
    validar_columnas("SEGUIMIENTO ENTREGAS", header,
                      ["Estatus BO", "Ciudad Base", "Modelo", "Nuevo / Semi", "F / Entrega", "VIN"])
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
    en_prep_por_ciudad = collections.defaultdict(list)

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
        if raw_status == "EN PREPARACION":
            modelo_raw = get(r, "Modelo").strip()
            vin = get(r, "VIN").strip()
            en_prep_por_ciudad[city or "Sin ciudad"].append({"vin": vin, "modelo": modelo_raw})
        if stage == "__UNMAPPED__":
            unmapped_status[raw_status] += 1
            continue
        if stage is not None:
            etapas_count[stage] += 1
            if city:
                etapas_ciudades[stage][city] += 1

        # nuevo_semi_mtd/modelo_mtd son "por mes" (se apilan a MIX_CLOSED/MODELO_CLOSED como el
        # mes en curso) -- deben filtrarse por F/Entrega del mes actual, a diferencia de
        # etapas_count (foto del embudo completo tal cual esta HOY en la fuente, sin filtrar por
        # fecha -- asi se comporto siempre porque Back Office solo tenia un mes a la vez en esta
        # hoja). Bug real detectado 1-sep-2026: al inicio de mes, SEGUIMIENTO ENTREGAS trae tanto
        # colas de agosto sin archivar como los primeros registros de septiembre juntos, y sin
        # este filtro nuevo_semi_mtd/modelo_mtd mezclaban ambos meses bajo la etiqueta del mes
        # nuevo. Ver project_dashboard_growth_automation.md para el detalle completo.
        fe = parse_fe(get(r, "F / Entrega"))
        if fe and fe.month == today.month and fe.year == today.year:
            diaskey = CITY_TO_DIASKEY.get(city, "otros")
            if raw_status == "ENTREGADO":
                entregado_by_day[fe.day][diaskey] += 1
                nuevo_semi_mtd[get(r, "Nuevo / Semi").strip()] += 1
                modelo_raw = get(r, "Modelo").strip()
                mkey = MODEL_MAP.get(modelo_raw, "otros")
                modelo_mtd[mkey] += 1
            elif stage is not None:
                agendada_by_day[fe.day] += 1

    if unmapped_status:
        print("WARNING: Estatus BO sin mapear:", dict(unmapped_status), file=sys.stderr)

    # "En preparación" = ya salió de Fleet, va hacia Sales/Growth -- el equipo debe trabajarlo de
    # inmediato para agendar cita y entregar ASAP. Pedido explícito de Ricardo 3-sep-2026 (ver
    # project_dashboard_growth_automation.md). VIN/Modelo sí están poblados en la fuente; Driver/
    # Numero/Agente/F-Tentativa-Liberacion casi siempre vienen vacíos todavía -- no se muestran.
    en_prep_by_ciudad = sorted(
        [{"ciudad": c, "count": len(items),
          "modelos": [i["modelo"] for i in items],
          "vins": [i["vin"] for i in items if i["vin"]]}
         for c, items in en_prep_por_ciudad.items()],
        key=lambda d: -d["count"])
    en_prep_total = sum(d["count"] for d in en_prep_by_ciudad)

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

    # ---------- Log Inventario Diario: leer, escribir/actualizar hoy, releer ----------
    # La fila de HOY se sobreescribe en cada refresh (no solo se escribe una vez) -- este log
    # es una foto de "Listo/Entrega ahora mismo", no un acumulado del dia, asi que debe reflejar
    # el estado mas reciente cada vez que el pipeline corre, igual que el proceso manual que
    # reemplazo (ver project_mix_flota_report.md, actualizaba in-place varias veces por dia).
    # Bug real encontrado 31-ago-2026: esta version solo escribia una vez (si faltaba la fila del
    # dia) y nunca la volvia a tocar -- Ricardo veia 22-33 unidades Listo/Entrega en vivo mientras
    # el log del dia se quedo congelado en 18, capturado en el primer refresh de la mañana.
    log_rng = "'Log Inventario Diario'!A1:J1000"
    log_rows = sheets_get(token, BO_ID, log_rng)
    log_header, log_data = log_rows[0], log_rows[1:]
    today_iso = today.isoformat()
    today_row_idx = next((i for i, row in enumerate(log_data) if row and row[0] == today_iso), None)

    listo_by_city = etapas_ciudades.get("listo", {})
    new_row = [
        today_iso, str(sum(listo_by_city.values())),
        str(listo_by_city.get("Tijuana", 0)), str(listo_by_city.get("Mexicali", 0)),
        str(listo_by_city.get("Monterrey", 0)), str(listo_by_city.get("Guadalajara", 0)),
        str(listo_by_city.get("Queretaro", 0)), str(listo_by_city.get("CDMX / Edo Mex", 0)),
        str(listo_by_city.get("Merida", 0)), str(listo_by_city.get("Saltillo", 0)),
    ]
    if today_row_idx is None:
        sheets_append(token, BO_ID, "'Log Inventario Diario'!A1:J1", new_row)
        log_data.append(new_row)
    else:
        sheet_row_num = today_row_idx + 2  # +1 por header, +1 por indexado en 1
        sheets_update(token, BO_ID, f"'Log Inventario Diario'!A{sheet_row_num}:J{sheet_row_num}", new_row)
        log_data[today_row_idx] = new_row

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

    # ---------- Back Office: GLOBAL DECLINADOS (agendas de entrega declinadas + recuperación) ----------
    # Regla de negocio (confirmada por Ricardo 30-ago-2026): un asesor tiene 30 dias desde la
    # fecha de agenda declinada para recuperar al cliente -- despues de eso pasa a Contact
    # Center y ya no cuenta en sus comisiones. "vencido" abajo = pendiente con mas de 30 dias.
    DEPARTED_AGENTS = {"araceli olvera", "mariam bangoura", "fernando velazquez",
                        "hector vera", "carlos mejia", "yael munoz"}

    def norm_simple(s):
        return re.sub(r"\s+", " ", norm_ascii(s)).strip().lower()

    decl_raw = sheets_get(token, BO_ID, "'GLOBAL DECLINADOS'!A1:P1001")
    dheader, drows = decl_raw[0], decl_raw[1:]
    validar_columnas("GLOBAL DECLINADOS", dheader,
                      ["Agente", "STATUS DECLINADO", "MOTIVO", "MES DECLINACIÓN", "FECHA AGENDA "])
    didx = {h: i for i, h in enumerate(dheader)}

    def dget(r, col):
        i = didx.get(col)
        if i is None or len(r) <= i:
            return ""
        return r[i]

    MES_ORDER = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
                 "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11,
                 "diciembre": 12}

    # Clasificacion trabajable/no-trabajable por motivo (juicio de negocio, no un dato --
    # confirmar/ajustar con Ricardo si algun motivo deberia cambiar de lado). "Trabajable" =
    # se puede reactivar con la accion correcta (seguimiento, otro modelo, inventario nuevo).
    # "No trabajable" = el cliente ya resolvio su necesidad por otro lado o ya no califica.
    MOTIVO_TRABAJABLE = {
        "DEJA DE CONTESTAR": True, "TUVO UN IMPREVISTO": True, "QUIERE OTRO MODELO": True,
        "NO QUIERE SEMINUEVO": True, "NO CONSIGUIÓ AVAL": True, "NO LE CONVENCE EL CONTRATO": True,
        "LE FATA UN DOCUMENTO": True, "ISSUE MECANICO/ESTETICO": True, "NO CONSIGUE DINERO": True,
        "YA ADQUIRIÓ AUTO": False, "EXPRESA NO SEGUIR": False, "YA NO TRABAJA EN PLATAFORMAS": False,
    }

    agenda_total = agenda_recuperado = agenda_perdido = agenda_pendiente = agenda_vencido = 0
    agenda_by_month = collections.OrderedDict()
    agenda_motivos = collections.Counter()
    agenda_motivo_trabajable = {}
    agenda_by_agent = collections.defaultdict(collections.Counter)
    agenda_orphan = collections.defaultdict(list)
    agenda_semaforo = collections.Counter()  # pendientes por bracket de dias hacia el limite de 30

    for r in drows:
        ciudad_d = (r[0].strip() if len(r) > 0 else "")
        agente_d = dget(r, "Agente").strip()
        status_d = dget(r, "STATUS DECLINADO").strip()
        motivo_d = dget(r, "MOTIVO").strip()
        mes_d = dget(r, "MES DECLINACIÓN").strip()
        fecha_d = dget(r, "FECHA AGENDA ").strip()
        if not agente_d and not ciudad_d:
            continue
        agenda_total += 1
        fecha_parsed = parse_date_multi(fecha_d, ["%d/%m/%Y"])
        age_days = (today - fecha_parsed).days if fecha_parsed else None
        is_pending = status_d == "DECLINADO/SIGUE EN ESPERA"
        is_vencido = is_pending and age_days is not None and age_days > 30

        if status_d == "ENTREGADO":
            agenda_recuperado += 1
        elif status_d == "NO VUELVE A RETOMAR":
            agenda_perdido += 1
        elif is_pending:
            agenda_pendiente += 1
            if is_vencido:
                agenda_vencido += 1
            if age_days is None:
                agenda_semaforo["sin_fecha"] += 1
            elif age_days <= 15:
                agenda_semaforo["verde"] += 1
            elif age_days <= 30:
                agenda_semaforo["amarillo"] += 1
            else:
                agenda_semaforo["rojo"] += 1

        if mes_d:
            mc = agenda_by_month.setdefault(mes_d, collections.Counter())
            mc["total"] += 1
            if status_d == "ENTREGADO":
                mc["recuperado"] += 1
            elif status_d == "NO VUELVE A RETOMAR":
                mc["perdido"] += 1
            elif is_pending:
                mc["pendiente"] += 1

        if motivo_d:
            agenda_motivos[motivo_d] += 1
            if motivo_d not in agenda_motivo_trabajable:
                agenda_motivo_trabajable[motivo_d] = MOTIVO_TRABAJABLE.get(motivo_d, True)

        if agente_d:
            ac = agenda_by_agent[agente_d]
            ac["total"] += 1
            if status_d == "ENTREGADO":
                ac["recuperado"] += 1
            elif status_d == "NO VUELVE A RETOMAR":
                ac["perdido"] += 1
            elif is_pending:
                ac["pendiente"] += 1
                if is_vencido:
                    ac["vencido"] += 1
            if norm_simple(agente_d) in DEPARTED_AGENTS and is_pending:
                agenda_orphan[agente_d].append(age_days if age_days is not None else 0)

    def mes_sort_key(mes_label):
        parts = mes_label.split()
        if len(parts) == 2 and parts[0].lower() in MES_ORDER:
            return (parts[1], MES_ORDER[parts[0].lower()])
        return ("9999", 99)

    agenda_decline_by_month = [
        {"mes": m, "total": c["total"], "recuperado": c.get("recuperado", 0),
         "pendiente": c.get("pendiente", 0), "perdido": c.get("perdido", 0),
         "pct_recuperado": round(c.get("recuperado", 0) / c["total"] * 100, 1) if c["total"] else 0}
        for m, c in sorted(agenda_by_month.items(), key=lambda kv: mes_sort_key(kv[0]))
    ]

    agenda_decline_motivos = [
        {"motivo": k, "count": v, "trabajable": agenda_motivo_trabajable.get(k, True)}
        for k, v in agenda_motivos.most_common()
    ]

    agenda_decline_by_agent = []
    for agente_d, c in agenda_by_agent.items():
        if norm_simple(agente_d) in DEPARTED_AGENTS:
            continue
        total_a = c["total"]
        agenda_decline_by_agent.append({
            "agente": agente_d, "total": total_a,
            "recuperado": c.get("recuperado", 0), "pendiente": c.get("pendiente", 0),
            "vencido": c.get("vencido", 0), "perdido": c.get("perdido", 0),
            "pct_recuperado": round(c.get("recuperado", 0) / total_a * 100, 1) if total_a else 0,
        })
    agenda_decline_by_agent.sort(key=lambda d: -d["total"])

    agenda_decline_orphaned = [
        {"agente": a, "count": len(ages), "min_age": min(ages), "max_age": max(ages)}
        for a, ages in sorted(agenda_orphan.items(), key=lambda kv: -len(kv[1]))
    ]

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
        "en_prep_by_ciudad": en_prep_by_ciudad,
        "en_prep_total": en_prep_total,
        "agenda_decline_kpis": {
            "total": agenda_total, "recuperado": agenda_recuperado,
            "pct_recuperado": round(agenda_recuperado / agenda_total * 100, 1) if agenda_total else 0,
            "pendiente": agenda_pendiente, "vencido": agenda_vencido,
            "pct_vencido_of_pendiente": round(agenda_vencido / agenda_pendiente * 100, 1) if agenda_pendiente else 0,
            "perdido": agenda_perdido,
        },
        "agenda_decline_by_month": agenda_decline_by_month,
        "agenda_decline_semaforo": {
            "verde": agenda_semaforo.get("verde", 0), "amarillo": agenda_semaforo.get("amarillo", 0),
            "rojo": agenda_semaforo.get("rojo", 0),
        },
        "agenda_decline_motivos": agenda_decline_motivos,
        "agenda_decline_by_agent": agenda_decline_by_agent,
        "agenda_decline_orphaned": agenda_decline_orphaned,
    }

    out_path = os.path.join(os.path.dirname(__file__), "data.js")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("window.__DASHBOARD_DATA__ = ")
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write(";\n")

    # ---------- Snapshot de cierre de mes ----------
    # Bug real detectado 1-sep-2026 (ver project_weekly_business_review_ceo.md /
    # project_dashboard_growth_automation.md): el pipeline nunca archivaba el detalle fino del
    # cierre (inventario Listo/Entrega por ciudad, mix nuevo/semi de lo que quedó sin salir) --
    # esa info solo sobrevivió porque Ricardo tomó captura de pantalla justo al cierre de agosto.
    # Para que no vuelva a depender de una captura manual: si HOY es el último día calendario del
    # mes (en hora CDMX), se archiva un snapshot con el detalle de cierre en snapshots/. Corre en
    # cada refresh de ese día (varias veces, 8am-7pm) y se sobreescribe cada vez -- así el snapshot
    # que queda al final del día es el más completo. Solo funciona hacia adelante: no puede
    # reconstruir el detalle de meses ya cerrados sin captura.
    tomorrow = today + datetime.timedelta(days=1)
    if tomorrow.month != today.month:
        snap_dir = os.path.join(os.path.dirname(__file__), "snapshots")
        os.makedirs(snap_dir, exist_ok=True)
        snapshot = {
            "mes_label": month_label,
            "fecha_cierre": today.isoformat(),
            "generado_en": datetime.datetime.now(MX_TZ).isoformat(),
            "entregas_totales_mes": entregado_mtd,
            "mix_nuevo_semi": {"nuevo": mtd_nuevo, "seminuevo": mtd_semi},
            "modelo_mtd": {k: modelo_mtd.get(k, 0) for k in MODELO_KEYS},
            "listo_entrega_por_ciudad": ciudad_listo,
            "listo_entrega_total": sum(d["value"] for d in ciudad_listo),
            "forecast_vs_cierre": {"forecast_total": forecast_total, "entregas_reales": entregado_mtd},
        }
        snap_path = os.path.join(snap_dir, f"cierre_{today.strftime('%Y-%m')}.json")
        with open(snap_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
        print(f"OK -- snapshot de cierre escrito en snapshots/cierre_{today.strftime('%Y-%m')}.json "
              f"(listo/entrega total={snapshot['listo_entrega_total']})")

    print(f"OK -- data.js escrito. Entregado MTD={entregado_mtd}, waitlist activo={sum(tier_totals.values())}, "
          f"etapas_total={etapas_total}, forecast={forecast_total}")


if __name__ == "__main__":
    main()
