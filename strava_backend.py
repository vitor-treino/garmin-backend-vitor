"""
Strava Backend — Vitor 21K Dashboard
Usa a API do Strava (estável, sem bloqueio 429)
O Garmin já sincroniza tudo para o Strava automaticamente.
"""

import os, json, logging, requests
from datetime import datetime, timedelta
from flask import Flask, jsonify, request
from flask_cors import CORS

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Config ────────────────────────────────────────────────────────────────────
STRAVA_CLIENT_ID     = os.getenv("STRAVA_CLIENT_ID", "")
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET", "")
STRAVA_REFRESH_TOKEN = os.getenv("STRAVA_REFRESH_TOKEN", "")
PORT                 = int(os.getenv("PORT", 5050))
CACHE_FILE           = "/tmp/strava_cache.json"
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

# ── Cache ─────────────────────────────────────────────────────────────────────
def load_cache():
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE) as f:
                d = json.load(f)
            age = (datetime.now() - datetime.fromisoformat(
                d.get("_cached_at","2000-01-01"))).total_seconds()/60
            if age < CACHE_TTL_MIN:
                log.info(f"Cache válido ({age:.0f}min)")
                return d
    except: pass
    return None

def save_cache(d):
    d["_cached_at"] = datetime.now().isoformat()
    try:
        with open(CACHE_FILE,"w") as f:
            json.dump(d, f, default=str)
    except Exception as e:
        log.warning(f"Cache não salvo: {e}")

# ── Strava OAuth ──────────────────────────────────────────────────────────────
def get_access_token():
    """
    Usa o refresh_token para gerar um access_token válido.
    O refresh_token não expira — o access_token dura 6 horas.
    """
    if not STRAVA_CLIENT_ID or not STRAVA_CLIENT_SECRET or not STRAVA_REFRESH_TOKEN:
        raise ValueError("Configure STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET e STRAVA_REFRESH_TOKEN")

    r = requests.post("https://www.strava.com/oauth/token", data={
        "client_id":     STRAVA_CLIENT_ID,
        "client_secret": STRAVA_CLIENT_SECRET,
        "refresh_token": STRAVA_REFRESH_TOKEN,
        "grant_type":    "refresh_token",
    })
    r.raise_for_status()
    return r.json()["access_token"]

def strava_get(path, token, params=None):
    """Faz uma chamada GET autenticada na API do Strava."""
    try:
        r = requests.get(
            f"https://www.strava.com/api/v3/{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params or {},
            timeout=15
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"Strava API skip [{path}]: {e}")
        return None

# ── Rotas ─────────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def index():
    return jsonify({"status":"online","service":"Strava Backend Vitor 21K"})

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "configured": bool(STRAVA_CLIENT_ID and STRAVA_REFRESH_TOKEN)
    })

@app.route("/sync", methods=["GET","POST"])
def sync():
    cached = load_cache()
    if cached:
        return jsonify(cached)

    try:
        token = get_access_token()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Erro de autenticação Strava: {str(e)}"}), 401

    data = {}

    # ── Perfil do atleta ──────────────────────────────────────────────────────
    athlete = strava_get("athlete", token)
    if athlete:
        data["profile"] = {
            "displayName": f"{athlete.get('firstname','')} {athlete.get('lastname','')}".strip(),
            "city":        athlete.get("city",""),
            "country":     athlete.get("country",""),
            "weight":      athlete.get("weight"),      # kg
            "ftp":         athlete.get("ftp"),         # FTP ciclismo (pode ser null)
        }

    # ── Estatísticas do atleta ────────────────────────────────────────────────
    if athlete:
        stats = strava_get(f"athletes/{athlete['id']}/stats", token)
        if stats:
            ytd  = stats.get("ytd_run_totals",{})
            all_ = stats.get("all_run_totals",{})
            recent = stats.get("recent_run_totals",{})
            data["stravaStats"] = {
                "ytdRuns":       ytd.get("count"),
                "ytdDistKm":     round(ytd.get("distance",0)/1000, 1),
                "ytdTimeH":      round(ytd.get("moving_time",0)/3600, 1),
                "ytdElevation":  ytd.get("elevation_gain"),
                "allRuns":       all_.get("count"),
                "allDistKm":     round(all_.get("distance",0)/1000, 1),
                "recentRuns":    recent.get("count"),
                "recentDistKm":  round(recent.get("distance",0)/1000, 1),
            }

    # ── Atividades — últimas 30 ───────────────────────────────────────────────
    four_weeks_ago = int((datetime.now() - timedelta(days=28)).timestamp())
    acts_raw = strava_get("athlete/activities", token, params={
        "after":    four_weeks_ago,
        "per_page": 50,
        "page":     1,
    }) or []

    activities = []
    for a in acts_raw:
        dist_km = round(a.get("distance",0)/1000, 2)
        dur_sec = a.get("moving_time", 0)
        pace_sec = (dur_sec / dist_km) if dist_km > 0 else 0
        pace_min = int(pace_sec//60)
        pace_s   = int(pace_sec%60)

        activities.append({
            "activityId":    a.get("id"),
            "activityName":  a.get("name",""),
            "activityType":  {"typeKey": a.get("sport_type","").lower()},
            "startTimeLocal":a.get("start_date_local",""),
            "distance":      a.get("distance",0),       # metros
            "distanceKm":    dist_km,
            "duration":      dur_sec,                   # segundos
            "paceFormatted": f"{pace_min}:{str(pace_s).zfill(2)}/km" if pace_sec else "--",
            "averageHR":     a.get("average_heartrate"),
            "maxHR":         a.get("max_heartrate"),
            "calories":      a.get("calories"),
            "elevationGain": a.get("total_elevation_gain"),
            "averageCadence":a.get("average_cadence"),
            "averageSpeed":  round(a.get("average_speed",0)*3.6, 1),  # km/h
            "maxSpeed":      round(a.get("max_speed",0)*3.6, 1),
            "kudos":         a.get("kudos_count"),
            "sufferScore":   a.get("suffer_score"),
            "deviceName":    a.get("device_name",""),
            "hasHeartRate":  a.get("has_heartrate", False),
        })

    data["activities"] = activities

    # ── Métricas calculadas das últimas corridas ───────────────────────────────
    runs = [a for a in activities if "run" in a["activityType"].get("typeKey","")]
    if runs:
        paces = [a["duration"]/a["distanceKm"] for a in runs if a["distanceKm"]>0]
        hrs   = [a["averageHR"] for a in runs if a["averageHR"]]
        cals  = [a["calories"]  for a in runs if a["calories"]]
        dists = [a["distanceKm"] for a in runs]

        data["runMetrics"] = {
            "totalRuns":     len(runs),
            "totalKm":       round(sum(dists), 1),
            "avgPaceSec":    round(sum(paces)/len(paces)) if paces else None,
            "bestPaceSec":   round(min(paces)) if paces else None,
            "avgHR":         round(sum(hrs)/len(hrs)) if hrs else None,
            "avgCalories":   round(sum(cals)/len(cals)) if cals else None,
            "longestRunKm":  round(max(dists), 1) if dists else None,
        }

    # ── Semana atual ──────────────────────────────────────────────────────────
    hoje   = datetime.now()
    seg    = hoje - timedelta(days=hoje.weekday())
    seg    = seg.replace(hour=0,minute=0,second=0,microsecond=0)
    week_acts = [a for a in activities
                 if "run" in a["activityType"].get("typeKey","")
                 and datetime.fromisoformat(a["startTimeLocal"].replace("Z","")) >= seg]
    data["weekRuns"] = {
        "count":  len(week_acts),
        "distKm": round(sum(a["distanceKm"] for a in week_acts), 1),
        "timeMin": round(sum(a["duration"] for a in week_acts)/60, 0),
    }

    save_cache(data)
    log.info(f"Sync OK: {len(activities)} atividades, {len(runs)} corridas")
    return jsonify(data)

@app.route("/clear-cache", methods=["GET","POST"])
def clear_cache():
    if os.path.exists(CACHE_FILE): os.remove(CACHE_FILE)
    return jsonify({"status":"cache limpo"})

if __name__ == "__main__":
    print(f"\n  Strava Backend | Client ID: {STRAVA_CLIENT_ID or 'NAO CONFIGURADO'} | porta {PORT}\n")
    app.run(host="0.0.0.0", port=PORT, debug=False)
