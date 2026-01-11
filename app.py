from flask import Flask, render_template, request, jsonify, render_template_string, Response
import requests
import concurrent.futures
import sqlite3
import datetime
import os
import threading
import pytz
import urllib3
import time
import random
import socket
import re
import json
import csv
import io
from collections import Counter, defaultdict
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

# 禁用安全请求警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# ================= 配置区域 =================

# 1. 首页公告配置 (修改 'version' 编号可强制让用户再次看到弹窗)
HOME_NOTICE = {
    "enabled": True,
    "version": "v1.0",  # 更改此版本号，用户会再次看到弹窗
    "title": "📢 平台更新说明",
    "content": """
    <p>1. <b>随便看点啥重磅升级</b>：优化了推荐算法，现在全是精选爽剧！</p>
    <p>2. <b>视频源切换优化</b>：首页搜索自动全网搜，详情页可以切换视频源。注意：带有“极速“标志的影片播放更快哦！！</p>
    <p>3. <b>额外声明</b>：本站由本人独立开发，可能面临不稳定等小问题，希望各位多点耐心，遇事不决！！重启解决！！</p>
    <p>4. <b>关于短剧</b>：直接搜索短剧名称可以检索到，但是首页推荐仍旧存在算法优化，一直是韩国短剧，我也蒙了</p>
    <p style="margin-top:10px; color: #00f2ff; font-size:12px;">祝您观影愉快！天天NB！</p>
    """
}

# 2. 洗脑短剧关键词库
BRAINWASH_KEYWORDS = ["总裁", "战神", "夫人", "赘婿", "复仇", "逆袭", "重生", "豪门", "千金", "萌宝", "龙王", "神医"]


# ================= 0. 底层网络与缓存 =================
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
random_pool_cache = SimpleCache(ttl_seconds=3600)


def get_session():
    session = requests.Session()
    retry = Retry(connect=3, backoff_factor=0.5)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    })
    return session


# ================= 1. 基础配置与数据库 =================
DB_FILE = 'site_stats.db'
ADMIN_IP_FILTER = []


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
        full_action = endpoint
        if extra_info:
            full_action = f"{endpoint} | {extra_info}"
        threading.Thread(target=background_logger, args=(ip, full_action, ua)).start()
    except:
        pass


def background_logger(ip, endpoint, user_agent):
    if ip in ADMIN_IP_FILTER: return

    # 1. 尝试获取位置 (独立 try-except，防止API失败影响日志写入)
    location = "未知"
    try:
        if not ip.startswith('127.') and not ip.startswith('192.168.'):
            # 缩短超时时间，避免阻塞
            resp = requests.get(f"http://ip-api.com/json/{ip}?lang=zh-CN", timeout=3, verify=False)
            if resp.status_code == 200 and resp.json()['status'] == 'success':
                d = resp.json()
                location = f"{d['country']} {d['regionName']} {d['city']}"
    except:
        # API 失败不报错，继续执行写入
        pass

    # 2. 写入数据库 (核心逻辑，必须执行)
    try:
        now = datetime.datetime.now(pytz.timezone('Asia/Shanghai')).strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(DB_FILE) as conn:
            conn.cursor().execute(
                "INSERT INTO visits (ip, location, time, endpoint, user_agent) VALUES (?, ?, ?, ?, ?)",
                (ip, location, now, endpoint, user_agent))
            conn.commit()
    except Exception as e:
        print(f"Log Error: {e}")


# ================= 2. 视频源逻辑 =================
DIRECT_SOURCES = [
    {"name": "极速资源", "api": "https://jszyapi.com/api.php/provide/vod/", "type": 1, "speed": "🚀 极速"},
    {"name": "暴风资源", "api": "https://bfzyapi.com/api.php/provide/vod", "type": 1, "speed": "⚡ 高速"},
    {"name": "量子资源", "api": "https://cj.lziapi.com/api.php/provide/vod/", "type": 1, "speed": "⚡ 高速"},
    {"name": "非凡资源", "api": "http://cj.ffzyapi.com/api.php/provide/vod/", "type": 1, "speed": "🐢 稳定"},
    {"name": "360资源", "api": "https://360zy.com/api.php/provide/vod", "type": 1, "speed": "🐢 稳定"},
    {"name": "光速资源", "api": "http://api.guangsuapi.com/api.php/provide/vod/", "type": 1, "speed": "⚡ 高速"}
]

TAG_TO_ID = {"电影": "1", "电视剧": "2", "综艺": "3", "动漫": "4", "短剧": "5", "热门": ""}


def normalize_type(raw_type):
    if not raw_type: return "其他"
    if any(k in raw_type for k in ['电影', '片', '剧场']): return "电影"
    if any(k in raw_type for k in ['剧', '连续', '集']): return "电视剧"
    if any(k in raw_type for k in ['动漫', '动画']): return "动漫"
    if any(k in raw_type for k in ['综艺', '秀']): return "综艺"
    if any(k in raw_type for k in ['短剧']): return "短剧"
    return "其他"


def fetch_single_source_search(source, keyword, session):
    try:
        resp = session.get(source['api'], params={"ac": "list", "wd": keyword}, timeout=5, verify=False)
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


def search_global(keyword):
    cached_data = search_cache.get(keyword)
    if cached_data: return cached_data
    session = get_session()
    all_movies = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(DIRECT_SOURCES)) as executor:
        futures = [executor.submit(fetch_single_source_search, src, keyword, session) for src in DIRECT_SOURCES]
        for future in concurrent.futures.as_completed(futures):
            res = future.result()
            if res: all_movies.extend(res)
    all_movies.sort(key=lambda x: (x['title'] != keyword, len(x['title'])))
    search_cache.set(keyword, all_movies)
    return all_movies


def fetch_category_list(type_id):
    session = get_session()
    all_items = []
    if type_id == '5':  # 短剧优化
        pg_range = range(1, 4)
        target_sources = [s for s in DIRECT_SOURCES if
                          "量子" in s['name'] or "非凡" in s['name'] or "极速" in s['name']]
    else:
        pg_range = [random.randint(1, 3)]
        fast_sources = [s for s in DIRECT_SOURCES if "极速" in s['speed'] or "高速" in s['speed']]
        target_sources = random.sample(fast_sources, min(3, len(fast_sources)))

    for source in target_sources:
        try:
            for pg in pg_range:
                params = {"ac": "list", "pg": pg}
                if type_id: params["t"] = type_id
                resp = session.get(source['api'], params=params, timeout=4, verify=False)
                data = resp.json()
                video_list = data.get("list") or data.get("data")
                if video_list:
                    for i in video_list:
                        name = i.get("vod_name")
                        if "福利" in name or "伦理" in name: continue
                        all_items.append({
                            "id": i["vod_id"], "title": name, "api": source['api'],
                            "img": i.get("vod_pic"), "type": normalize_type(i.get("type_name", ""))
                        })
                if type_id != '5': break
        except:
            pass

    if type_id == '5':
        brainwash_items = [item for item in all_items if
                           any(keyword in item['title'] for keyword in BRAINWASH_KEYWORDS)]
        if brainwash_items:
            if len(brainwash_items) < 5:
                remaining = [x for x in all_items if x not in brainwash_items]
                random.shuffle(remaining)
                brainwash_items.extend(remaining[:10])
            all_items = brainwash_items

    return all_items


def get_video_details(api_url, vod_id):
    try:
        session = get_session()
        resp = session.get(api_url, params={"ac": "detail", "ids": vod_id}, timeout=10, verify=False)
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
                "pic": info.get("vod_pic"), "episodes": episodes, "api": api_url
            }
    except:
        pass
    return None


# ================= 3. 高级数据看板 (含导出功能) =================
ANALYTICS_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Core Analytics | 控制台</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        :root { --bg-color: #0f0c29; --card-bg: rgba(255, 255, 255, 0.03); --glass-border: 1px solid rgba(255, 255, 255, 0.08); --text-main: #ffffff; --text-muted: #8b9bb4; --accent-cyan: #00f2ff; --accent-purple: #bd00ff; --accent-green: #00ff9d; }
        body { margin: 0; padding: 20px; background: linear-gradient(135deg, #0f0c29 0%, #302b63 50%, #24243e 100%); color: var(--text-main); font-family: 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; min-height: 100vh; }
        .container { max-width: 1400px; margin: 0 auto; }
        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 30px; }
        .logo { font-size: 24px; font-weight: 700; background: linear-gradient(to right, var(--accent-cyan), var(--accent-purple)); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        .btn-export { text-decoration: none; padding: 8px 16px; background: rgba(0, 242, 255, 0.1); border: 1px solid var(--accent-cyan); color: var(--accent-cyan); border-radius: 6px; font-size: 13px; transition: all 0.3s; }
        .btn-export:hover { background: var(--accent-cyan); color: #000; box-shadow: 0 0 15px var(--accent-cyan); }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 20px; margin-bottom: 20px; }
        .card { background: var(--card-bg); backdrop-filter: blur(10px); border: var(--glass-border); border-radius: 16px; padding: 20px; box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.37); }
        .card h3 { margin: 0 0 15px 0; font-size: 14px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 1px; }
        .kpi-value { font-size: 36px; font-weight: 700; margin-bottom: 5px; }
        .kpi-sub { font-size: 12px; color: var(--text-muted); display: flex; align-items: center; gap: 5px; }
        .chart-container { position: relative; height: 300px; width: 100%; }
        .wide-card { grid-column: span 2; }
        @media(max-width: 768px) { .wide-card { grid-column: span 1; } }
        .list-item { display: flex; justify-content: space-between; align-items: center; padding: 12px 0; border-bottom: 1px solid rgba(255,255,255,0.05); }
        .list-title { font-size: 14px; color: #fff; display: flex; align-items: center; gap: 10px; }
        .list-val { font-size: 14px; font-weight: bold; color: var(--accent-cyan); }
        .progress-bar { height: 4px; background: rgba(255,255,255,0.1); border-radius: 2px; margin-top: 5px; width: 100%; overflow: hidden; }
        .progress-fill { height: 100%; background: linear-gradient(90deg, var(--accent-cyan), var(--accent-purple)); }
        table { width: 100%; border-collapse: collapse; font-size: 13px; }
        th { text-align: left; color: var(--text-muted); padding: 10px; border-bottom: 1px solid rgba(255,255,255,0.1); }
        td { padding: 10px; color: #eee; border-bottom: 1px solid rgba(255,255,255,0.05); }
        .tag { padding: 2px 8px; border-radius: 4px; font-size: 11px; }
        .tag-play { background: rgba(0, 242, 255, 0.15); color: var(--accent-cyan); }
        .tag-search { background: rgba(189, 0, 255, 0.15); color: var(--accent-purple); }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="logo">ADMIN /// NEXUS</div>
            <div>
                <a href="/admin/export_csv" class="btn-export">📥 导出所有数据 (CSV)</a>
            </div>
        </div>
        <div class="grid">
            <div class="card">
                <h3>今日访客 (UV)</h3>
                <div class="kpi-value">{{ stats.today_uv }}</div>
                <div class="kpi-sub">真实用户</div>
            </div>
            <div class="card">
                <h3>页面浏览 (PV)</h3>
                <div class="kpi-value">{{ stats.today_pv }}</div>
                <div class="kpi-sub">{{ '▲' if stats.pv_trend >= 0 else '▼' }} {{ stats.pv_trend }}% 较昨日</div>
            </div>
            <div class="card">
                <h3>平均观看时长</h3>
                <div class="kpi-value">{{ stats.avg_watch_time }}<span style="font-size:16px">min</span></div>
                <div class="kpi-sub">深度: {{ stats.engagement_rate }}%</div>
            </div>
            <div class="card">
                <h3>搜索热度</h3>
                <div class="kpi-value">{{ stats.search_count_today }}</div>
                <div class="kpi-sub">今日搜索次数</div>
            </div>
        </div>
        <div class="grid">
            <div class="card wide-card">
                <h3>流量趋势 (近7日)</h3>
                <div class="chart-container"><canvas id="trafficChart"></canvas></div>
            </div>
            <div class="card">
                <h3>设备分布</h3>
                <div class="chart-container"><canvas id="deviceChart"></canvas></div>
            </div>
        </div>
        <div class="grid">
            <div class="card">
                <h3>🔥 热门视频 TOP 5</h3>
                {% for video in stats.top_videos %}
                <div class="list-item">
                    <div style="width: 100%">
                        <div class="list-title"><span>{{ loop.index }}. {{ video.name }}</span><span style="margin-left:auto; color:var(--accent-cyan)">{{ video.count }}次</span></div>
                        <div class="progress-bar"><div class="progress-fill" style="width: {{ video.percent }}%"></div></div>
                    </div>
                </div>
                {% else %}
                <div style="color: #666; font-size: 12px; padding: 10px;">暂无足够播放数据</div>
                {% endfor %}
            </div>
            <div class="card">
                <h3>🔍 用户搜索热词</h3>
                {% for term in stats.top_search %}
                <div class="list-item"><span class="list-title">{{ term.name }}</span><span class="list-val">{{ term.count }}</span></div>
                {% else %}
                <div style="color: #666; font-size: 12px; padding: 10px;">暂无搜索数据</div>
                {% endfor %}
            </div>
             <div class="card">
                <h3>🌍 访客地理位置</h3>
                {% for geo in stats.geo_dist %}
                <div class="list-item"><span class="list-title">{{ geo.name }}</span><span class="list-val">{{ geo.count }}</span></div>
                {% else %}
                 <div style="color: #666; font-size: 12px; padding: 10px;">位置数据收集失败或为空</div>
                {% endfor %}
            </div>
        </div>
        <div class="card">
            <h3>实时访问日志 (最后 20 条)</h3>
            <table>
                <thead><tr><th>时间</th><th>动作</th><th>位置</th><th>设备</th></tr></thead>
                <tbody>
                    {% for log in stats.recent_logs %}
                    <tr>
                        <td style="color:#8b9bb4">{{ log.time_str }}</td>
                        <td>
                            {% if 'Play' in log.type %}<span class="tag tag-play">PLAY</span>
                            {% elif 'Search' in log.type %}<span class="tag tag-search">SEARCH</span>
                            {% endif %} {{ log.details }}
                        </td>
                        <td>{{ log.location }}</td>
                        <td style="font-size: 11px; color: #666;">{{ log.device }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>
    <script>
        const ctxTraffic = document.getElementById('trafficChart').getContext('2d');
        new Chart(ctxTraffic, {
            type: 'line',
            data: {
                labels: {{ stats.chart_dates | safe }},
                datasets: [{
                    label: '独立访客 (UV)', data: {{ stats.chart_uv | safe }},
                    borderColor: '#00f2ff', backgroundColor: 'rgba(0, 242, 255, 0.1)', tension: 0.4, fill: true
                }, {
                    label: '浏览量 (PV)', data: {{ stats.chart_pv | safe }},
                    borderColor: '#bd00ff', backgroundColor: 'rgba(189, 0, 255, 0.05)', tension: 0.4, fill: true, borderDash: [5, 5]
                }]
            },
            options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { labels: { color: '#fff' } } }, scales: { y: { grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#8b9bb4' } }, x: { grid: { display: false }, ticks: { color: '#8b9bb4' } } } }
        });
        const ctxDevice = document.getElementById('deviceChart').getContext('2d');
        new Chart(ctxDevice, {
            type: 'doughnut',
            data: { labels: ['移动端', '桌面端', '其他'], datasets: [{ data: {{ stats.device_stats | safe }}, backgroundColor: ['#00f2ff', '#bd00ff', '#8b9bb4'], borderWidth: 0 }] },
            options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: 'bottom', labels: { color: '#fff' } } } }
        });
    </script>
</body>
</html>
"""


def analyze_logs():
    stats = {'today_uv': 0, 'today_pv': 0, 'pv_trend': 0, 'avg_watch_time': 0, 'engagement_rate': 0,
             'search_count_today': 0,
             'top_videos': [], 'top_search': [], 'geo_dist': [], 'chart_dates': [], 'chart_uv': [], 'chart_pv': [],
             'device_stats': [0, 0, 0], 'recent_logs': []}
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        limit_date = (datetime.datetime.now() - datetime.timedelta(days=7)).strftime('%Y-%m-%d')
        # 获取更多数据用于分析
        c.execute("SELECT ip, time, endpoint, location, user_agent FROM visits WHERE time > ? ORDER BY time DESC",
                  (limit_date,))
        rows = c.fetchall()
        conn.close()

        clean_logs = []
        ip_activity = defaultdict(int)
        daily_stats = defaultdict(lambda: {'uv': set(), 'pv': 0})

        # 数据容器
        video_c = Counter()
        search_c = Counter()
        geo_c = Counter()
        device_c = Counter()

        for r in rows:
            ip, t_str, action, loc, ua = r
            if any(bot in ua for bot in ['Uptime', 'bot', 'Spider', 'Slurp']): continue
            try:
                t_obj = datetime.datetime.strptime(t_str.split('.')[0], "%Y-%m-%d %H:%M:%S")
            except:
                continue

            clean_logs.append({'ip': ip, 'time': t_obj, 'action': action, 'loc': loc, 'ua': ua})
            d_str = t_obj.strftime('%Y-%m-%d')
            daily_stats[d_str]['pv'] += 1
            daily_stats[d_str]['uv'].add(ip)

            if '[Active]' in action: ip_activity[ip] += 1

            # 统计逻辑优化
            ua_lower = ua.lower()
            if 'android' in ua_lower or 'iphone' in ua_lower:
                device_c['mobile'] += 1
            elif 'windows' in ua_lower or 'macintosh' in ua_lower:
                device_c['desktop'] += 1
            else:
                device_c['other'] += 1

            # 地理位置统计
            if loc and loc != '未知':
                simple_loc = loc.split(' ')[0].replace('中国', '')
                if simple_loc: geo_c[simple_loc] += 1

            # 视频ID提取
            if '播放:' in action:
                try:
                    # 格式: 播放: ID-12345
                    vid = action.split('ID-')[1].strip()
                    video_c[vid] += 1
                except:
                    pass

            # 搜索词提取
            if '搜索:' in action:
                try:
                    # 格式: 搜索: 关键字
                    kw = action.split('搜索:')[1].strip()
                    if kw:
                        search_c[kw] += 1
                        if d_str == datetime.datetime.now().strftime('%Y-%m-%d'):
                            stats['search_count_today'] += 1
                except:
                    pass

        today = datetime.datetime.now().strftime('%Y-%m-%d')
        yst = (datetime.datetime.now() - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
        stats['today_pv'] = daily_stats[today]['pv']
        stats['today_uv'] = len(daily_stats[today]['uv'])
        yst_pv = daily_stats[yst]['pv']
        if yst_pv > 0:
            stats['pv_trend'] = int(((stats['today_pv'] - yst_pv) / yst_pv) * 100)
        else:
            stats['pv_trend'] = 100 if stats['today_pv'] > 0 else 0

        for i in range(6, -1, -1):
            d = (datetime.datetime.now() - datetime.timedelta(days=i)).strftime('%Y-%m-%d')
            stats['chart_dates'].append(d[5:])
            stats['chart_uv'].append(len(daily_stats[d]['uv']))
            stats['chart_pv'].append(daily_stats[d]['pv'])

        total_v = sum(video_c.values()) or 1
        for vid, cnt in video_c.most_common(5): stats['top_videos'].append(
            {'name': f"ID: {vid}", 'count': cnt, 'percent': int((cnt / total_v) * 100)})
        for k, c in search_c.most_common(5): stats['top_search'].append({'name': k, 'count': c})
        for k, c in geo_c.most_common(5): stats['geo_dist'].append({'name': k, 'count': c})
        stats['device_stats'] = [device_c['mobile'], device_c['desktop'], device_c['other']]

        total_minutes = sum(ip_activity.values()) * 0.5
        if stats['today_uv'] > 0: stats['avg_watch_time'] = round(total_minutes / stats['today_uv'], 1)
        deep_users = sum(1 for v in ip_activity.values() if v > 10)
        if stats['today_uv'] > 0: stats['engagement_rate'] = int((deep_users / stats['today_uv']) * 100)

        for log in clean_logs[:20]:
            log_type = "View"
            details = log['action']
            if '播放' in log['action']: log_type = "Play"
            if '搜索' in log['action']: log_type = "Search"

            # 简化显示
            if 'ID-' in details: details = f"ID: {details.split('ID-')[1]}"
            if '搜索:' in details: details = f"Key: {details.split('搜索:')[1]}"

            stats['recent_logs'].append({
                'time_str': log['time'].strftime('%H:%M:%S'),
                'type': log_type,
                'details': details[:30],  # 截断过长日志
                'location': log['loc'],
                'device': 'Mobile' if 'Mobile' in log['ua'] else 'PC'
            })
    except Exception as e:
        print(f"Stats Error: {e}")
    return stats


# ================= 4. 路由逻辑 =================
@app.route('/')
def home():
    log_traffic('首页访问')
    try:
        with open('templates/index.html', 'r', encoding='utf-8') as f:
            html_content = f.read()

        if HOME_NOTICE['enabled']:
            # 增加 LocalStorage 逻辑，只弹一次
            notice_injection = f"""
            <div id="site-notice" style="position: fixed; top: 80px; right: 20px; width: 300px; 
                 background: rgba(20, 20, 35, 0.95); backdrop-filter: blur(10px); 
                 border: 1px solid rgba(0, 242, 255, 0.3); border-radius: 12px; z-index: 9999;
                 box-shadow: 0 10px 30px rgba(0,0,0,0.5); font-family: sans-serif; 
                 transform: translateX(120%); transition: transform 0.5s ease; color: #fff; display:none;">
                <div style="padding: 15px; border-bottom: 1px solid rgba(255,255,255,0.05); display: flex; justify-content: space-between; align-items: center;">
                    <span style="font-weight: bold; color: #00f2ff;">{HOME_NOTICE['title']}</span>
                    <button onclick="closeNotice()" style="background:none; border:none; color: #888; cursor: pointer;">✕</button>
                </div>
                <div style="padding: 15px; font-size: 14px; line-height: 1.6; color: #ccc;">
                    {HOME_NOTICE['content']}
                </div>
            </div>
            <script>
                window.addEventListener('load', function() {{
                    var version = "{HOME_NOTICE['version']}";
                    if (!localStorage.getItem('notice_closed_' + version)) {{
                        var notice = document.getElementById('site-notice');
                        notice.style.display = 'block';
                        setTimeout(function() {{ notice.style.transform = 'translateX(0)'; }}, 500);
                    }}
                }});
                function closeNotice() {{
                    var notice = document.getElementById('site-notice');
                    notice.style.transform = 'translateX(120%)';
                    localStorage.setItem('notice_closed_{HOME_NOTICE['version']}', 'true');
                }}
            </script>
            """
            html_content = html_content.replace('</body>', notice_injection + '</body>')
        return render_template_string(html_content)
    except:
        return render_template('index.html')


@app.route('/api/search_json')
def search_json_handler():
    keyword = request.args.get('keyword')
    if not keyword: return jsonify([])
    movies = search_global(keyword)
    return jsonify(movies)


@app.route('/api/random_pool')
def random_pool_handler():
    tag = request.args.get('tag', '热门')
    type_id = TAG_TO_ID.get(tag, "")
    cache_key = f"pool_{tag}_{random.randint(1, 5)}" if tag == "短剧" else f"pool_{tag}"
    cached = random_pool_cache.get(cache_key)
    if cached: return jsonify(cached)
    pool = fetch_category_list(type_id)
    random.shuffle(pool)
    random_pool_cache.set(cache_key, pool)
    return jsonify(pool)


@app.route('/api/cover_rescue')
def cover_rescue_handler():
    title = request.args.get('title')
    bad_url = request.args.get('bad_url', '')
    if not title: return jsonify({'url': ''})
    movies = search_global(title)
    for m in movies:
        if title in m['title'] and m['img'] and m['img'] != bad_url:
            return jsonify({'url': m['img']})
    return jsonify({'url': ''})


@app.route('/search', methods=['POST', 'GET'])
def search_handler():
    keyword = request.form.get('keyword') or request.args.get('keyword')
    if not keyword: return render_template('index.html')
    log_traffic(f'搜索: {keyword}')
    movies = search_global(keyword)
    return render_template('results.html', movies=movies, keyword=keyword)


@app.route('/play')
def play_handler():
    vod_id = request.args.get('id')
    ep_index = request.args.get('ep_index', 0, type=int)
    api = request.args.get('api')
    # 修复：明确记录ID，方便解析
    log_traffic(f'播放: ID-{vod_id}')
    video_data = get_video_details(api, vod_id)
    if video_data and video_data.get('episodes'):
        if ep_index >= len(video_data['episodes']): ep_index = 0
        return render_template('player.html', video=video_data, current_ep=video_data['episodes'][ep_index],
                               current_index=ep_index, current_api=api)
    return "<h3>⚠️ 视频加载失败，可能是源站已删除该资源。请返回尝试其他源。</h3>"


@app.route('/api/heartbeat', methods=['POST'])
def heartbeat():
    try:
        log_traffic('[Active] Heartbeat')
    except:
        pass
    return "ok"


@app.route('/admin/analytics')
def admin_analytics_pro():
    stats_data = analyze_logs()
    return render_template_string(ANALYTICS_TEMPLATE, stats=stats_data)


# 新增：CSV 导出路由
@app.route('/admin/export_csv')
def export_csv_handler():
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT * FROM visits ORDER BY time DESC")
        rows = c.fetchall()
        conn.close()

        # 使用 StringIO 生成 CSV
        si = io.StringIO()
        cw = csv.writer(si)
        cw.writerow(['ID', 'IP', 'Location', 'Time', 'Action', 'User-Agent'])  # Header
        cw.writerows(rows)

        output = make_response(si.getvalue())
        output.headers["Content-Disposition"] = "attachment; filename=traffic_data.csv"
        output.headers["Content-type"] = "text/csv"
        return output
    except Exception as e:
        return f"Export Error: {e}"


# 辅助函数：生成响应对象
from flask import make_response


@app.route('/admin/dashboard')
def admin_stats_basic():
    return "Use /admin/analytics for Pro version."


def find_available_port(start_port):
    port = start_port
    while port < 65535:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(('127.0.0.1', port)) != 0: return port
            port += 1
    return start_port


if __name__ == '__main__':
    # 优先使用环境变量 PORT（Render 部署必须），本地开发则使用 5000
    port = int(os.environ.get('PORT', 5000))
    print(f"🚀 服务启动: http://0.0.0.0:{port}")
    
    # host必须设为 '0.0.0.0' 才能被外网访问
    app.run(host='0.0.0.0', port=port)