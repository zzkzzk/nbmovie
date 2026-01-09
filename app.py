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

# ================= 0. 智能统计系统 (修复时间格式版) =================
DB_FILE = 'site_stats.db'
# 管理员IP过滤 (填入你的IP，避免污染数据)
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
        # 自动迁移检查
        try:
            c.execute("SELECT location FROM visits LIMIT 1")
        except sqlite3.OperationalError:
            c.execute("ALTER TABLE visits ADD COLUMN location TEXT")
        conn.commit()


init_db()


def get_ip_location(ip):
    """查询IP位置 (后台线程)"""
    if ip == "127.0.0.1" or ip.startswith("192.168"):
        return "本地测试/内网"
    try:
        # 使用 ip-api.com (支持中文)
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
    """后台静默记录"""
    if ip in ADMIN_IP_FILTER: return

    location = get_ip_location(ip)
    # 存入数据库时确保格式干净，不带微秒
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
    """记录入口，启动线程"""
    try:
        if request.headers.getlist("X-Forwarded-For"):
            ip = request.headers.getlist("X-Forwarded-For")[0]
        else:
            ip = request.remote_addr
        user_agent = request.headers.get('User-Agent', '')
        threading.Thread(target=background_logger, args=(ip, endpoint, user_agent)).start()
    except:
        pass


# ================= 1. 源列表逻辑 (保持不变) =================
DIRECT_SOURCES = [
    {"name": "默认资源 (LZI)", "api": "https://cj.lziapi.com/api.php/provide/vod/from/lzm3u8/at/json", "type": 1}]
TVBOX_CONFIGS = [
    {"name": "Dxawi", "url": "https://dxawi.github.io/0/0.json"},
    {"name": "潇洒", "url": "https://raw.githubusercontent.com/PizazzGY/TVBox/main/api.json"}
]
VALID_SOURCES = []


def fetch_tvbox_sites(config):
    try:
        resp = requests.get(config['url'], timeout=3)
        if resp.status_code == 200 and "sites" in resp.json():
            return [{"name": f"[{config['name']}] {s['name']}", "api": s['api'], "type": s['type']} for s in
                    resp.json()['sites'] if s.get("type") in [0, 1]]
    except:
        pass
    return []


print("🚀 初始化源列表...")
VALID_SOURCES = list(DIRECT_SOURCES)
with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
    futures = [executor.submit(fetch_tvbox_sites, cfg) for cfg in TVBOX_CONFIGS]
    for future in concurrent.futures.as_completed(futures):
        VALID_SOURCES.extend(future.result())

seen_apis = set()
final_sources = []
for s in VALID_SOURCES:
    if s['api'] not in seen_apis:
        final_sources.append(s)
        seen_apis.add(s['api'])
VALID_SOURCES = final_sources


# ================= 2. 核心业务逻辑 (保持不变) =================
def search_api(api_url, keyword):
    try:
        resp = requests.get(api_url, params={"ac": "detail", "wd": keyword}, timeout=5)
        data = resp.json()
        return [
            {"id": i["vod_id"], "title": i["vod_name"], "img": i["vod_pic"], "note": i["vod_remarks"], "api": api_url}
            for i in data.get("list", [])]
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

            episodes = [{"index": i, "name": p.split("$")[-2] if len(p.split("$")) > 1 else f"第{i + 1}集",
                         "url": p.split("$")[-1] if len(p.split("$")) > 1 else p.split("$")[0]} for i, p in
                        enumerate(play_url.split("#"))]
            return {"id": info["vod_id"], "title": info["vod_name"], "desc": info.get("vod_content", ""),
                    "pic": info["vod_pic"], "episodes": episodes, "api": api_url}
    except:
        pass
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
        return render_template('player.html', video=video_data, current_ep=video_data['episodes'][ep_index],
                               current_index=ep_index, current_api=api)
    return "<h3>加载失败</h3>"


# ================= 4. 高级数据后台 (修复 Bug 版) =================

@app.route('/admin/export_csv')
def export_csv():
    """导出为 Excel 可直接打开的 CSV 格式"""
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("SELECT id, time, ip, location, endpoint, user_agent FROM visits ORDER BY time DESC")
            rows = c.fetchall()

        si = io.StringIO()
        si.write('\ufeff')
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
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()

            # --- 1. 行为分类统计 ---
            c.execute("SELECT endpoint FROM visits")
            all_actions = c.fetchall()

            count_home = 0
            count_search = 0
            count_play = 0

            for (action,) in all_actions:
                if '首页' in action:
                    count_home += 1
                elif '搜索' in action:
                    count_search += 1
                elif '播放' in action:
                    count_play += 1

            total_pv = len(all_actions)
            c.execute("SELECT COUNT(DISTINCT ip) FROM visits")
            total_uv = c.fetchone()[0]

            # --- 2. 获取并归纳数据 ---
            c.execute("SELECT ip, location, time, endpoint FROM visits ORDER BY time DESC LIMIT 500")
            raw_logs = c.fetchall()

            grouped_logs = []
            if raw_logs:
                curr_ip = raw_logs[0][0]
                curr_loc = raw_logs[0][1]
                curr_group_actions = []

                for row in raw_logs:
                    ip, loc, time_str, action = row

                    # === 修复核心: 强行去除可能存在的微秒小数 ===
                    # 如果 time_str 是 "2023-01-01 12:00:00.123456"，split('.')[0] 会把它变成 "2023-01-01 12:00:00"
                    clean_time_str = str(time_str).split('.')[0]

                    if ip != curr_ip:
                        if curr_group_actions:
                            # 计算时长时使用清洗后的时间
                            start_t = datetime.datetime.strptime(curr_group_actions[-1]['clean_time'],
                                                                 "%Y-%m-%d %H:%M:%S")
                            end_t = datetime.datetime.strptime(curr_group_actions[0]['clean_time'], "%Y-%m-%d %H:%M:%S")
                            duration = (end_t - start_t).seconds
                            if duration < 60:
                                duration_str = f"{duration}秒"
                            else:
                                duration_str = f"{duration // 60}分{duration % 60}秒"

                            grouped_logs.append({
                                'ip': curr_ip,
                                'location': curr_loc,
                                'latest_time': curr_group_actions[0]['time_only'],
                                'duration': duration_str,
                                'actions': curr_group_actions
                            })

                        curr_ip = ip
                        curr_loc = loc
                        curr_group_actions = []

                    # 提取 HH:MM:SS
                    try:
                        time_only = clean_time_str.split(' ')[1]
                    except:
                        time_only = clean_time_str  # 防止格式异常报错

                    curr_group_actions.append({
                        'full_time': time_str,
                        'clean_time': clean_time_str,  # 存一个清洗版用于计算
                        'time_only': time_only,
                        'action': action
                    })

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

            # --- 3. 生成 HTML ---
            html = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>数据指挥中心 Pro</title>
                <meta name="viewport" content="width=device-width, initial-scale=1">
                <style>
                    body {{ font-family: -apple-system, sans-serif; background: #f0f2f5; padding: 20px; color: #333; max-width: 1200px; margin: 0 auto; }}
                    .header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }}
                    .btn-dl {{ background: #28a745; color: white; text-decoration: none; padding: 10px 20px; border-radius: 6px; font-weight: bold; box-shadow: 0 2px 5px rgba(0,0,0,0.1); transition: 0.3s; }}
                    .btn-dl:hover {{ background: #218838; transform: translateY(-2px); }}
                    .stats-row {{ display: flex; gap: 15px; margin-bottom: 25px; flex-wrap: wrap; }}
                    .card {{ background: white; padding: 20px; border-radius: 10px; flex: 1; min-width: 140px; text-align: center; box-shadow: 0 2px 8px rgba(0,0,0,0.05); }}
                    .card h3 {{ margin: 0 0 10px 0; font-size: 14px; color: #888; font-weight: normal; }}
                    .card .num {{ font-size: 28px; font-weight: bold; color: #333; }}
                    .card.highlight .num {{ color: #007bff; }}
                    .table-header {{ display: flex; padding: 10px 15px; background: #e9ecef; border-radius: 8px 8px 0 0; font-weight: bold; font-size: 13px; color: #555; margin-top: 10px; }}
                    .col-ip {{ width: 35%; }}
                    .col-time {{ width: 25%; text-align: right; }}
                    .col-dur {{ width: 20%; text-align: right; }}
                    .col-act {{ width: 20%; text-align: right; }}
                    details {{ background: white; margin-bottom: 2px; border-bottom: 1px solid #eee; }}
                    details:first-of-type {{ border-top: none; }}
                    details:last-child {{ border-radius: 0 0 8px 8px; border-bottom: none; }}
                    summary {{ padding: 15px; cursor: pointer; list-style: none; display: flex; align-items: center; transition: background 0.1s; }}
                    summary:hover {{ background: #f8f9fa; }}
                    summary::-webkit-details-marker {{ display: none; }}
                    .sum-content {{ display: flex; width: 100%; align-items: center; }}
                    .ip-box {{ width: 35%; display: flex; flex-direction: column; }}
                    .ip-txt {{ font-weight: bold; font-size: 14px; color: #333; }}
                    .loc-txt {{ font-size: 12px; color: #666; margin-top: 2px; }}
                    .time-txt {{ width: 25%; text-align: right; color: #888; font-size: 13px; }}
                    .dur-txt {{ width: 20%; text-align: right; font-weight: bold; color: #28a745; font-size: 13px; }}
                    .act-count {{ width: 20%; text-align: right; font-size: 12px; background: #eee; padding: 2px 8px; border-radius: 10px; margin-left: auto; }}
                    .detail-box {{ background: #fafafa; padding: 10px 20px; border-top: 1px solid #eee; }}
                    .log-row {{ display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px dashed #eee; font-size: 13px; }}
                    .log-row:last-child {{ border-bottom: none; }}
                    .log-action {{ color: #333; }}
                    .log-time {{ color: #999; font-family: monospace; }}
                </style>
            </head>
            <body>
                <div class="header">
                    <h2>📊 运营数据中心</h2>
                    <a href="/admin/export_csv" class="btn-dl">📥 导出 Excel 报表</a>
                </div>
                <div class="stats-row">
                    <div class="card highlight">
                        <h3>总访问 (PV)</h3>
                        <div class="num">{total_pv}</div>
                    </div>
                    <div class="card highlight">
                        <h3>独立访客 (UV)</h3>
                        <div class="num">{total_uv}</div>
                    </div>
                </div>
                <div class="stats-row">
                    <div class="card">
                        <h3>1. 首页访问</h3>
                        <div class="num" style="color:#6c757d">{count_home}</div>
                    </div>
                    <div class="card">
                        <h3>2. 搜索意向</h3>
                        <div class="num" style="color:#fd7e14">{count_search}</div>
                    </div>
                    <div class="card">
                        <h3>3. 播放转化</h3>
                        <div class="num" style="color:#28a745">{count_play}</div>
                    </div>
                </div>
                <h3>📡 实时访客追踪</h3>
                <div class="table-header">
                    <div class="col-ip">用户 / 位置</div>
                    <div class="col-time">最近活动</div>
                    <div class="col-dur">停留时长</div>
                    <div class="col-act">操作数</div>
                </div>
                <div style="box-shadow: 0 2px 10px rgba(0,0,0,0.05); border-radius: 0 0 8px 8px;">
                    {"".join([f'''
                    <details>
                        <summary>
                            <div class="sum-content">
                                <div class="ip-box">
                                    <span class="ip-txt">{g['ip']}</span>
                                    <span class="loc-txt">{g['location']}</span>
                                </div>
                                <div class="time-txt">{g['latest_time']}</div>
                                <div class="dur-txt">{g['duration']}</div>
                                <div class="act-count">{len(g['actions'])} 次操作 ▼</div>
                            </div>
                        </summary>
                        <div class="detail-box">
                            {''.join([f'<div class="log-row"><span class="log-action">{a["action"]}</span><span class="log-time">{a["time_only"]}</span></div>' for a in g['actions']])}
                        </div>
                    </details>
                    ''' for g in grouped_logs])}
                </div>
                <p style="text-align:center; color:#999; font-size:12px; margin-top:30px;">
                   提示：点击条目可展开查看具体操作流水。建议每天导出 CSV 备份。
                </p>
            </body>
            </html>
            """
            return html
    except Exception as e:
        import traceback
        return f"系统错误详情: {str(e)} <br><pre>{traceback.format_exc()}</pre>"


if __name__ == '__main__':
    print("⚡️ 本地启动: http://127.0.0.1:5000")
    print("📊 数据后台: http://127.0.0.1:5000/admin/dashboard")
    app.run(host='0.0.0.0', port=5000, debug=True)