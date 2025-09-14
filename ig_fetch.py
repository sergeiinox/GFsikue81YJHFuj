# ig_fetch.py
import os, csv, json, time, pathlib, math
from datetime import datetime, timedelta, timezone

import requests

BASE = "https://graph.facebook.com/v23.0"
TOKEN = os.environ["META_TOKEN"]       # ← подставляется из Secrets
IG_ID  = os.environ["IG_USER_ID"]

out = pathlib.Path("out"); out.mkdir(exist_ok=True)

# --- Дата-окно: последние 28 дней (сегодня включительно) ---
# GitHub Actions работает в UTC; нам достаточно календарных дат ISO.
UNTIL_DT = datetime.utcnow().date()
SINCE_DT = UNTIL_DT - timedelta(days=27)
SINCE = SINCE_DT.isoformat()   # YYYY-MM-DD
UNTIL = UNTIL_DT.isoformat()

# --- Хелперы ---
def ru_date(iso):
    months = ["января","февраля","марта","апреля","мая","июня","июля","августа","сентября","октября","ноября","декабря"]
    y,m,d = iso.split("-"); return f"{int(d)} {months[int(m)-1]} {y}"

def get_json(path, params, retries=3):
    params = dict(params or {})
    params["access_token"] = TOKEN
    url = f"{BASE}/{path}"
    last_err = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=40)
            if r.status_code == 200:
                return r.json()
            # иногда Meta отвечает 400 c JSON-ошибкой — тоже разберём
            try:
                j = r.json()
            except Exception:
                j = {"error": {"message": r.text}}
            last_err = j
        except requests.RequestException as e:
            last_err = {"error": {"message": str(e)}}
        time.sleep(2*(i+1))
    raise SystemExit(f"API error: {json.dumps(last_err, ensure_ascii=False)}")

def pick_total(resp, name):
    x = next((d for d in resp.get("data", []) if d.get("name")==name), None)
    return (((x or {}).get("total_value") or {}).get("value")) or 0

def breakdown_map(resp, name, key_field):
    """
    Возвращает dict по разрезу (например, {'REELS': 123, 'FEED': 45})
    """
    res = {}
    x = next((d for d in resp.get("data", []) if d.get("name")==name), None)
    if not x: return res
    b = (((x.get("total_value") or {}).get("breakdowns")) or [])
    if not b: return res
    results = (b[0] or {}).get("results") or []
    for r in results:
        vals = (r.get("dimension_values") or [])
        if vals:
            res[vals[0]] = r.get("value", 0)
    return res

def write_csv(path, rows, header):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        for row in rows:
            w.writerow(row)

# --- 1) Тоталы за окно (views, profile_views, website_clicks, profile_links_taps, reach) ---
totals = get_json(f"{IG_ID}/insights", {
    "metric": "views,profile_views,website_clicks,profile_links_taps,reach",
    "metric_type": "total_value",
    "period": "day",
    "since": SINCE,
    "until": UNTIL
})

views_total         = pick_total(totals, "views")
profile_views_total = pick_total(totals, "profile_views")
website_clicks      = pick_total(totals, "website_clicks")
profile_links_taps  = pick_total(totals, "profile_links_taps")
reach_total         = pick_total(totals, "reach")

write_csv("out/totals.csv",
          [[SINCE, UNTIL, views_total, profile_views_total, website_clicks, profile_links_taps, reach_total]],
          ["since","until","views","profile_views","website_clicks","profile_links_taps","reach"])


# --- 2) Дневные ряды ---
# a) reach — всегда как time_series
series_reach = get_json(f"{IG_ID}/insights", {
    "metric": "reach",
    "metric_type": "time_series",
    "period": "day",
    "since": SINCE,
    "until": UNTIL
})
reach_vals = next((d for d in series_reach.get("data", []) if d.get("name")=="reach"), {}).get("values", [])

# b) content_views — пытаемся как time_series; если API не поддерживает, просто пропускаем
cv_vals = []
try:
    series_cv = get_json(f"{IG_ID}/insights", {
        "metric": "content_views",
        "metric_type": "time_series",
        "period": "day",
        "since": SINCE,
        "until": UNTIL
    })
    cv_vals = next((d for d in series_cv.get("data", []) if d.get("name")=="content_views"), {}).get("values", [])
    CV_TS_OK = True
except SystemExit:
    CV_TS_OK = False  # у аккаунта нет time_series для content_views

# собираем daily.csv; если content_views нет — оставляем 0 и vpr=0
by_date = {}
for v in reach_vals:
    d = v.get("end_time","")[:10]
    by_date.setdefault(d, {"reach":0,"content_views":0})
    by_date[d]["reach"] = v.get("value", 0)
for v in cv_vals:
    d = v.get("end_time","")[:10]
    by_date.setdefault(d, {"reach":0,"content_views":0})
    by_date[d]["content_views"] = v.get("value", 0)

daily_rows = []
for d in sorted(by_date.keys()):
    r = by_date[d]["reach"]; cv = by_date[d]["content_views"]
    vpr = (cv / r) if (r and cv) else 0
    daily_rows.append([d, r, cv, round(vpr, 3)])

write_csv("out/daily.csv", daily_rows, ["date","reach","content_views","views_per_reach"])


# --- 3) Разрез по подписке (FOLLOWER/NON_FOLLOWER) — тоталы за окно ---
by_follow = get_json(f"{IG_ID}/insights", {
    "metric": "views,reach",
    "metric_type": "total_value",
    "breakdown": "follow_type",
    "period": "day",
    "since": SINCE,
    "until": UNTIL
})

follow_views = breakdown_map(by_follow, "views", "follow_type")
follow_reach = breakdown_map(by_follow, "reach", "follow_type")

write_csv("out/followers.csv",
          [["views", follow_views.get("FOLLOWER",0), follow_views.get("NON_FOLLOWER",0), follow_views.get("UNKNOWN",0)],
           ["reach", follow_reach.get("FOLLOWER",0), follow_reach.get("NON_FOLLOWER",0), follow_reach.get("UNKNOWN",0)]],
          ["metric","follower","non_follower","unknown"])

# --- 4) Разрез по поверхности (FEED/STORY/REELS/AD) — тоталы за окно ---
def surface_insights(metric):
    return get_json(f"{IG_ID}/insights", {
        "metric": metric,
        "metric_type": "total_value",
        "breakdown": "media_product_type",
        "period": "day",
        "since": SINCE,
        "until": UNTIL
    })

# 4a) пробуем content_views + reach
by_surface_cv = surface_insights("content_views")
by_surface_r  = surface_insights("reach")
surf_cv    = breakdown_map(by_surface_cv, "content_views", "media_product_type")
surf_reach = breakdown_map(by_surface_r,  "reach",         "media_product_type")

# 4b) если content_views пуст — fallback на views
if sum(surf_cv.values()) == 0:
    by_surface_v = surface_insights("views")
    surf_cv = breakdown_map(by_surface_v, "views", "media_product_type")

surfaces = ["REELS", "STORY", "FEED", "AD"]
rows = []
for s in surfaces:
    v = surf_cv.get(s, 0)
    r = surf_reach.get(s, 0)
    rows.append([s, v, r, round((v/r if r else 0), 3)])

write_csv("out/surfaces.csv", rows, ["surface", "content_views", "reach", "views_per_reach"])


# --- 5) Вовлечённость (итоги за окно) ---
eng = get_json(f"{IG_ID}/insights", {
    "metric": "accounts_engaged,total_interactions,likes,comments,shares,saves,replies",
    "metric_type": "total_value",
    "period": "day",
    "since": SINCE,
    "until": UNTIL
})
def gtot(name): return pick_total(eng, name)
write_csv("out/engagement_totals.csv",
          [[SINCE, UNTIL, gtot("accounts_engaged"), gtot("total_interactions"),
            gtot("likes"), gtot("comments"), gtot("shares"), gtot("saves"), gtot("replies")]],
          ["since","until","accounts_engaged","total_interactions","likes","comments","shares","saves","replies"])

# --- 6) Аудитория ---
# a) follower_count — дневной ряд (time_series)
aud_fc = get_json(f"{IG_ID}/insights", {
    "metric": "follower_count",
    "metric_type": "time_series",
    "period": "day",
    "since": SINCE,
    "until": UNTIL
})
fc_vals = next((d for d in aud_fc.get("data", []) if d.get("name")=="follower_count"), {}).get("values", [])

# делаем daily CSV с follower_count и чистым приростом от дня к дню (net_delta)
aud_rows = []
prev = None
for v in fc_vals:
    d = v.get("end_time","")[:10]
    cnt = v.get("value", 0)
    net = (cnt - prev) if prev is not None else 0
    aud_rows.append([d, cnt, net])
    prev = cnt

write_csv("out/audience_daily.csv", aud_rows, ["date","follower_count","net_delta"])

# b) follows_and_unfollows — ТОЛЬКО итог за окно (total_value)
aud_fu = get_json(f"{IG_ID}/insights", {
    "metric": "follows_and_unfollows",
    "metric_type": "total_value",
    "period": "day",
    "since": SINCE,
    "until": UNTIL
})
fu_val = next((d for d in aud_fu.get("data", []) if d.get("name")=="follows_and_unfollows"), None)
follows = unfollows = 0
if fu_val and fu_val.get("total_value", {}).get("value"):
    follows   = fu_val["total_value"]["value"].get("follows", 0)
    unfollows = fu_val["total_value"]["value"].get("unfollows", 0)

# запишем отдельный CSV с итогами
write_csv("out/audience_totals.csv",
          [[SINCE, UNTIL, follows, unfollows, follows - unfollows]],
          ["since","until","follows","unfollows","net_follows"])


# --- 7) Демография ---
# Требования: metric_type=total_value, period обязателен; для engaged/reached демографии —
# timeframe: this_month (или this_week). Разрезы запрашиваем по одному: breakdown=age|gender|country|city.

def get_demo(metric, breakdown, timeframe=None):
    params = {
        "metric": metric,
        "metric_type": "total_value",
        "period": "day",
        "breakdown": breakdown
    }
    if timeframe:
        params["timeframe"] = timeframe
    return get_json(f"{IG_ID}/insights", params)

# 7.1 engaged / reached — this_month (если пусто/ошибка — пробуем this_week)
def fetch_demo_safe(metric, breakdown):
    try:
        return get_demo(metric, breakdown, timeframe="this_month")
    except SystemExit:
        try:
            return get_demo(metric, breakdown, timeframe="this_week")
        except SystemExit:
            return {"data": []}

# 7.2 follower_demographics — без timeframe (если вернёт ошибку — попробуем this_month)
def fetch_follower_demo(breakdown):
    try:
        return get_demo("follower_demographics", breakdown)
    except SystemExit:
        try:
            return get_demo("follower_demographics", breakdown, timeframe="this_month")
        except SystemExit:
            return {"data": []}

import pathlib, json, csv
pathlib.Path("out").mkdir(exist_ok=True)

def write_demo_csv(path, resp, label):
    # парсим total_value.breakdowns[0].results: ["dimension_values": [TIMEFRAME?, SEGMENT], "value": N]
    rows = []
    for x in resp.get("data", []):
        tv = (x.get("total_value") or {})
        b = (tv.get("breakdowns") or [])
        if not b: 
            continue
        for r in (b[0].get("results") or []):
            dv = r.get("dimension_values") or []
            if len(dv) == 2:
                timeframe, segment = dv[0], dv[1]
            elif len(dv) == 1:
                timeframe, segment = "", dv[0]
            else:
                timeframe, segment = "", ""
            rows.append([label, timeframe, segment, r.get("value", 0)])
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["metric","timeframe","segment","value"]); w.writerows(rows)

# Собираем по разрезам
for breakdown in ("age", "gender", "country", "city"):
    # engaged
    write_demo_csv(f"out/demography_engaged_{breakdown}.csv",
                   fetch_demo_safe("engaged_audience_demographics", breakdown),
                   f"engaged_{breakdown}")
    # reached
    write_demo_csv(f"out/demography_reached_{breakdown}.csv",
                   fetch_demo_safe("reached_audience_demographics", breakdown),
                   f"reached_{breakdown}")
    # followers
    write_demo_csv(f"out/demography_followers_{breakdown}.csv",
                   fetch_follower_demo(breakdown),
                   f"followers_{breakdown}")



# --- 8) Производные коэффициенты ---
pv_per_1000_views = round((profile_views_total / views_total * 1000), 1) if views_total else 0.0
ctr_profile = round((website_clicks / profile_views_total * 100), 2) if profile_views_total else 0.0

# доли подписчики / не-подписчики по views
f_views = (follow_views.get("FOLLOWER",0)); nf_views = (follow_views.get("NON_FOLLOWER",0))
share_f = round(100 * f_views / (f_views + nf_views), 1) if (f_views + nf_views) else 0.0
share_nf = round(100 - share_f, 1) if (f_views + nf_views) else 0.0

# ==== HTML ОТЧЁТ (подготовка данных для секций) ==============================

# доли подписчик/неподписчик по VIEWS
fv  = follow_views.get("FOLLOWER", 0)
nfv = follow_views.get("NON_FOLLOWER", 0)
views_sum = fv + nfv
views_share_f  = round(100 * fv / views_sum, 1) if views_sum else 0.0
views_share_nf = round(100 - views_share_f, 1)   if views_sum else 0.0

# доли подписчик/неподписчик по REACH
fr  = follow_reach.get("FOLLOWER", 0)
nfr = follow_reach.get("NON_FOLLOWER", 0)
reach_sum = fr + nfr
reach_share_f  = round(100 * fr / reach_sum, 1) if reach_sum else 0.0
reach_share_nf = round(100 - reach_share_f, 1)   if reach_sum else 0.0

# вовлечённость
likes    = gtot("likes")
comments = gtot("comments")
shares_m = gtot("shares")
saves    = gtot("saves")
accounts_engaged   = gtot("accounts_engaged")
total_interactions = gtot("total_interactions")

# ==== HTML ===================================================================
html = f"""<!doctype html><meta charset="utf-8">
<title>Instagram: отчёт (последние 28 дней)</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{{font:15px/1.6 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:40px auto;max-width:980px;color:#111}}
h1,h2{{line-height:1.25;margin:0 0 10px}}
.card{{border:1px solid #e5e7eb;border-radius:14px;padding:16px;margin:14px 0}}
.kpi{{display:grid;grid-template-columns:repeat(2,minmax(260px,1fr));gap:12px}}
.kpi .box{{border:1px solid #e5e7eb;border-radius:12px;padding:12px}}
table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #eee;padding:8px;text-align:left}}
th{{background:#fafafa}}
.mono{{font-variant-numeric:tabular-nums}}
.small{{color:#555;font-size:13px}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
@media(max-width:760px){{.kpi,.grid2{{grid-template-columns:1fr}}}}
</style>
<body>
<h1>Instagram — автоотчёт</h1>
<p class="small">Окно: <b>{ru_date(SINCE)}</b> — <b>{ru_date(UNTIL)}</b></p>

<div class="kpi">
  <div class="box"><b>Просмотры (views)</b><div class="mono">{views_total:,}</div></div>
  <div class="box"><b>Охват (reach)</b><div class="mono">{reach_total:,}</div></div>
  <div class="box"><b>Посещения профиля</b><div class="mono">{profile_views_total:,}</div></div>
  <div class="box">
    <b>CTR профиля (сайт/ссылки → из визитов)</b>
    <div class="mono">{ctr_profile}%</div>
    <div class="small">website_clicks={website_clicks:,}, profile_links_taps={profile_links_taps:,}</div>
  </div>
  <div class="box"><b>Переходов в профиль на 1000 views</b><div class="mono">{pv_per_1000_views}</div></div>
</div>

<div class="card">
  <h2>Подписчики vs Не-подписчики (по views)</h2>
  <p class="mono">подписчики: {fv:,} ({views_share_f}%) · не-подписчики: {nfv:,} ({views_share_nf}%)</p>
</div>

<div class="card">
  <h2>Подписчики vs Не-подписчики (по reach)</h2>
  <p class="mono">подписчики: {fr:,} ({reach_share_f}%) · не-подписчики: {nfr:,} ({reach_share_nf}%)</p>
</div>

<div class="card">
  <h2>По типам контента (поверхности)</h2>
  <table>
    <tr><th>Surface</th><th>Views</th><th>Reach</th><th>Views/Reach</th></tr>
    {"".join(f"<tr><td>{s}</td><td class='mono'>{surf_cv.get(s,0):,}</td><td class='mono'>{surf_reach.get(s,0):,}</td><td class='mono'>{(surf_cv.get(s,0)/(surf_reach.get(s,0) or 1)):.2f}</td></tr>" for s in ["REELS","STORY","FEED","AD"])}
  </table>
  <p class="small">В «Views» используются <b>content_views</b> (если доступны), иначе — fallback к <b>views</b>.</p>
</div>

<div class="card">
  <h2>Дневные ряды</h2>
  <table>
    <tr><th>Дата</th><th>Reach</th><th>Views (content_views)</th><th>Views/Reach</th></tr>
    {"".join(f"<tr><td>{ru_date(d)}</td><td class='mono'>{int(r):,}</td><td class='mono'>{int(cv):,}</td><td class='mono'>{vpr:.3f}</td></tr>" for d,r,cv,vpr in daily_rows)}
  </table>
  <p class="small">CSV: <code>out/daily.csv</code></p>
</div>

<div class="card">
  <h2>Вовлечённость</h2>
  <ul>
    <li><b>Accounts engaged</b>: <span class="mono">{accounts_engaged:,}</span></li>
    <li><b>Interactions</b>: <span class="mono">{total_interactions:,}</span> (likes {likes:,}, comments {comments:,}, shares {shares_m:,}, saves {saves:,})</li>
  </ul>
  <p class="small">CSV: <code>out/engagement_totals.csv</code></p>
</div>

<div class="card">
  <h2>Файлы данных (CSV)</h2>
  <ul>
    <li><code>out/totals.csv</code> — views, profile_views, clicks, <b>reach</b></li>
    <li><code>out/daily.csv</code> — дневные ряды: reach, content_views, views_per_reach</li>
    <li><code>out/followers.csv</code> — FOLLOWER / NON_FOLLOWER (views и reach)</li>
    <li><code>out/surfaces.csv</code> — FEED / STORY / REELS / AD</li>
    <li><code>out/engagement_totals.csv</code> — likes/comments/shares/saves и др.</li>
  </ul>
  <p class="small">Если на сайте нет папки <code>out/</code>, убери строку <code>rm -rf out</code> из шага «Build report» в workflow — и CSV начнут публиковаться.</p>
</div>

<p class="small">Источник «From Home/Profile/Explore» в API недоступен — смотри в приложении IG. Отчёт генерируется автоматически по расписанию.</p>
</body>
"""
pathlib.Path("report.html").write_text(html, encoding="utf-8")



print("OK; window:", SINCE, "→", UNTIL)
