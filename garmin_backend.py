"""
Vitor Treino Backend — Strava + Garmin + PostgreSQL
Dados persistentes entre dispositivos via PostgreSQL (Railway)
"""

import os, json, logging, pickle, requests
from datetime import date, timedelta, datetime
from flask import Flask, jsonify, request
from flask_cors import CORS

try:
    from garminconnect import Garmin, GarminConnectAuthenticationError
except ImportError:
    Garmin = None

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Config ────────────────────────────────────────────────────────────────────
STRAVA_CLIENT_ID     = os.getenv("STRAVA_CLIENT_ID", "")
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET", "")
STRAVA_REFRESH_TOKEN = os.getenv("STRAVA_REFRESH_TOKEN", "")
GARMIN_EMAIL         = os.getenv("GARMIN_EMAIL", "")
GARMIN_PASSWORD      = os.getenv("GARMIN_PASSWORD", "")
DATABASE_URL         = os.getenv("DATABASE_URL", "")
PORT                 = int(os.getenv("PORT", 5050))
CACHE_FILE           = "/tmp/vitor_cache.json"
SESSION_FILE         = "/tmp/garmin_session.pkl"
CACHE_TTL_MIN        = 60

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}},
     allow_headers=["Content-Type"], methods=["GET","POST","OPTIONS"])

@app.after_request
def cors_headers(r):
    r.headers["Access-Control-Allow-Origin"]  = "*"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type"
    r.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return r

@app.route("/<path:p>", methods=["OPTIONS"])
@app.route("/", methods=["OPTIONS"])
def options(p=""): return jsonify({"ok": True}), 200

# ══════════════════════════════════════════════════════════════════
#  BANCO DE DADOS — PostgreSQL (Railway) com fallback para JSON
# ══════════════════════════════════════════════════════════════════
def get_db():
    """Retorna conexão PostgreSQL ou None se não disponível."""
    if not DATABASE_URL:
        return None
    try:
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        return conn
    except Exception as e:
        log.warning(f"PostgreSQL indisponível: {e}")
        return None

def init_db():
    """Cria tabela de dados se não existir."""
    conn = get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS userdata (
                key VARCHAR(50) PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        log.info("PostgreSQL: tabela OK")
    except Exception as e:
        log.warning(f"Init DB error: {e}")

def db_get(key):
    """Busca valor do PostgreSQL."""
    conn = get_db()
    if not conn:
        # Fallback: arquivo JSON
        f = f"/tmp/vitor_{key}.json"
        try:
            if os.path.exists(f):
                with open(f) as fp:
                    return json.load(fp)
        except: pass
        return None
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM userdata WHERE key = %s", (key,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return json.loads(row[0]) if row else None
    except Exception as e:
        log.warning(f"DB get error: {e}")
        return None

def db_set(key, value):
    """Salva valor no PostgreSQL."""
    conn = get_db()
    if not conn:
        # Fallback: arquivo JSON
        f = f"/tmp/vitor_{key}.json"
        try:
            with open(f, 'w') as fp:
                json.dump(value, fp, default=str)
            return True
        except: return False
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO userdata (key, value, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value, updated_at = NOW()
        """, (key, json.dumps(value, default=str)))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        log.warning(f"DB set error: {e}")
        return False

# ── Cache ─────────────────────────────────────────────────────────────────────
def load_cache():
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE) as f:
                d = json.load(f)
            age = (datetime.now() - datetime.fromisoformat(
                d.get("_cached_at","2000-01-01"))).total_seconds()/60
            if age < CACHE_TTL_MIN:
                return d
    except: pass
    return None

def save_cache(d):
    d["_cached_at"] = datetime.now().isoformat()
    try:
        with open(CACHE_FILE,"w") as f:
            json.dump(d, f, default=str)
    except: pass

# ── STRAVA ────────────────────────────────────────────────────────────────────
def get_strava_token():
    r = requests.post("https://www.strava.com/oauth/token", data={
        "client_id": STRAVA_CLIENT_ID, "client_secret": STRAVA_CLIENT_SECRET,
        "refresh_token": STRAVA_REFRESH_TOKEN, "grant_type": "refresh_token",
    }, timeout=15)
    r.raise_for_status()
    return r.json()["access_token"]

def strava_get(path, token, params=None):
    try:
        r = requests.get(f"https://www.strava.com/api/v3/{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params or {}, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"Strava skip [{path}]: {e}")
        return None

def fetch_strava(data):
    if not STRAVA_CLIENT_ID or not STRAVA_REFRESH_TOKEN:
        return data
    try:
        token   = get_strava_token()
        athlete = strava_get("athlete", token)
        if athlete:
            data["profile"] = {
                "displayName": f"{athlete.get('firstname','')} {athlete.get('lastname','')}".strip(),
                "city": athlete.get("city",""), "weight": athlete.get("weight"),
            }
            stats = strava_get(f"athletes/{athlete['id']}/stats", token)
            if stats:
                ytd    = stats.get("ytd_run_totals",{})
                recent = stats.get("recent_run_totals",{})
                data["stravaStats"] = {
                    "ytdRuns": ytd.get("count"), "ytdDistKm": round(ytd.get("distance",0)/1000,1),
                    "ytdTimeH": round(ytd.get("moving_time",0)/3600,1),
                    "ytdElevation": ytd.get("elevation_gain"),
                    "recentRuns": recent.get("count"),
                    "recentDistKm": round(recent.get("distance",0)/1000,1),
                }

        four_weeks_ago = int((datetime.now()-timedelta(days=28)).timestamp())
        acts_raw = strava_get("athlete/activities", token,
            params={"after": four_weeks_ago, "per_page": 50, "page": 1}) or []

        activities = []
        for a in acts_raw:
            dist_km = round(a.get("distance",0)/1000, 2)
            dur_sec = a.get("moving_time", 0)
            pace_sec = (dur_sec/dist_km) if dist_km > 0 else 0
            pm, ps = int(pace_sec//60), int(pace_sec%60)
            activities.append({
                "activityId": a.get("id"), "activityName": a.get("name",""),
                "activityType": {"typeKey": a.get("sport_type","").lower()},
                "startTimeLocal": a.get("start_date_local",""),
                "distance": a.get("distance",0), "distanceKm": dist_km,
                "duration": dur_sec,
                "paceFormatted": f"{pm}:{str(ps).zfill(2)}/km" if pace_sec else "--",
                "averageHR": a.get("average_heartrate"), "maxHR": a.get("max_heartrate"),
                "calories": a.get("calories"), "elevationGain": a.get("total_elevation_gain"),
                "averageCadence": a.get("average_cadence"), "sufferScore": a.get("suffer_score"),
                "deviceName": a.get("device_name",""), "hasHeartRate": a.get("has_heartrate",False),
            })
        data["activities"] = activities

        runs = [a for a in activities if "run" in a["activityType"].get("typeKey","")]
        if runs:
            paces = [a["duration"]/a["distanceKm"] for a in runs if a["distanceKm"]>0]
            hrs   = [a["averageHR"] for a in runs if a["averageHR"]]
            dists = [a["distanceKm"] for a in runs]
            data["runMetrics"] = {
                "totalRuns": len(runs), "totalKm": round(sum(dists),1),
                "avgPaceSec": round(sum(paces)/len(paces)) if paces else None,
                "bestPaceSec": round(min(paces)) if paces else None,
                "avgHR": round(sum(hrs)/len(hrs)) if hrs else None,
                "longestRunKm": round(max(dists),1) if dists else None,
            }

        hoje = datetime.now()
        seg  = (hoje-timedelta(days=hoje.weekday())).replace(hour=0,minute=0,second=0,microsecond=0)
        week = [a for a in activities
                if "run" in a["activityType"].get("typeKey","")
                and datetime.fromisoformat(a["startTimeLocal"].replace("Z","")) >= seg]
        data["weekRuns"] = {
            "count": len(week), "distKm": round(sum(a["distanceKm"] for a in week),1),
            "timeMin": round(sum(a["duration"] for a in week)/60,0),
        }
        log.info(f"Strava OK: {len(activities)} atividades")
    except Exception as e:
        log.warning(f"Strava erro: {e}")
    return data

# ── GARMIN ────────────────────────────────────────────────────────────────────
def get_garmin_client():
    if not Garmin or not GARMIN_EMAIL or not GARMIN_PASSWORD:
        raise ValueError("Garmin não configurado")
    api = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, "rb") as f:
                saved = pickle.load(f)
            api.session_data = saved
            api.login(tokenstore=saved)
            return api
        except:
            try: os.remove(SESSION_FILE)
            except: pass
    api.login()
    try:
        with open(SESSION_FILE, "wb") as f:
            pickle.dump(api.session_data, f)
    except: pass
    return api

def safe(fn, *args, default=None, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        log.warning(f"Garmin skip [{fn.__name__}]: {e}")
        return default

def parse_sleep(raw):
    if not raw: return []
    items = raw if isinstance(raw, list) else [raw]
    return [{"calendarDate": s.get("calendarDate",""),
             "totalSleepSeconds": s.get("sleepTimeSeconds",s.get("totalSleepTimeInSeconds",0)),
             "deepSleepSeconds": s.get("deepSleepSeconds",0),
             "remSleepSeconds": s.get("remSleepSeconds",0),
             "lightSleepSeconds": s.get("lightSleepSeconds",0),
             "sleepScores": s.get("sleepScores",{})} for s in items]

def fetch_garmin(data):
    try:
        api   = get_garmin_client()
        today = date.today()
        stats = safe(api.get_stats, today.isoformat(), default={}) or {}
        data.update({
            "steps": stats.get("totalSteps"),
            "caloriesActive": stats.get("activeKilocalories"),
            "intensityMinutes": (stats.get("moderateIntensityMinutes",0) or 0) +
                                (stats.get("vigorousIntensityMinutes",0) or 0)*2,
            "stressAvg": stats.get("averageStressLevel"),
            "bodyBattery": stats.get("bodyBatteryChargedValue"),
            "hrResting": stats.get("restingHeartRate"),
        })
        vo2 = safe(api.get_max_metrics, today.isoformat())
        if vo2 and isinstance(vo2, list) and len(vo2) > 0:
            v = (vo2[0].get("generic",{}).get("vo2MaxPreciseValue") or
                 vo2[0].get("running",{}).get("vo2MaxPreciseValue"))
            if v: data["vo2max"] = round(float(v), 1)
        hrv = safe(api.get_hrv_data, today.isoformat())
        if hrv:
            data["hrv"] = hrv.get("hrvSummary",{}).get("lastNight") or hrv.get("lastNight")
        two_weeks = today - timedelta(days=14)
        data["sleep"] = parse_sleep(safe(api.get_sleep_data, two_weeks.isoformat(), today.isoformat()))
        tr = safe(api.get_training_status, today.isoformat())
        if tr:
            data.update({"trainingLoad": tr.get("latestTrainingLoad"),
                         "recoveryTime": tr.get("recoveryTime"),
                         "trainingStatus": tr.get("trainingStatusPhrase")})
        log.info("Garmin OK")
    except Exception as e:
        log.warning(f"Garmin indisponível: {e}")
        data["garminError"] = str(e)
    return data

# ── Rotas ─────────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "online", "service": "Vitor Treino Backend", "db": bool(DATABASE_URL)})

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "strava_configured": bool(STRAVA_CLIENT_ID and STRAVA_REFRESH_TOKEN),
        "garmin_configured": bool(GARMIN_EMAIL and GARMIN_PASSWORD),
        "session_cached": os.path.exists(SESSION_FILE),
        "database": "postgresql" if DATABASE_URL else "file_fallback",
    })

@app.route("/sync", methods=["GET","POST"])
def sync():
    cached = load_cache()
    if cached:
        return jsonify(cached)
    data = {}
    data = fetch_strava(data)
    data = fetch_garmin(data)
    save_cache(data)
    return jsonify(data)

@app.route("/clear-cache", methods=["GET","POST"])
def clear_cache():
    if os.path.exists(CACHE_FILE): os.remove(CACHE_FILE)
    return jsonify({"status": "cache limpo"})

@app.route("/clear-session", methods=["GET","POST"])
def clear_session():
    for f in [SESSION_FILE, CACHE_FILE]:
        if os.path.exists(f): os.remove(f)
    return jsonify({"status": "sessão e cache limpos"})

# ── Dados pessoais (PostgreSQL) ───────────────────────────────────────────────
@app.route("/userdata", methods=["GET"])
def get_userdata():
    data = db_get("userdata") or {
        "completedTrainings": {}, "trainingTimes": {},
        "trainingMetrics": {}, "bioData": [], "gymLogs": {},
        "saudeHistory": []
    }
    return jsonify(data)

@app.route("/userdata", methods=["POST"])
def set_userdata():
    try:
        incoming = request.get_json(force=True) or {}
        current  = db_get("userdata") or {
            "completedTrainings": {}, "trainingTimes": {},
            "trainingMetrics": {}, "bioData": [], "gymLogs": {},
            "saudeHistory": []
        }
        # Merge dicts
        for key in ["completedTrainings", "trainingTimes", "trainingMetrics", "gymLogs"]:
            if key in incoming:
                if not isinstance(current.get(key), dict):
                    current[key] = {}
                current[key].update(incoming[key])
        # bioData: evita duplicatas por data
        if "bioData" in incoming and isinstance(incoming["bioData"], list):
            existing = {b.get("data") for b in current.get("bioData", [])}
            for b in incoming["bioData"]:
                if b.get("data") not in existing:
                    current.setdefault("bioData", []).append(b)
                    existing.add(b.get("data"))
            current["bioData"].sort(key=lambda x: x.get("data",""))
        # saudeHistory: mesmo padrão por data
        if "saudeHistory" in incoming and isinstance(incoming["saudeHistory"], list):
            existing = {s.get("data") for s in current.get("saudeHistory", [])}
            for s in incoming["saudeHistory"]:
                if s.get("data") not in existing:
                    current.setdefault("saudeHistory", []).append(s)
                    existing.add(s.get("data"))
            current["saudeHistory"].sort(key=lambda x: x.get("data",""))
        current["_updated"] = datetime.now().isoformat()
        db_set("userdata", current)
        return jsonify({"status": "ok", "db": "postgresql" if DATABASE_URL else "file"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/userdata/reset", methods=["POST"])
def reset_userdata():
    db_set("userdata", {})
    return jsonify({"status": "resetado"})

# ── Init ──────────────────────────────────────────────────────────────────────
init_db()

if __name__ == "__main__":
    print(f"\n  Vitor Treino Backend")
    print(f"  Strava: {bool(STRAVA_CLIENT_ID)} | Garmin: {bool(GARMIN_EMAIL)} | DB: {'PostgreSQL' if DATABASE_URL else 'File'}")
    print(f"  Porta: {PORT}\n")
    app.run(host="0.0.0.0", port=PORT, debug=False)
