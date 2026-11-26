import os
import requests
import concurrent.futures
from urllib.parse import quote, urlsplit
from dotenv import load_dotenv
from flask import Flask, render_template, request, Response, jsonify

load_dotenv()
app = Flask(__name__)

DEBUG = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
SGDB_API_KEY = os.environ.get('SGDB_API_KEY', '')

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8'
}

@app.route('/')
def index():
    return render_template('index.html')

# --- 搜索功能 A: Steam 官方商店 ---
def search_steam_store(query):
    results = []
    try:
        url = "https://store.steampowered.com/api/storesearch/"
        params = {'term': query, 'l': 'schinese', 'cc': 'CN'}
        r = requests.get(url, params=params, headers=HEADERS, timeout=5)
        data = r.json()
        
        if data.get('total', 0) > 0:
            for item in data['items'][:3]: # 取前3个
                app_id = item['id']
                img_url = f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{app_id}/library_600x900_2x.jpg"
                results.append({
                    'name': f"[Steam] {item['name']}", # 标记来源
                    'url': img_url,
                    'source': 'steam'
                })
    except Exception as e:
        print(f"Steam Search Error: {e}")
    return results

# --- 搜索功能 B: Bangumi ---
def search_bangumi(query):
    results = []
    try:
        url = f"https://api.bgm.tv/search/subject/{quote(query, safe='')}"
        params = {'type': 4, 'responseGroup': 'small', 'max_results': 3}
        r = requests.get(url, params=params, headers=HEADERS, timeout=5)
        data = r.json()

        if data.get('list'):
            for item in data['list'][:3]:
                name = item.get('name_cn') or item.get('name') or 'Unknown'
                img_url = item.get('images', {}).get('large') or item.get('images', {}).get('common', '')
                if img_url:
                    img_url = img_url.replace('http://', 'https://')
                    results.append({
                        'name': f'[Bangumi] {name}',
                        'url': img_url,
                        'source': 'bangumi'
                    })
    except Exception as e:
        print(f"Bangumi Search Error: {e}")
    return results

# --- 搜索功能 C: SteamGridDB ---
def search_sgdb(query):
    results = []
    if not SGDB_API_KEY:
        return results

    try:
        auth_headers = {'Authorization': f'Bearer {SGDB_API_KEY}'}
        
        # 1. 搜 ID
        search_url = f"https://www.steamgriddb.com/api/v2/search/autocomplete/{quote(query, safe='')}"
        r = requests.get(search_url, headers=auth_headers, timeout=5)
        data = r.json()
        
        if data['success'] and len(data['data']) > 0:
            game_id = data['data'][0]['id']
            game_name = data['data'][0]['name']
            
            # 2. 搜图
            grid_url = f"https://www.steamgriddb.com/api/v2/grids/game/{game_id}?dimensions=600x900&styles=alternate,material,white_logo"
            r_grid = requests.get(grid_url, headers=auth_headers, timeout=5)
            grid_data = r_grid.json()
            
            if grid_data['success']:
                for item in grid_data['data'][:4]: # 取前4张美图
                    results.append({
                        'name': f"[Art] {game_name}", # 标记来源
                        'url': item['url'],
                        'source': 'sgdb'
                    })
    except Exception as e:
        print(f"SGDB Search Error: {e}")
    return results

@app.route('/search', methods=['GET'])
def search_game():
    query = request.args.get('q', '').strip()
    if not query or len(query) > 100:
        return jsonify([])

    final_results = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        future_steam = executor.submit(search_steam_store, query)
        future_bangumi = executor.submit(search_bangumi, query)
        future_sgdb = executor.submit(search_sgdb, query)

        final_results.extend(future_steam.result())
        final_results.extend(future_bangumi.result())
        final_results.extend(future_sgdb.result())

    return jsonify(final_results)

ALLOWED_IMAGE_DOMAINS = {
    'shared.akamai.steamstatic.com', 'steamcdn-a.akamaihd.net',
    'cdn.steamgriddb.com', 'lain.bgm.tv',
}
STEAM_IMAGE_DOMAINS = {'shared.akamai.steamstatic.com', 'steamcdn-a.akamaihd.net'}
IMAGE_TYPES = {'image/jpeg', 'image/png', 'image/webp', 'image/gif', 'image/avif'}
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def image_target(url):
    parts = urlsplit(url)
    if (parts.scheme != 'https' or parts.hostname not in ALLOWED_IMAGE_DOMAINS
            or parts.username is not None or parts.password is not None
            or parts.port not in (None, 443)):
        raise ValueError('Image URL is outside the allowed providers')
    return parts


def fetch_image(url):
    with requests.get(url, headers=HEADERS, stream=True, timeout=(3, 5),
                      allow_redirects=False) as upstream:
        if upstream.status_code != 200:
            return 'Image unavailable', 404
        media_type = upstream.headers.get('Content-Type', '').split(';', 1)[0].lower().strip()
        if media_type not in IMAGE_TYPES:
            return 'Unsupported image type', 415
        payload = bytearray()
        for chunk in upstream.iter_content(64 * 1024):
            if len(payload) + len(chunk) > MAX_IMAGE_BYTES:
                return 'Image too large', 413
            payload.extend(chunk)
        return Response(bytes(payload), mimetype=media_type,
                        headers={'X-Content-Type-Options': 'nosniff'})


@app.route('/proxy_image')
def proxy_image():
    url = request.args.get('url', '')
    if not url or len(url) > 2048:
        return 'Invalid image URL', 400
    try:
        target = image_target(url)
    except ValueError:
        return 'Image URL not allowed', 403

    try:
        result = fetch_image(url)
        filename = target.path.rsplit('/', 1)[-1]
        if (isinstance(result, tuple) and result[1] == 404
                and target.hostname in STEAM_IMAGE_DOMAINS
                and filename in ('library_600x900_2x.jpg', 'library_600x900.jpg')):
            fallback = target._replace(path=target.path.rsplit('/', 1)[0] + '/header.jpg').geturl()
            return fetch_image(fallback)
        return result
    except requests.RequestException:
        return 'Image provider unavailable', 502


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=DEBUG, host='127.0.0.1', port=port)