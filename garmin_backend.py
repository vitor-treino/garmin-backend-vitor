"""
Garmin Connect Backend — Vitor 21K Dashboard
Com sessão persistente para evitar erro 429 (Too Many Requests)
"""

import os, json, logging, pickle
from datetime import date, timedelta, datetime
from flask import Flask, jsonify
from flask_cors import CORS

try:
    from garminconnect import Garmin, GarminConnectAuthenticationError
except ImportError:
    print("ERRO: pip install flask flask-cors garminconnect python-dotenv")
    exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

GARMIN_EMAIL    = os.getenv("GARMIN_EMAIL", "")
GARMIN_PASSWORD = os.getenv("GARMIN_PASSWORD", "")
PORT            = int(os.getenv("PORT", 5050))
CACHE_FILE      = "/tmp/garmin_cache.json"
SESSION_FILE    = "/tmp/garmin_session.pkl"
CACHE_TTL_MIN   = 60  # aumentado para 60 min para reduzir logins

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app, origins=["*"])

# ── Sessão persistente ────────────────────────────────────────────────────────
def get_garmin_client():
    """
    Reutiliza sessão salva em vez de fazer login toda vez.
    Só faz login novo se a sessão expirar ou não existir.
    """
    if not GARMIN_EMAIL or not GARMIN_PASSWORD:
        raise ValueError("Configure GARMIN_EMAIL e GARMIN_PASSWORD")

    api = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)

    # Tenta carregar sessão salva
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, "rb") as f:
                saved = pickle.load(f)
            api.session_data = saved
            api.login(tokenstore=saved)
            log.info("Sessao Garmin reutilizada (sem novo login)")
            return api
        except Exception as e:
            log.warning(f"Sessao invalida, fazendo novo login: {e}")
            if os.path.exists(SESSION_FILE):
                os.remove(SESSION_FILE)

    # Login novo
    log.info("Fazendo login no Garmin Connect...")
    api.login()

    # Salva sessão para próxima vez
    try:
        with open(SESSION_FILE, "wb") as f:
            pickle.dump(api.session_data, f)
        log.info("Sessao Garmin salva")
    except Exception as e:
        log.warning(f"Nao salvou sessao: {e}")

    return api

# ── Cache de dados ────────────────────────────────────────────────────────────
def load_cache():
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE) as f:
                data = json.load(f)
            age = (datetime.now() - datetime.fromisoformat(data.get("_cached_at","2000-01-01"))).total_seconds()/60
            if age < CACHE_TTL_MIN:
                log.info(f"Cache valido ({age:.0f} min)")
                return data
    except:
        pass
    return None

def save_cache(data):
    data["_cached_at"] = datetime.now().isoformat()
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(data, f, default=str)
    except Exception as e:
        log.warning(f"Cache nao salvo: {e}")

def safe(fn, *args, default=None, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        log.warning(f"skip [{fn.__name__}]: {e}")
        return default

def parse_sleep(raw):
    if not raw:
        return []
    items = raw if isinstance(raw, list) else [raw]
    return [{
        "calendarDate":      s.get("calendarDate",""),
        "totalSleepSeconds": s.get("sleepTimeSeconds", s.get("totalSleepTimeInSeconds",0)),
        "deepSleepSeconds":  s.get("deepSleepSeconds",0),
        "remSleepSeconds":   s.get("remSleepSeconds",0),
        "lightSleepSeconds": s.get("lightSleepSeconds",0),
        "sleepScores":       s.get("sleepScores",{}),
    } for s in items]

# ── Rotas ─────────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "online", "service": "Garmin Backend Vitor 21K"})

@app.route("/health", methods=["GET"])
def health():
    session_exists = os.path.exists(SESSION_FILE)
    return jsonify({
        "status": "ok",
        "configured": bool(GARMIN_EMAIL),
        "session_cached": session_exists
    })

@app.route("/sync", methods=["POST", "GET"])
def sync():
    cached = load_cache()
    if cached:
        return jsonify(cached)

    try:
        api = get_garmin_client()
    except GarminConnectAuthenticationError as e:
        return jsonify({"error": "Autenticacao falhou. Verifique email/senha.", "detail": str(e)}), 401
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        err = str(e)
        if "429" in err:
            return jsonify({"error": "Garmin bloqueou temporariamente (429). Aguarde 30 minutos e tente novamente.", "detail": err}), 429
        return jsonify({"error": err}), 500

    today     = date.today()
    two_weeks = today - timedelta(days=14)
    data      = {}

    stats = safe(api.get_stats, today.isoformat(), default={}) or {}
    data.update({
        "steps":            stats.get("totalSteps"),
        "caloriesActive":   stats.get("activeKilocalories"),
        "intensityMinutes": (stats.get("moderateIntensityMinutes",0) or 0) + (stats.get("vigorousIntensityMinutes",0) or 0)*2,
        "stressAvg":        stats.get("averageStressLevel"),
        "bodyBattery":      stats.get("bodyBatteryChargedValue"),
        "hrResting":        stats.get("restingHeartRate"),
    })

    vo2 = safe(api.get_max_metrics, today.isoformat())
    if vo2 and isinstance(vo2, list) and len(vo2) > 0:
        v = vo2[0].get("generic",{}).get("vo2MaxPreciseValue") or vo2[0].get("running",{}).get("vo2MaxPreciseValue")
        if v: data["vo2max"] = round(float(v), 1)

    hrv = safe(api.get_hrv_data, today.isoformat())
    if hrv:
        data["hrv"] = hrv.get("hrvSummary",{}).get("lastNight") or hrv.get("lastNight")

    data["sleep"] = parse_sleep(safe(api.get_sleep_data, two_weeks.isoformat(), today.isoformat()))

    tr = safe(api.get_training_status, today.isoformat())
    if tr:
        data.update({
            "trainingLoad":   tr.get("latestTrainingLoad"),
            "recoveryTime":   tr.get("recoveryTime"),
            "trainingStatus": tr.get("trainingStatusPhrase")
        })

    acts = safe(api.get_activities, 0, 30) or []
    data["activities"] = sorted([{
        "activityId":    a.get("activityId"),
        "activityName":  a.get("activityName",""),
        "activityType":  a.get("activityType",{}),
        "startTimeLocal":a.get("startTimeLocal",""),
        "distance":      a.get("distance",0),
        "duration":      a.get("duration",0),
        "averageHR":     a.get("averageHR"),
        "maxHR":         a.get("maxHR"),
        "calories":      a.get("calories"),
        "vO2MaxValue":   a.get("vO2MaxValue"),
        "averageCadence":a.get("averageRunningCadenceInStepsPerMinute"),
        "elevationGain": a.get("elevationGain"),
    } for a in acts], key=lambda x: x.get("startTimeLocal",""), reverse=True)

    save_cache(data)
    log.info(f"Sync OK: {len(data['activities'])} atividades")
    return jsonify(data)

@app.route("/clear-cache", methods=["POST"])
def clear_cache():
    if os.path.exists(CACHE_FILE):
        os.remove(CACHE_FILE)
    return jsonify({"status": "cache limpo"})

@app.route("/clear-session", methods=["POST"])
def clear_session():
    """Use apenas se precisar forcar novo login"""
    if os.path.exists(SESSION_FILE):
        os.remove(SESSION_FILE)
    if os.path.exists(CACHE_FILE):
        os.remove(CACHE_FILE)
    return jsonify({"status": "sessao e cache limpos"})

if __name__ == "__main__":
    print(f"\n  Garmin Backend | {GARMIN_EMAIL or 'EMAIL NAO CONFIGURADO'} | porta {PORT}\n")
    app.run(host="0.0.0.0", port=PORT, debug=False)
