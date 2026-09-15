#!/usr/bin/env python3
"""
Refresca los datos del Dashboard - Growth (OCN) desde Google Sheets y escribe data.js.
Corre sin intervención humana (GitHub Actions) o a mano (`python3 refresh_data.py`).

Fuentes:
  - Back Office (GLOBAL OCN + SEGUIMIENTO ENTREGAS)
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

# "Entregados por día" -- Back Office ("SEGUIMIENTO ENTREGAS") solo conserva el mes en curso;
# agosto ya fue archivado/rotado de esa pestaña (confirmado 15-sep-2026: 0 filas con F/Entrega
# en agosto en una lectura en vivo). Igual que MIX_CLOSED/MODELO_CLOSED arriba, agosto se congela
# aqui a mano (extraido del ultimo data.js generado antes de la rotacion, commit 8c81493,
# 2026-08-31 22:46 UTC) y se prepende al mes en curso en cada corrida. Formato: (label "D-ago",
# dict por ciudad igual a DIAS_KEYS, agendadas_pendientes_ese_dia).
DIAS_AGOSTO_CLOSED = [
    ("3-ago", {"cdmx": 0, "mty": 1, "tij": 0, "qro": 0, "gdl": 0, "mxl": 0, "otros": 1}, 0),
    ("4-ago", {"cdmx": 7, "mty": 3, "tij": 1, "qro": 0, "gdl": 0, "mxl": 0, "otros": 0}, 0),
    ("5-ago", {"cdmx": 5, "mty": 5, "tij": 3, "qro": 0, "gdl": 0, "mxl": 0, "otros": 1}, 0),
    ("6-ago", {"cdmx": 6, "mty": 4, "tij": 3, "qro": 0, "gdl": 2, "mxl": 0, "otros": 0}, 0),
    ("7-ago", {"cdmx": 5, "mty": 4, "tij": 4, "qro": 0, "gdl": 0, "mxl": 0, "otros": 0}, 0),
    ("10-ago", {"cdmx": 1, "mty": 3, "tij": 0, "qro": 3, "gdl": 0, "mxl": 0, "otros": 0}, 0),
    ("11-ago", {"cdmx": 1, "mty": 3, "tij": 1, "qro": 0, "gdl": 1, "mxl": 0, "otros": 3}, 0),
    ("12-ago", {"cdmx": 4, "mty": 2, "tij": 2, "qro": 3, "gdl": 0, "mxl": 0, "otros": 1}, 0),
    ("13-ago", {"cdmx": 5, "mty": 2, "tij": 3, "qro": 0, "gdl": 0, "mxl": 0, "otros": 1}, 0),
    ("14-ago", {"cdmx": 3, "mty": 1, "tij": 1, "qro": 0, "gdl": 0, "mxl": 0, "otros": 0}, 0),
    ("17-ago", {"cdmx": 3, "mty": 2, "tij": 1, "qro": 3, "gdl": 4, "mxl": 0, "otros": 1}, 0),
    ("18-ago", {"cdmx": 0, "mty": 2, "tij": 4, "qro": 0, "gdl": 0, "mxl": 3, "otros": 0}, 0),
    ("19-ago", {"cdmx": 2, "mty": 1, "tij": 2, "qro": 0, "gdl": 4, "mxl": 1, "otros": 3}, 0),
    ("20-ago", {"cdmx": 3, "mty": 4, "tij": 1, "qro": 4, "gdl": 1, "mxl": 0, "otros": 2}, 0),
    ("21-ago", {"cdmx": 3, "mty": 2, "tij": 4, "qro": 2, "gdl": 0, "mxl": 3, "otros": 0}, 0),
    ("24-ago", {"cdmx": 3, "mty": 6, "tij": 4, "qro": 0, "gdl": 1, "mxl": 1, "otros": 1}, 0),
    ("25-ago", {"cdmx": 4, "mty": 4, "tij": 1, "qro": 0, "gdl": 0, "mxl": 1, "otros": 0}, 0),
    ("26-ago", {"cdmx": 6, "mty": 5, "tij": 4, "qro": 0, "gdl": 0, "mxl": 0, "otros": 1}, 0),
    ("27-ago", {"cdmx": 4, "mty": 2, "tij": 3, "qro": 0, "gdl": 0, "mxl": 2, "otros": 1}, 0),
    ("28-ago", {"cdmx": 5, "mty": 1, "tij": 2, "qro": 0, "gdl": 0, "mxl": 1, "otros": 2}, 0),
    ("29-ago", {"cdmx": 4, "mty": 2, "tij": 2, "qro": 0, "gdl": 1, "mxl": 0, "otros": 0}, 0),
    ("31-ago", {"cdmx": 4, "mty": 5, "tij": 1, "qro": 0, "gdl": 0, "mxl": 0, "otros": 2}, 9),
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
                      ["Estatus BO", "Ciudad Base", "Modelo", "Nuevo / Semi", "F / Entrega", "VIN", "Agente"])
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

    # "Entregados por día" debe conservar historico desde agosto (no solo el mes en curso) para
    # que Ricardo no tenga que scrollear para ver la tendencia completa -- pedido explicito
    # 15-sep-2026. La fuente en vivo (SEGUIMIENTO ENTREGAS) solo conserva el mes en curso
    # (agosto ya fue archivado de esa pestaña), asi que el rango en vivo arranca en el dia 1
    # del mes en curso -- agosto se cubre aparte via DIAS_AGOSTO_CLOSED (mismo patron que
    # MIX_CLOSED/MODELO_CLOSED). Sin límite superior (igual que el filtro anterior por mes, que
    # tampoco topaba en "hoy" -- una fecha agendada más adelante en el mes seguía contando para
    # la línea de agendadas/pendientes).
    DIAS_RANGE_START = datetime.date(today.year, today.month, 1)

    entregado_by_day = collections.defaultdict(collections.Counter)
    agendada_by_day = collections.Counter()
    entregas_por_agente_mes = collections.Counter()
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

        # nuevo_semi_mtd/modelo_mtd son "por mes" (se apilan a MIX_CLOSED/MODELO_CLOSED como el
        # mes en curso) -- deben filtrarse por F/Entrega del mes actual, a diferencia de
        # etapas_count (foto del embudo completo tal cual esta HOY en la fuente, sin filtrar por
        # fecha -- asi se comporto siempre porque Back Office solo tenia un mes a la vez en esta
        # hoja). Bug real detectado 1-sep-2026: al inicio de mes, SEGUIMIENTO ENTREGAS trae tanto
        # colas de agosto sin archivar como los primeros registros de septiembre juntos, y sin
        # este filtro nuevo_semi_mtd/modelo_mtd mezclaban ambos meses bajo la etiqueta del mes
        # nuevo. Ver project_dashboard_growth_automation.md para el detalle completo.
        fe = parse_fe(get(r, "F / Entrega"))
        is_current_month = fe and fe.month == today.month and fe.year == today.year
        if is_current_month:
            if raw_status == "ENTREGADO":
                nuevo_semi_mtd[get(r, "Nuevo / Semi").strip()] += 1
                modelo_raw = get(r, "Modelo").strip()
                mkey = MODEL_MAP.get(modelo_raw, "otros")
                modelo_mtd[mkey] += 1
                agente = get(r, "Agente").strip()
                if agente:
                    entregas_por_agente_mes[agente] += 1
        if fe and fe >= DIAS_RANGE_START:
            diaskey = CITY_TO_DIASKEY.get(city, "otros")
            if raw_status == "ENTREGADO":
                entregado_by_day[fe][diaskey] += 1
            elif stage is not None:
                agendada_by_day[fe] += 1

    if unmapped_status:
        print("WARNING: Estatus BO sin mapear:", dict(unmapped_status), file=sys.stderr)

    etapas_total = sum(etapas_count.values())
    entregado_mtd = etapas_count.get("entregado", 0)

    dias_present = sorted(set(list(entregado_by_day.keys()) + list(agendada_by_day.keys())))
    dias_labels = [a[0] for a in DIAS_AGOSTO_CLOSED] + [f"{d.day}-{MONTH_LABELS_ES[d.month-1].lower()}" for d in dias_present]
    entregados_dia = [a[1] for a in DIAS_AGOSTO_CLOSED] + [{k: entregado_by_day[d].get(k, 0) for k in DIAS_KEYS} for d in dias_present]
    agendadas_dia = [a[2] for a in DIAS_AGOSTO_CLOSED] + [agendada_by_day.get(d, 0) for d in dias_present]

    entregas_agente_mes = [{"agente": a, "total": n}
                            for a, n in entregas_por_agente_mes.most_common()]

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

    # Log Inventario Diario -- eliminado del dashboard 15-sep-2026 a pedido de Ricardo (no se
    # podia actualizar desde el 10-sep, ver project_dashboard_growth_automation.md para el
    # detalle completo de como se sacaba, por si se retoma a futuro cuando se restaure el
    # acceso de escritura al Back Office).

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

    # "Volumen esperado por día, por ciudad" (Chart 2 de Fleet Backlog) y "Agendas declinadas —
    # recuperación de ventas" (pestaña GLOBAL DECLINADOS) -- eliminados del dashboard 15-sep-2026
    # a pedido de Ricardo (no aportaban valor / no se actualizaban de forma útil). Reemplazado
    # "Agendas declinadas" por el ranking de entregas por asesor (entregas_agente_mes, arriba).

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
        "entregas_agente_mes": entregas_agente_mes,
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
    }

    # ---------- Aprobaciones (fuente: CSVs manuales, ver compute_aprobaciones.py) ----------
    # Este pipeline corre en GitHub Actions (nube, sin acceso a los CSVs locales de Ricardo),
    # asi que esta seccion NO se recalcula aqui -- se recalcula a mano corriendo
    # compute_aprobaciones.py cada vez que Ricardo trae CSVs nuevos, lo que escribe
    # aprobaciones_snapshot.json (committed al repo). Aqui solo se relee ese snapshot ya
    # calculado y se reincrusta en data.js en cada corrida automatica, para que el resto del
    # dashboard se siga refrescando solo sin que esta seccion desaparezca ni truene.
    aprob_snapshot_path = os.path.join(os.path.dirname(__file__), "aprobaciones_snapshot.json")
    if os.path.exists(aprob_snapshot_path):
        with open(aprob_snapshot_path, encoding="utf-8") as f:
            data.update(json.load(f))

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
