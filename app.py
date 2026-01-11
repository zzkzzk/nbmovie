from flask import Flask, render_template, request, jsonify, render_template_string, Response, make_response
import requests
import concurrent.futures
import sqlite3
import datetime
import os
import threading
import pytz
import urllib3
import time
import io
import csv
from collections import Counter
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

# --- 新增：引入限流库 ---
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# 禁用安全请求警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# ================= 新增：防御机制配置 (放在 app 初始化的正下方) =================

# 1. 定义获取真实IP的函数 (适配 Render 代理环境，防止误杀)
def get_real_ip():
    if request.headers.getlist("X-Forwarded-For"):
        return request.headers.getlist("X-Forwarded-For")[0]
    return get_remote_address()

# 2. 初始化限流器
limiter = Limiter(
    key_func=get_real_ip,
    app=app,
    default_limits=["2000 per day", "500 per hour"], # 默认全站每人每天上限，防极端爬虫
    storage_uri="memory://"
)

# ================= 配置区域 =================

HOME_NOTICE = {
    "enabled": True,
    "version": "v5.0",
    "title": "⚡ 系统升级完毕",
    "content": """
    <p>1. <b>后台升级</b>：新增可视化数据看板，流量一目了然。</p>
    <p>2. <b>智能日志</b>：优化了播放记录，精确统计热门片单。</p>
    <p>3. <b>性能维持</b>：在增强功能的同时，保持了极速响应内核。</p>
    """
}

# ================= 0. 底层网络与缓存 =================
GLOBAL_SESSION = requests.Session()
retry = Retry(connect=2, read=2, backoff_factor=0.1, status_forcelist=[500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
GLOBAL_SESSION.mount('http://', adapter)
GLOBAL_SESSION.mount('https://', adapter)
GLOBAL_SESSION.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
})


class SimpleCache:
    def __init__(self, ttl_seconds=600):
        self.cache = {}
        self.ttl = ttl_seconds
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            if key in self.cache:
                timestamp, data = self.cache[key]
                if time.time() - timestamp < self.ttl:
                    return data
                else:
                    del self.cache[key]
        return None

    def set(self, key, data):
        with self.lock:
            if len(self.cache) > 1000: self.cache.clear()
            self.cache[key] = (time.time(), data)


search_cache = SimpleCache(ttl_seconds=1800)

# ================= 1. 基础配置与数据库 =================
DB_FILE = 'site_stats.db'


def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS visits 
                     (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                      ip TEXT, location TEXT, time TIMESTAMP, endpoint TEXT, user_agent TEXT)''')
        try:
            c.execute("SELECT location FROM visits LIMIT 1")
        except:
            c.execute("ALTER TABLE visits ADD COLUMN location TEXT")
        conn.commit()


init_db()


def log_traffic(endpoint, extra_info=None):
    try:
        if request.headers.getlist("X-Forwarded-For"):
            ip = request.headers.getlist("X-Forwarded-For")[0]
        else:
            ip = request.remote_addr
        ua = request.headers.get('User-Agent', '')

        if 'UptimeRobot' in ua:
            threading.Thread(target=simple_logger, args=(ip, endpoint, ua)).start()
            return

        full_action = endpoint
        if extra_info:
            full_action = f"{endpoint} | {extra_info}"

        threading.Thread(target=background_logger, args=(ip, full_action, ua)).start()
    except:
        pass


def simple_logger(ip, endpoint, ua):
    try:
        now = datetime.datetime.now(pytz.timezone('Asia/Shanghai')).strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(DB_FILE) as conn:
            conn.cursor().execute(
                "INSERT INTO visits (ip, location, time, endpoint, user_agent) VALUES (?, ?, ?, ?, ?)",
                (ip, "UptimeRobot", now, endpoint, ua))
            conn.commit()
    except:
        pass


def background_logger(ip, endpoint, user_agent):
    location = "未知"
    try:
        if not ip.startswith('127.') and not ip.startswith('192.168.'):
            resp = GLOBAL_SESSION.get(f"http://ip-api.com/json/{ip}?lang=zh-CN", timeout=2, verify=False)
            if resp.status_code == 200 and resp.json()['status'] == 'success':
                d = resp.json()
                location = f"{d['country']} {d['regionName']} {d['city']}"
    except:
        pass

    try:
        now = datetime.datetime.now(pytz.timezone('Asia/Shanghai')).strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(DB_FILE) as conn:
            conn.cursor().execute(
                "INSERT INTO visits (ip, location, time, endpoint, user_agent) VALUES (?, ?, ?, ?, ?)",
                (ip, location, now, endpoint, user_agent))
            conn.commit()
    except:
        pass


# ================= 2. 视频源逻辑 =================
DIRECT_SOURCES = [
    {"name": "量子资源", "api": "https://cj.lziapi.com/api.php/provide/vod/", "speed": "🚀 极速", "primary": True},
    {"name": "非凡资源", "api": "http://cj.ffzyapi.com/api.php/provide/vod/", "speed": "🐢 稳定", "primary": False},
    {"name": "暴风资源", "api": "https://bfzyapi.com/api.php/provide/vod", "speed": "⚡ 高速", "primary": False}
]


def normalize_type(raw_type):
    if not raw_type: return "其他"
    if any(k in raw_type for k in ['电影', '片', '剧场']): return "电影"
    if any(k in raw_type for k in ['剧', '连续', '集']): return "电视剧"
    if any(k in raw_type for k in ['动漫', '动画']): return "动漫"
    if any(k in raw_type for k in ['综艺', '秀']): return "综艺"
    if any(k in raw_type for k in ['短剧']): return "短剧"
    return "其他"


def fetch_single_source_search(source, keyword):
    try:
        resp = GLOBAL_SESSION.get(source['api'], params={"ac": "list", "wd": keyword}, timeout=5)
        data = resp.json()
        video_list = data.get("list") or data.get("data")
        results = []
        if video_list:
            for i in video_list:
                name = i.get("vod_name", "未知")
                if "福利" in name or "伦理" in name: continue
                results.append({
                    "id": i["vod_id"], "title": name, "img": i.get("vod_pic"),
                    "note": i.get("vod_remarks", ""), "api": source['api'],
                    "source_name": source['name'], "speed": source.get('speed', '未知'),
                    "type": normalize_type(i.get("type_name", "")), "raw_type": i.get("type_name", "")
                })
        return results
    except:
        return []


def search_global(keyword, mode='fast'):
    cache_key = f"{keyword}_{mode}"
    cached_data = search_cache.get(cache_key)
    if cached_data: return cached_data

    all_movies = []
    if mode == 'fast':
        target_sources = [DIRECT_SOURCES[0]]
    else:
        target_sources = DIRECT_SOURCES

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(target_sources)) as executor:
        futures = [executor.submit(fetch_single_source_search, src, keyword) for src in target_sources]
        for future in concurrent.futures.as_completed(futures):
            res = future.result()
            if res: all_movies.extend(res)

    all_movies.sort(key=lambda x: (x['title'] != keyword, len(x['title']), x['title']))
    search_cache.set(cache_key, all_movies)
    return all_movies


def get_video_details(api_url, vod_id):
    try:
        resp = GLOBAL_SESSION.get(api_url, params={"ac": "detail", "ids": vod_id}, timeout=6)
        data = resp.json()
        video_list = data.get("list") or data.get("data")
        if video_list:
            info = video_list[0]
            play_url_str = info.get("vod_play_url", "")
            target_play_url = ""
            chunks = play_url_str.split("$$$")
            found_m3u8 = False
            for chunk in chunks:
                if ".m3u8" in chunk:
                    target_play_url = chunk;
                    found_m3u8 = True;
                    break
            if not found_m3u8 and chunks: target_play_url = chunks[0]
            episodes = []
            if target_play_url:
                for idx, item in enumerate(target_play_url.split("#")):
                    parts = item.split("$")
                    if len(parts) >= 2:
                        name = parts[-2]; url = parts[-1]
                    else:
                        name = f"第{idx + 1}集"; url = parts[0]
                    if url.endswith(".m3u8") or url.endswith(".mp4"):
                        episodes.append({"index": idx, "name": name, "url": url})
            return {
                "id": info["vod_id"], "title": info["vod_name"],
                "desc": info.get("vod_content", "").replace('<p>', '').replace('</p>', ''),
                "pic": info.get("vod_pic"), "episodes": episodes, "api": api_url,
                "type_name": normalize_type(info.get("type_name", ""))
            }
    except:
        pass
    return None


# ================= 3. 数据分析逻辑 =================
def get_dashboard_stats():
    stats = {
        'today_pv': 0, 'today_uv': 0, 'total_logs': 0,
        'top_search': [], 'top_play': [], 'recent_logs': [],
        'chart_labels': [], 'chart_data': []
    }
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()

        # 1. 基础总数
        c.execute("SELECT COUNT(*) FROM visits")
        stats['total_logs'] = c.fetchone()[0]

        # 2. 今日数据
        today_str = datetime.datetime.now(pytz.timezone('Asia/Shanghai')).strftime("%Y-%m-%d")
        c.execute("SELECT COUNT(*) FROM visits WHERE time LIKE ?", (f"{today_str}%",))
        stats['today_pv'] = c.fetchone()[0]

        c.execute("SELECT COUNT(DISTINCT ip) FROM visits WHERE time LIKE ?", (f"{today_str}%",))
        stats['today_uv'] = c.fetchone()[0]

        # 3. 最近20条日志
        c.execute("SELECT time, ip, location, endpoint FROM visits ORDER BY time DESC LIMIT 20")
        stats['recent_logs'] = [{'time': r[0].split(' ')[1], 'ip': r[1], 'loc': r[2], 'act': r[3]} for r in
                                c.fetchall()]

        # 4. 图表数据 (过去7天PV)
        dates = []
        counts = []
        for i in range(6, -1, -1):
            d = (datetime.datetime.now(pytz.timezone('Asia/Shanghai')) - datetime.timedelta(days=i)).strftime(
                "%Y-%m-%d")
            c.execute("SELECT COUNT(*) FROM visits WHERE time LIKE ?", (f"{d}%",))
            cnt = c.fetchone()[0]
            dates.append(d[5:])  # 只取 MM-DD
            counts.append(cnt)
        stats['chart_labels'] = dates
        stats['chart_data'] = counts

        # 5. 热门搜索 & 播放 (取最近1000条分析，避免太慢)
        c.execute("SELECT endpoint FROM visits ORDER BY time DESC LIMIT 1000")
        rows = c.fetchall()
        search_words = []
        play_names = []
        for r in rows:
            act = r[0]
            if '搜索:' in act:
                search_words.append(act.split('搜索:')[1].strip())
            if '播放:' in act:
                # 尝试提取片名
                if '(' in act and ')' in act:
                    play_names.append(act.split('播放:')[1].split('(')[0].strip())
                else:
                    play_names.append(act)  # 旧数据格式

        stats['top_search'] = Counter(search_words).most_common(8)
        stats['top_play'] = Counter(play_names).most_common(8)

        conn.close()
    except Exception as e:
        print(f"Stats Error: {e}")
    return stats


# ================= 4. 路由 =================
@app.route('/')
def home():
    log_traffic('首页访问')
    try:
        with open('templates/index.html', 'r', encoding='utf-8') as f:
            html_content = f.read()
        if HOME_NOTICE['enabled']:
            pass
        return render_template_string(html_content)
    except:
        return render_template('index.html')


@app.route('/api/search_json')
@limiter.limit("8 per minute")  # 🔒 修改点：API限流，每分钟每IP最多10次
def search_json_handler():
    keyword = request.args.get('keyword')
    if not keyword: return jsonify([])
    movies = search_global(keyword, mode='all')
    return jsonify(movies)


@app.route('/api/cover_rescue')
def cover_rescue_handler():
    return jsonify({'url': ''})


@app.route('/search', methods=['POST', 'GET'])
@limiter.limit("8 per minute")   # 🔒 修改点：搜索限流，每分钟每IP最多8次
def search_handler():
    keyword = request.form.get('keyword') or request.args.get('keyword')
    if not keyword: return render_template('index.html')
    log_traffic(f'搜索: {keyword}')
    movies = search_global(keyword, mode='fast')
    return render_template('results.html', movies=movies, keyword=keyword)


@app.route('/play')
def play_handler():
    vod_id = request.args.get('id')
    ep_index = request.args.get('ep_index', 0, type=int)
    api = request.args.get('api')

    # 关键修改：获取视频信息后再记录日志，这样日志里就有片名了
    video_data = get_video_details(api, vod_id)

    if video_data:
        # 记录片名，方便后台统计
        log_traffic(f'播放: {video_data["title"]} (ID-{vod_id})')

        if video_data.get('episodes'):
            if ep_index >= len(video_data['episodes']): ep_index = 0
            return render_template('player.html', video=video_data, current_ep=video_data['episodes'][ep_index],
                                   current_index=ep_index, current_api=api)

    log_traffic(f'播放失败: ID-{vod_id}')
    return "<h3>⚠️ 视频加载失败，请返回重试。</h3>"


@app.route('/api/heartbeat', methods=['POST', 'GET'])
def heartbeat():
    return "ok"


# --- 管理后台 (已修复安全漏洞) ---
@app.route('/admin')
def admin_dashboard():
    # --- 🔒 只有携带正确参数才能访问 ---
    password = request.args.get('pass')
    if password != 'Zzk1810342428!':  # 您的专属密码
        return "<h1>🚫 521 love you - 什么都没有，别试了</h1>", 403
    # -----------------------------------

    stats = get_dashboard_stats()
    return render_template('admin.html', stats=stats)


@app.route('/admin/export_csv')
def export_csv_handler():
    # 导出也建议加一道锁，防止别人猜到URL直接导出
    password = request.args.get('pass')
    if password != 'Zzk1810342428!':
         return "<h1>🚫 403 Forbidden</h1>", 403

    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT * FROM visits ORDER BY time DESC LIMIT 5000")
        rows = c.fetchall()
        conn.close()
        si = io.StringIO()
        cw = csv.writer(si)
        cw.writerow(['ID', 'IP', 'Location', 'Time', 'Action', 'User-Agent'])
        cw.writerows(rows)
        output = make_response(si.getvalue())
        output.headers["Content-Disposition"] = "attachment; filename=traffic_data.csv"
        output.headers["Content-type"] = "text/csv"
        return output
    except Exception as e:
        return f"Export Error: {e}"


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"🚀 服务启动: http://0.0.0.0:{port}")
    app.run(host='0.0.0.0', port=port, threaded=True)