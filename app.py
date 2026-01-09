from flask import Flask, render_template, request
import requests
import concurrent.futures
import os

# 初始化 Flask (云端直接用默认路径即可)
app = Flask(__name__)

# ================= 1. 源列表 =================

# A. 铁饭碗
DIRECT_SOURCES = [
    {
        "name": "默认资源 (LZI)", 
        "api": "https://cj.lziapi.com/api.php/provide/vod/from/lzm3u8/at/json",
        "type": 1
    }
]

# B. 潜力股 (精简为最稳的两个，防止云端启动超时)
TVBOX_CONFIGS = [
    {"name": "Dxawi", "url": "https://dxawi.github.io/0/0.json"},
    {"name": "潇洒",   "url": "https://raw.githubusercontent.com/PizazzGY/TVBox/main/api.json"}
]

VALID_SOURCES = []

# ================= 2. 初始化逻辑 =================

def fetch_tvbox_sites(config):
    name_prefix = config['name']
    url = config['url']
    extracted = []
    try:
        # 云端网络有时候慢，设置3秒超时
        resp = requests.get(url, timeout=3)
        resp.encoding = 'utf-8'
        if resp.status_code != 200: return []
        data = resp.json()
        if "sites" in data:
            for site in data["sites"]:
                if site.get("type") in [0, 1]:
                    new_name = f"[{name_prefix}] {site.get('name')}"
                    extracted.append({"name": new_name, "api": site.get("api"), "type": site.get("type")})
    except:
        pass
    return extracted

# ⚠️ 注意：云服务器启动时会自动运行这个，不需要手动调用
# 我们把它放在全局加载，确保每次有人访问都有数据
print("🚀 云端实例正在初始化源列表...")
VALID_SOURCES = list(DIRECT_SOURCES)
with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
    futures = [executor.submit(fetch_tvbox_sites, cfg) for cfg in TVBOX_CONFIGS]
    for future in concurrent.futures.as_completed(futures):
        VALID_SOURCES.extend(future.result())

# 去重
seen_apis = set([s['api'] for s in VALID_SOURCES])
final_sources = []
for s in VALID_SOURCES:
    if s['api'] in seen_apis:
        final_sources.append(s)
        seen_apis.remove(s['api'])
VALID_SOURCES = final_sources

# ================= 3. 业务逻辑 (保持不变) =================

def search_api(api_url, keyword):
    params = {"ac": "detail", "wd": keyword}
    try:
        resp = requests.get(api_url, params=params, timeout=5)
        data = resp.json()
        movies = []
        if data.get("list"):
            for item in data["list"]:
                movies.append({
                    "id": item["vod_id"],
                    "title": item["vod_name"],
                    "img": item["vod_pic"],
                    "note": item["vod_remarks"],
                    "api": api_url
                })
        return movies
    except:
        return []

def get_video_details(api_url, vod_id):
    params = {"ac": "detail", "ids": vod_id}
    try:
        resp = requests.get(api_url, params=params, timeout=5)
        data = resp.json()
        if data.get("list"):
            info = data["list"][0]
            play_url_str = info.get("vod_play_url", "")
            play_from_str = info.get("vod_play_from", "")
            
            video_playlists = play_url_str.split("$$$")
            source_names = play_from_str.split("$$$")
            
            selected_playlist = video_playlists[0]
            for index, name in enumerate(source_names):
                if "m3u8" in name.lower() or "hls" in name.lower():
                    if index < len(video_playlists):
                        selected_playlist = video_playlists[index]
                    break
            
            episodes = []
            for index, raw_item in enumerate(selected_playlist.split("#")):
                parts = raw_item.split("$")
                url = parts[-1] if len(parts) >= 2 else parts[0]
                name = parts[-2] if len(parts) >= 2 else f"第{index+1}集"
                episodes.append({"index": index, "name": name, "url": url})
                
            return {
                "id": info["vod_id"], "title": info["vod_name"],
                "desc": info.get("vod_content", "").replace('<p>','').replace('</p>',''),
                "pic": info["vod_pic"], "episodes": episodes, "api": api_url
            }
    except:
        pass
    return None

# ================= 4. 路由 =================

@app.route('/')
def home():
    return render_template('index.html', sources=VALID_SOURCES)

@app.route('/search', methods=['POST'])
def search_handler():
    keyword = request.form.get('keyword')
    api = request.form.get('source_api')
    if not api and VALID_SOURCES: api = VALID_SOURCES[0]['api']
    return render_template('results.html', movies=search_api(api, keyword), current_api=api)

@app.route('/play')
def play_handler():
    vod_id = request.args.get('id')
    ep_index = request.args.get('ep_index', 0, type=int)
    api = request.args.get('api')
    video_data = get_video_details(api, vod_id)
    if video_data:
        if ep_index >= len(video_data['episodes']): ep_index = 0
        return render_template('player.html', video=video_data, current_ep=video_data['episodes'][ep_index], current_index=ep_index, current_api=api)
    return "<h3>加载失败，请重试</h3>"

# ❌ 注意：云端代码最后不要写 app.run()，也不要写 if __name__ == ...
# 因为云平台会自己用 WSGI 协议来启动它。