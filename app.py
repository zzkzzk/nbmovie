from flask import Flask, render_template, request, make_response
import requests
import concurrent.futures
import sqlite3
import datetime
import os
import threading
import csv
import io
import pytz  # 引入时区库

app = Flask(__name__)

# ================= 0. 隐形数据统计系统 =================
DB_FILE = 'site_stats.db'
ADMIN_IP_FILTER = [] 

# 定义伪装请求头，解决搜索和播放被拦截的问题
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS visits 
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                      ip TEXT, 
                      location TEXT,
                      time TIMESTAMP, 
                      endpoint TEXT,
                      user_agent TEXT)''')
        try:
            c.execute("SELECT location FROM visits LIMIT 1")
        except sqlite3.OperationalError:
            c.execute("ALTER TABLE visits ADD COLUMN location TEXT")
        conn.commit()

init_db()

def get_ip_location(ip):
    if ip == "127.0.0.1" or ip.startswith("192.168") or ip.startswith("10."):
        return "内网/本地"
    try:
        url = f"http://ip-api.com/json/{ip}?lang=zh-CN"
        resp = requests.get(url, headers=HEADERS, timeout=3)
        if resp.status_code == 200:
            data = resp.json()
            if data['status'] == 'success':
                return f"{data['country']} {data['regionName']} {data['city']}"
    except: pass
    return "未知位置"

def background_logger(ip, endpoint, user_agent):
    if ip in ADMIN_IP_FILTER: return
    location = get_ip_location(ip)
    
    # 【修复】强制使用北京时间
    tz = pytz.timezone('Asia/Shanghai')
    now = datetime.datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("INSERT INTO visits (ip, location, time, endpoint, user_agent) VALUES (?, ?, ?, ?, ?)",
                      (ip, location, now, endpoint, user_agent))
            conn.commit()
    except Exception as e: print(f"Log Error: {e}")

def log_traffic(endpoint):
    try:
        if request.headers.getlist("X-Forwarded-For"):
            ip = request.headers.getlist("X-Forwarded-For")[0]
        else:
            ip = request.remote_addr
        user_agent = request.headers.get('User-Agent', '')
        threading.Thread(target=background_logger, args=(ip, endpoint, user_agent)).start()
    except: pass

# ================= 1. 视频源逻辑 =================
DIRECT_SOURCES = [{"name": "默认资源 (LZI)", "api": "https://cj.lziapi.com/api.php/provide/vod/from/lzm3u8/at/json", "type": 1}]
TVBOX_CONFIGS = [
    {"name": "Dxawi", "url": "https://dxawi.github.io/0/0.json"},
    {"name": "潇洒",   "url": "https://raw.githubusercontent.com/PizazzGY/TVBox/main/api.json"}
]
VALID_SOURCES = []

def fetch_tvbox_sites(config):
    name_prefix = config['name']
    try:
        # 【修复】加入 headers 防止无法获取源配置
        resp = requests.get(config['url'], headers=HEADERS, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            if "sites" in data:
                return [{"name": f"[{name_prefix}] {s['name']}", "api": s['api'], "type": s['type']} for s in data['sites'] if s.get("type") in [0, 1]]
    except: pass
    return []

print("🚀 云端实例正在初始化源列表...")
VALID_SOURCES = list(DIRECT_SOURCES)
with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
    futures = [executor.submit(fetch_tvbox_sites, cfg) for cfg in TVBOX_CONFIGS]
    for future in concurrent.futures.as_completed(futures):
        VALID_SOURCES.extend(future.result())

seen_apis = set([s['api'] for s in VALID_SOURCES])
final_sources = []
for s in VALID_SOURCES:
    if s['api'] not in seen_apis:
        final_sources.append(s)
        seen_apis.add(s['api'])
VALID_SOURCES = final_sources

# ================= 2. 核心业务逻辑 =================
def search_api(api_url, keyword):
    try:
        # 【修复】加入 headers 和延长超时，防止搜索被拦截或断开
        resp = requests.get(api_url, params={"ac": "list", "wd": keyword}, headers=HEADERS, timeout=10)
        data = resp.json()
        movies = []
        if data.get("list"):
            for i in data["list"]:
                movies.append({"id": i["vod_id"], "title": i["vod_name"], "img": i["vod_pic"], "note": i["vod_remarks"], "api": api_url})
        return movies
    except Exception as e:
        print(f"搜索错误 {api_url}: {e}")
        return []

def get_video_details(api_url, vod_id):
    try:
        # 【修复】加入 headers
        resp = requests.get(api_url, params={"ac": "detail", "ids": vod_id}, headers=HEADERS, timeout=10)
        data = resp.json()
        if data.get("list"):
            info = data["list"][0]
            play_url = info.get("vod_play_url", "").split("$$$")[0]
            for chunk in info.get("vod_play_url", "").split("$$$"):
                if ".m3u8" in chunk: play_url = chunk; break
            
            episodes = []
            for idx, item in enumerate(play_url.split("#")):
                parts = item.split("$")
                url = parts[-1] if len(parts) >= 2 else parts[0]
                name = parts[-2] if len(parts) >= 2 else f"第{idx+1}集"
                episodes.append({"index": idx, "name": name, "url": url})
            return {"id": info["vod_id"], "title": info["vod_name"], "desc": info.get("vod_content", "").replace('<p>','').replace('</p>',''), "pic": info["vod_pic"], "episodes": episodes, "api": api_url}
    except: pass
    return None

# ================= 3. 路由 =================
@app.route('/')
def home():
    log_traffic('首页访问')
    return render_template('index.html', sources=VALID_SOURCES)

@app.route('/search', methods=['POST'])
def search_handler():
    keyword = request.form.get('keyword')
    api = request.form.get('source_api')
    log_traffic(f'搜索: {keyword}')
    if not api and VALID_SOURCES: api = VALID_SOURCES[0]['api']
    return render_template('results.html', movies=search_api(api, keyword), current_api=api)

@app.route('/play')
def play_handler():
    vod_id = request.args.get('id')
    ep_index = request.args.get('ep_index', 0, type=int)
    api = request.args.get('api')
    log_traffic(f'播放: ID-{vod_id} 集-{ep_index}')
    video_data = get_video_details(api, vod_id)
    if video_data:
        if ep_index >= len(video_data['episodes']): ep_index = 0
        return render_template('player.html', video=video_data, current_ep=video_data['episodes'][ep_index], current_index=ep_index, current_api=api)
    return "<h3>加载失败，请重试</h3>"

# ================= 4. 后台管理 =================
@app.route('/admin/export_csv')
def export_csv():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("SELECT id, time, ip, location, endpoint, user_agent FROM visits ORDER BY time DESC")
            rows = c.fetchall()
        si = io.StringIO(); si.write('\ufeff'); writer = csv.writer(si)
        writer.writerow(['ID', '时间', 'IP地址', '地理位置', '用户行为', '设备信息'])
        writer.writerows(rows)
        output = make_response(si.getvalue())
        output.headers["Content-Disposition"] = f"attachment; filename=traffic_data_{datetime.datetime.now().strftime('%Y%m%d')}.csv"
        output.headers["Content-type"] = "text/csv"
        return output
    except Exception as e: return f"Error: {e}"

@app.route('/admin/dashboard')
def admin_stats():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("SELECT endpoint FROM visits"); all_actions = c.fetchall()
            count_play = sum(1 for (a,) in all_actions if '播放' in a)
            total_pv = len(all_actions)
            c.execute("SELECT COUNT(DISTINCT ip) FROM visits"); total_uv = c.fetchone()[0] if all_actions else 0
            c.execute("SELECT ip, location, time, endpoint FROM visits ORDER BY time DESC LIMIT 200")
            raw_logs = c.fetchall()
            
            html = f"""
            <html><head><title>运营后台</title><meta name="viewport" content="width=device-width, initial-scale=1">
            <style>body{{font-family:sans-serif;padding:20px;background:#f5f5f5}} .card{{background:white;padding:15px;margin-bottom:10px;border-radius:8px}}</style>
            </head><body>
            <h2>📊 数据概览 (北京时间)</h2>
            <div style="display:flex;gap:10px">
                <div class="card" style="flex:1"><b>总访问(PV)</b><br><span style="font-size:24px;color:#007bff">{total_pv}</span></div>
                <div class="card" style="flex:1"><b>播放数</b><br><span style="font-size:24px;color:#28a745">{count_play}</span></div>
            </div>
            <a href="/admin/export_csv" style="display:block;background:#28a745;color:white;text-align:center;padding:10px;border-radius:5px;text-decoration:none;margin:20px 0;">📥 导出 Excel 报表</a>
            <div class="card"><h3>最近访客</h3><ul style="padding-left:20px;font-size:13px;color:#555">
                {''.join([f'<li>{r[2]} - {r[1]} - {r[3]}</li>' for r in raw_logs])}
            </ul></div></body></html>
            """
            return html
    except Exception as e: return f"Error: {e}"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)