from flask import Flask, render_template, request, make_response
import requests
import concurrent.futures
import sqlite3
import datetime
import os
import threading
import csv
import io

app = Flask(__name__)

# ================= 0. 隐形数据统计系统 =================
DB_FILE = 'site_stats.db'

# 【管理员设置】
# 如果你想过滤掉自己的访问数据，请填入你的公网IP。
# 如果你想看到所有人的数据（包括你自己），请保持为空列表 []
ADMIN_IP_FILTER = []


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
        # 数据库结构自动检查
        try:
            c.execute("SELECT location FROM visits LIMIT 1")
        except sqlite3.OperationalError:
            c.execute("ALTER TABLE visits ADD COLUMN location TEXT")
        conn.commit()


init_db()


def get_ip_location(ip):
    """
    [后台线程] 查询IP位置
    这步操作需要联网查询，放在后台线程中绝对不会卡顿用户页面
    """
    if ip == "127.0.0.1" or ip.startswith("192.168") or ip.startswith("10."):
        return "内网/本地"
    try:
        # 使用 ip-api.com (支持中文返回)
        url = f"http://ip-api.com/json/{ip}?lang=zh-CN"
        resp = requests.get(url, timeout=3)
        if resp.status_code == 200:
            data = resp.json()
            if data['status'] == 'success':
                return f"{data['country']} {data['regionName']} {data['city']}"
    except:
        pass
    return "未知位置"


def background_logger(ip, endpoint, user_agent):
    """[后台线程] 静默记录逻辑"""
    if ip in ADMIN_IP_FILTER: return

    location = get_ip_location(ip)
    # 存入数据库时确保格式干净，不带微秒，防止后续处理报错
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("INSERT INTO visits (ip, location, time, endpoint, user_agent) VALUES (?, ?, ?, ?, ?)",
                      (ip, location, now, endpoint, user_agent))
            conn.commit()
    except Exception as e:
        print(f"Log Error: {e}")


def log_traffic(endpoint):
    """
    [主程序] 记录入口
    这是唯一在用户请求中执行的代码，它只负责启动一个线程，瞬间完成，用户无感。
    """
    try:
        # Render 位于反向代理之后，必须使用 X-Forwarded-For 获取真实用户IP
        if request.headers.getlist("X-Forwarded-For"):
            ip = request.headers.getlist("X-Forwarded-For")[0]
        else:
            ip = request.remote_addr

        user_agent = request.headers.get('User-Agent', '')

        # 启动后台线程，立刻让主程序继续，不要等待数据库写入
        threading.Thread(target=background_logger, args=(ip, endpoint, user_agent)).start()
    except:
        pass


# ================= 1. 视频源逻辑 (保持原有功能) =================
DIRECT_SOURCES = [
    {"name": "默认资源 (LZI)", "api": "https://cj.lziapi.com/api.php/provide/vod/from/lzm3u8/at/json", "type": 1}
]

TVBOX_CONFIGS = [
    {"name": "Dxawi", "url": "https://dxawi.github.io/0/0.json"},
    {"name": "潇洒", "url": "https://raw.githubusercontent.com/PizazzGY/TVBox/main/api.json"}
]

VALID_SOURCES = []


def fetch_tvbox_sites(config):
    name_prefix = config['name']
    try:
        resp = requests.get(config['url'], timeout=3)
        if resp.status_code == 200:
            data = resp.json()
            if "sites" in data:
                return [{"name": f"[{name_prefix}] {s['name']}", "api": s['api'], "type": s['type']} for s in
                        data['sites'] if s.get("type") in [0, 1]]
    except:
        pass
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


# ================= 2. 核心业务逻辑 (解析) =================
def search_api(api_url, keyword):
    try:
        resp = requests.get(api_url, params={"ac": "detail", "wd": keyword}, timeout=5)
        data = resp.json()
        movies = []
        if data.get("list"):
            for i in data["list"]:
                movies.append({"id": i["vod_id"], "title": i["vod_name"], "img": i["vod_pic"], "note": i["vod_remarks"],
                               "api": api_url})
        return movies
    except:
        return []


def get_video_details(api_url, vod_id):
    try:
        resp = requests.get(api_url, params={"ac": "detail", "ids": vod_id}, timeout=5)
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
                name = parts[-2] if len(parts) >= 2 else f"第{idx + 1}集"
                episodes.append({"index": idx, "name": name, "url": url})

            return {"id": info["vod_id"], "title": info["vod_name"],
                    "desc": info.get("vod_content", "").replace('<p>', '').replace('</p>', ''), "pic": info["vod_pic"],
                    "episodes": episodes, "api": api_url}
    except:
        pass
    return None


# ================= 3. 路由 (已埋入隐形探针) =================
@app.route('/')
def home():
    log_traffic('首页访问')  # 探针：记录首页
    return render_template('index.html', sources=VALID_SOURCES)


@app.route('/search', methods=['POST'])
def search_handler():
    keyword = request.form.get('keyword')
    api = request.form.get('source_api')
    log_traffic(f'搜索: {keyword}')  # 探针：记录搜索词
    if not api and VALID_SOURCES: api = VALID_SOURCES[0]['api']
    return render_template('results.html', movies=search_api(api, keyword), current_api=api)


@app.route('/play')
def play_handler():
    vod_id = request.args.get('id')
    ep_index = request.args.get('ep_index', 0, type=int)
    api = request.args.get('api')
    log_traffic(f'播放: ID-{vod_id} 集-{ep_index}')  # 探针：记录播放行为
    video_data = get_video_details(api, vod_id)
    if video_data:
        if ep_index >= len(video_data['episodes']): ep_index = 0
        return render_template('player.html', video=video_data, current_ep=video_data['episodes'][ep_index],
                               current_index=ep_index, current_api=api)
    return "<h3>加载失败，请重试</h3>"


# ================= 4. 秘密数据后台 (CSV增强版) =================

@app.route('/admin/export_csv')
def export_csv():
    """一键导出Excel可读的CSV文件"""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("SELECT id, time, ip, location, endpoint, user_agent FROM visits ORDER BY time DESC")
            rows = c.fetchall()

        si = io.StringIO()
        si.write('\ufeff')  # 加入BOM头，解决Excel中文乱码问题
        writer = csv.writer(si)
        writer.writerow(['ID', '时间', 'IP地址', '地理位置', '用户行为', '设备信息'])
        writer.writerows(rows)

        output = make_response(si.getvalue())
        output.headers[
            "Content-Disposition"] = f"attachment; filename=traffic_data_{datetime.datetime.now().strftime('%Y%m%d')}.csv"
        output.headers["Content-type"] = "text/csv"
        return output
    except Exception as e:
        return f"导出失败: {e}"


@app.route('/admin/dashboard')
def admin_stats():
    """数据可视化看板"""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()

            # 1. 行为分类
            c.execute("SELECT endpoint FROM visits")
            all_actions = c.fetchall()
            count_home = sum(1 for (a,) in all_actions if '首页' in a)
            count_search = sum(1 for (a,) in all_actions if '搜索' in a)
            count_play = sum(1 for (a,) in all_actions if '播放' in a)
            total_pv = len(all_actions)

            c.execute("SELECT COUNT(DISTINCT ip) FROM visits")
            res = c.fetchone()
            total_uv = res[0] if res else 0

            # 2. 实时流归纳 (含时间清洗逻辑)
            c.execute("SELECT ip, location, time, endpoint FROM visits ORDER BY time DESC LIMIT 500")
            raw_logs = c.fetchall()

            grouped_logs = []
            if raw_logs:
                curr_ip = raw_logs[0][0]
                curr_loc = raw_logs[0][1]
                curr_group_actions = []

                for row in raw_logs:
                    ip, loc, time_str, action = row
                    clean_time_str = str(time_str).split('.')[0]  # 清洗微秒

                    if ip != curr_ip:
                        if curr_group_actions:
                            start_t = datetime.datetime.strptime(curr_group_actions[-1]['clean_time'],
                                                                 "%Y-%m-%d %H:%M:%S")
                            end_t = datetime.datetime.strptime(curr_group_actions[0]['clean_time'], "%Y-%m-%d %H:%M:%S")
                            duration = (end_t - start_t).seconds
                            duration_str = f"{duration}秒" if duration < 60 else f"{duration // 60}分{duration % 60}秒"

                            grouped_logs.append({
                                'ip': curr_ip, 'location': curr_loc,
                                'latest_time': curr_group_actions[0]['time_only'],
                                'duration': duration_str, 'actions': curr_group_actions
                            })
                        curr_ip = ip;
                        curr_loc = loc;
                        curr_group_actions = []

                    try:
                        time_only = clean_time_str.split(' ')[1]
                    except:
                        time_only = clean_time_str
                    curr_group_actions.append(
                        {'full_time': time_str, 'clean_time': clean_time_str, 'time_only': time_only, 'action': action})

                if curr_group_actions:
                    start_t = datetime.datetime.strptime(curr_group_actions[-1]['clean_time'], "%Y-%m-%d %H:%M:%S")
                    end_t = datetime.datetime.strptime(curr_group_actions[0]['clean_time'], "%Y-%m-%d %H:%M:%S")
                    duration = (end_t - start_t).seconds
                    grouped_logs.append({
                        'ip': curr_ip, 'location': curr_loc,
                        'latest_time': curr_group_actions[0]['time_only'],
                        'duration': f"{duration}秒" if duration < 60 else f"{duration // 60}分",
                        'actions': curr_group_actions
                    })

            # 3. HTML 渲染
            html = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>运营数据中心</title>
                <meta name="viewport" content="width=device-width, initial-scale=1">
                <style>
                    body {{ font-family: -apple-system, sans-serif; background: #f0f2f5; padding: 20px; color: #333; max-width: 1200px; margin: 0 auto; }}
                    .header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }}
                    .btn-dl {{ background: #28a745; color: white; text-decoration: none; padding: 10px 20px; border-radius: 6px; font-weight: bold; transition: 0.3s; }}
                    .stats-row {{ display: flex; gap: 15px; margin-bottom: 25px; flex-wrap: wrap; }}
                    .card {{ background: white; padding: 20px; border-radius: 10px; flex: 1; min-width: 140px; text-align: center; box-shadow: 0 2px 8px rgba(0,0,0,0.05); }}
                    .card .num {{ font-size: 28px; font-weight: bold; margin-top: 5px; }}
                    details {{ background: white; border-bottom: 1px solid #eee; }}
                    summary {{ padding: 15px; cursor: pointer; display: flex; align-items: center; list-style: none; }}
                    .sum-content {{ display: flex; width: 100%; align-items: center; justify-content: space-between; }}
                    .detail-box {{ background: #fafafa; padding: 10px 20px; }}
                </style>
            </head>
            <body>
                <div class="header">
                    <h2>📊 运营数据中心</h2>
                    <a href="/admin/export_csv" class="btn-dl">📥 导出 Excel 报表</a>
                </div>
                <div class="stats-row">
                    <div class="card"><h3 style="margin:0;color:#888">总访问 (PV)</h3><div class="num" style="color:#007bff">{total_pv}</div></div>
                    <div class="card"><h3 style="margin:0;color:#888">独立访客 (UV)</h3><div class="num" style="color:#007bff">{total_uv}</div></div>
                </div>
                <div class="stats-row">
                    <div class="card"><h3 style="margin:0;color:#888">1. 首页</h3><div class="num" style="color:#6c757d">{count_home}</div></div>
                    <div class="card"><h3 style="margin:0;color:#888">2. 搜索</h3><div class="num" style="color:#fd7e14">{count_search}</div></div>
                    <div class="card"><h3 style="margin:0;color:#888">3. 播放</h3><div class="num" style="color:#28a745">{count_play}</div></div>
                </div>
                <h3>📡 实时访客追踪</h3>
                <div style="box-shadow: 0 2px 10px rgba(0,0,0,0.05); border-radius: 8px; overflow:hidden">
                    {"".join([f'''
                    <details>
                        <summary>
                            <div class="sum-content">
                                <div style="width:35%"><span style="font-weight:bold">{g['ip']}</span><div style="font-size:12px;color:#666">{g['location']}</div></div>
                                <div style="width:25%;text-align:right;color:#888;font-size:13px">{g['latest_time']}</div>
                                <div style="width:20%;text-align:right;font-weight:bold;color:#28a745;font-size:13px">{g['duration']}</div>
                                <div style="width:20%;text-align:right;font-size:12px;background:#eee;padding:2px 8px;border-radius:10px">{len(g['actions'])} 操作</div>
                            </div>
                        </summary>
                        <div class="detail-box">
                            {''.join([f'<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px dashed #eee;font-size:13px"><span>{a["action"]}</span><span style="color:#999;font-family:monospace">{a["time_only"]}</span></div>' for a in g['actions']])}
                        </div>
                    </details>
                    ''' for g in grouped_logs])}
                </div>
            </body>
            </html>
            """
            return html
    except Exception as e:
        return f"系统错误: {e}"