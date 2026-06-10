# ============================================================
# kanko_app.py  観光観光案内アプリ フェーズ7（古地図表示追加版）
# ============================================================
import streamlit as st
import math, json, sqlite3, hashlib, os, time
import requests
import folium
from folium.plugins import MiniMap
from streamlit_folium import st_folium
from datetime import datetime, date
from collections import deque

st.set_page_config(
    page_title="観光スポットナビ | 播磨・関西・香川",
    page_icon="⛩",
    layout="wide",
    initial_sidebar_state="collapsed",
)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kanko_cache.db")

def init_db():
    try:
        con = sqlite3.connect(DB_PATH); cur = con.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS cache (latitude REAL, longitude REAL, mode TEXT, language TEXT, content TEXT, char_count INTEGER, created_at TEXT, PRIMARY KEY (latitude, longitude, mode, language))")
        cur.execute("CREATE TABLE IF NOT EXISTS wiki_cache (spot_id TEXT PRIMARY KEY, title TEXT, extract TEXT, fetched_at TEXT)")
        cur.execute("CREATE TABLE IF NOT EXISTS cloud_usage (usage_date TEXT PRIMARY KEY, count INTEGER DEFAULT 0)")
        con.commit(); con.close(); return True
    except Exception: return False

def cache_get(lat, lon, mode, lang="ja"):
    try:
        con=sqlite3.connect(DB_PATH); cur=con.cursor()
        cur.execute("SELECT content FROM cache WHERE latitude=? AND longitude=? AND mode=? AND language=?",(round(lat,4),round(lon,4),mode,lang))
        row=cur.fetchone(); con.close(); return row[0] if row else None
    except Exception: return None

def cache_set(lat, lon, mode, content, lang="ja"):
    try:
        con=sqlite3.connect(DB_PATH); cur=con.cursor()
        cur.execute("INSERT OR REPLACE INTO cache (latitude,longitude,mode,language,content,char_count,created_at) VALUES (?,?,?,?,?,?,?)",(round(lat,4),round(lon,4),mode,lang,content,len(content),datetime.now().isoformat()))
        con.commit(); con.close()
    except Exception: pass

def wiki_cache_get(spot_id):
    try:
        con=sqlite3.connect(DB_PATH); cur=con.cursor()
        cur.execute("SELECT extract FROM wiki_cache WHERE spot_id=?",(spot_id,))
        row=cur.fetchone(); con.close(); return row[0] if row else None
    except Exception: return None

def wiki_cache_set(spot_id, title, extract):
    try:
        con=sqlite3.connect(DB_PATH); cur=con.cursor()
        cur.execute("INSERT OR REPLACE INTO wiki_cache (spot_id,title,extract,fetched_at) VALUES (?,?,?,?)",(spot_id,title,extract,datetime.now().isoformat()))
        con.commit(); con.close()
    except Exception: pass

def cloud_usage_today():
    try:
        con=sqlite3.connect(DB_PATH); cur=con.cursor(); today=date.today().isoformat()
        cur.execute("SELECT count FROM cloud_usage WHERE usage_date=?",(today,))
        row=cur.fetchone(); con.close(); return row[0] if row else 0
    except Exception: return 0

def cloud_usage_increment():
    try:
        con=sqlite3.connect(DB_PATH); cur=con.cursor(); today=date.today().isoformat()
        cur.execute("INSERT OR IGNORE INTO cloud_usage (usage_date,count) VALUES (?,0)",(today,))
        cur.execute("UPDATE cloud_usage SET count=count+1 WHERE usage_date=?",(today,))
        con.commit(); con.close()
    except Exception: pass

def get_secret(key):
    try:
        if key == "GEMINI_API_KEY":
            k1 = st.secrets.get("GEMINI_API_KEY_1", "").strip()
            k2 = st.secrets.get("GEMINI_API_KEY_2", "").strip()
            if k1 and not k2:
                return k1
            if k1 and k2:
                return k1 + k2
            single = st.secrets.get("GEMINI_API_KEY", "").strip()
            if single: return single
        else:
            val = st.secrets.get(key, "").strip()
            if val: return val
    except Exception: pass
    return os.environ.get(key, "").strip()

@st.cache_data(ttl=86400, show_spinner=False)
def fetch_wikipedia(spot_id, title):
    cached=wiki_cache_get(spot_id)
    if cached: return cached
    try:
        r=requests.get("https://ja.wikipedia.org/w/api.php",
            params={"action":"query","titles":title,"prop":"extracts","exintro":True,"exchars":180,"format":"json","explaintext":True},
            timeout=8,headers={"User-Agent":"kanko-ar-app/4.0"})
        if r.status_code!=200: return ""
        pages=r.json().get("query",{}).get("pages",{})
        extract=next(iter(pages.values())).get("extract","").strip()
        if extract: wiki_cache_set(spot_id,title,extract)
        return extract
    except Exception: return ""

@st.cache_data(ttl=3600, show_spinner=False)
def fetch_overpass_spots(center_lat, center_lon, radius_m=5000):
    try:
        query=f'[out:json][timeout:15];(node["amenity"="place_of_worship"]["religion"="shinto"](around:{radius_m},{center_lat},{center_lon});node["historic"="castle"](around:{radius_m},{center_lat},{center_lon});node["tourism"="viewpoint"](around:{radius_m},{center_lat},{center_lon}););out body 10;'
        r=requests.post("https://overpass-api.de/api/interpreter",data={"data":query},timeout=15,headers={"User-Agent":"kanko-ar-app/4.0"})
        if r.status_code!=200: return []
        spots=[]
        for el in r.json().get("elements",[]):
            tags=el.get("tags",{}); name=tags.get("name:ja") or tags.get("name","")
            if not name: continue
            spots.append({"id":f"osm_{el['id']}","name":name,"name_kana":tags.get("name:ja-Hira",""),
                "category":("shrine" if tags.get("religion")=="shinto" else "castle" if tags.get("historic")=="castle" else "default"),
                "priority":3,"lat":el.get("lat",0),"lon":el.get("lon",0),"altitude":0,
                "prefecture":"兵庫県","city":tags.get("addr:city","播磨エリア"),
                "description":f"{name}（OpenStreetMapデータ）",
                "main_detail":f"{name}\n出典: © OpenStreetMap contributors",
                "trust_score":0.7,"approved":True,"location_limited":False,"location_limited_content":""})
        return spots
    except Exception: return []

def translate_google(text, target_lang="en"):
    """Google翻訳（無料・APIキー不要）"""
    if not text: return text
    cache_key = hashlib.md5(f"{text[:50]}_{target_lang}".encode()).hexdigest()
    cached = cache_get(0.0, 0.0, f"translate_{cache_key}", target_lang)
    if cached: return cached
    try:
        import urllib.parse
        encoded = urllib.parse.quote(text)
        url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=ja&tl={target_lang}&dt=t&q={encoded}"
        r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200: return text
        result = ""
        for item in r.json()[0]:
            if item[0]: result += item[0]
        if result:
            cache_set(0.0, 0.0, f"translate_{cache_key}", result, target_lang)
        return result if result else text
    except Exception: return text

def translate_deepl(text, target_lang="EN"):
    return translate_google(text, target_lang.lower())

def analyze_cloud_gemini(image_bytes):
    api_key = get_secret("GEMINI_API_KEY")
    dummy = {"cloud_type":"判定できませんでした","description":"APIキーを設定するか画像を再アップロードしてください。","weather_hint":"天気の予報は気象庁等でご確認ください。","is_dummy":True}
    if not api_key:
        dummy["description"] = f"APIキー未取得（長さ0）"
        return dummy
    if cloud_usage_today() >= 3:
        dummy["description"] = "本日の雲判定上限（3回）に達しました。明日またお試しください。"
        dummy["is_dummy"] = False
        return dummy
    try:
        import base64; img_b64 = base64.b64encode(image_bytes).decode()
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
        payload = {"contents":[{"parts":[{"text":'この画像の雲を分析してください。観光地で空を見上げている人に向けて、少しロマンティックで詩的な表現で説明してください。JSONのみで回答:{"cloud_type":"雲の種類（正式名称）","description":"雲の特徴・形・色・高さを、空を見上げながら読む人の心に響くような詩的な表現で3〜4行で","formation_reason":"この雲が生まれた理由を、自然の神秘や営みを感じさせるロマンティックな表現で2〜3行で","weather_hint":"この雲から読み取れる空の声・天気の傾向を旅人に語りかけるように2〜3行で","observation_tips":"この雲をより深く味わうための観察のヒントを1〜2行で"}'},{"inline_data":{"mime_type":"image/jpeg","data":img_b64}}]}]}
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code == 400:
            dummy["description"] = f"APIキーエラー(400)。キーの値を確認してください。key先頭:{api_key[:12]}"
            return dummy
        if r.status_code != 200:
            dummy["description"] = f"API通信エラー（HTTP {r.status_code}）"
            return dummy
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        if "{" in text and "}" in text:
            result = json.loads(text[text.index("{"):text.rindex("}")+1])
            result["is_dummy"] = False
            cloud_usage_increment()
            return result
        dummy["cloud_type"] = "解析完了"
        dummy["description"] = text[:120]
        dummy["is_dummy"] = False
        cloud_usage_increment()
        return dummy
    except Exception as e:
        dummy["description"] = f"エラー: {str(e)[:80]}"
        return dummy

def make_share_text(spot, mode_cfg, dist_km):
    cat_map={"shrine":"⛩","mountain":"🏔","castle":"🏯","temple":"🛕","default":"📍"}
    icon=cat_map.get(spot.get("category","default"),"📍")
    mode_name={"main":"観光案内","urban_legend":"都市伝説","powerspot":"パワースポット","healing":"撮影スポット","festival":"行事案内","old_map":"歴史案内","cloud":"雲判定"}.get(mode_cfg["key"],"観光案内")
    # 季節
    month = datetime.now().month
    season = "🌸 春" if month in (3,4,5) else "☀️ 夏" if month in (6,7,8) else "🍂 秋" if month in (9,10,11) else "❄️ 冬"
    # 訪問日時
    visit_dt = datetime.now().strftime("%Y年%m月%d日 %H:%M")
    # 説明文（最初の「。」まで表示）
    full_desc = spot.get("description","")
    if "。" in full_desc:
        desc = full_desc[:full_desc.index("。")+1]
    else:
        desc = full_desc[:50] + ("…" if len(full_desc) > 50 else "")
    return (
        f"{icon} {spot['name']}を訪れました！\n"
        f"📍 {spot['prefecture']} {spot['city']}\n"
        f"🏔 標高{spot['altitude']}m\n"
        f"📖 {desc}\n"
        f"📱 {mode_name}で探索中\n"
        f"📅 {visit_dt}　{season}\n"
        f"\n"
        f"▶ https://kanko-ar-harima.streamlit.app\n"
        f"\n"
        f"#観光スポットナビ #観光アプリ #{spot['name'].replace(' ','')} #{spot['city'].replace(' ','')}"
    )

# ============================================================
# ■ 古地図データ（国立公文書館デジタルアーカイブ）
#   出典: 国立公文書館デジタルアーカイブ
#   パブリックドメイン（著作権保護期間満了）
# ============================================================
OLD_MAP_IMAGES = {
    # 播磨国スポット → 元禄国絵図「播磨国」
    "takamikura_001": {
        "url": "https://www.digital.archives.go.jp/file/1406104.jpg",
        "title": "元禄国絵図 播磨国",
        "era": "元禄15年（1702年）",
        "source": "国土地理院 古地図コレクション",
        "source_url": "https://kochizu.gsi.go.jp/items/8",
        "note": "江戸幕府が作成した播磨国の公式絵図。1里＝6寸の縮尺で村名・石高が記されています。",
    },
    "kasagatayama_002": {
        "url": "https://www.digital.archives.go.jp/file/1406104.jpg",
        "title": "元禄国絵図 播磨国",
        "era": "元禄15年（1702年）",
        "source": "国土地理院 古地図コレクション",
        "source_url": "https://kochizu.gsi.go.jp/items/8",
        "note": "笠形山は播磨の名山として古地図にも記されています。",
    },
    "himeji_castle_003": {
        "url": "https://www.digital.archives.go.jp/file/1406104.jpg",
        "title": "元禄国絵図 播磨国",
        "era": "元禄15年（1702年）",
        "source": "国土地理院 古地図コレクション",
        "source_url": "https://kochizu.gsi.go.jp/items/8",
        "note": "江戸時代の絵図には「姫路」として城下町が白四角で記されています。",
    },
    "kakurinji_004": {
        "url": "https://www.digital.archives.go.jp/file/1406104.jpg",
        "title": "元禄国絵図 播磨国",
        "era": "元禄15年（1702年）",
        "source": "国土地理院 古地図コレクション",
        "source_url": "https://kochizu.gsi.go.jp/items/8",
        "note": "加古川周辺の村々が色分けされた楕円形の枠内に記されています。",
    },
    "ikarugatera_008": {
        "url": "https://www.digital.archives.go.jp/file/1406104.jpg",
        "title": "元禄国絵図 播磨国",
        "era": "元禄15年（1702年）",
        "source": "国土地理院 古地図コレクション",
        "source_url": "https://kochizu.gsi.go.jp/items/8",
        "note": "太子町周辺は揖保郡として記載されています。",
    },
    "kamo_jinja_010": {
        "url": "https://www.digital.archives.go.jp/file/1406104.jpg",
        "title": "元禄国絵図 播磨国",
        "era": "元禄15年（1702年）",
        "source": "国土地理院 古地図コレクション",
        "source_url": "https://kochizu.gsi.go.jp/items/8",
        "note": "龍野（たつの）は龍野藩の城下町として絵図に記されています。",
    },
    "iwatsuhime_011": {
        "url": "https://www.digital.archives.go.jp/file/1406104.jpg",
        "title": "元禄国絵図 播磨国",
        "era": "元禄15年（1702年）",
        "source": "国土地理院 古地図コレクション",
        "source_url": "https://kochizu.gsi.go.jp/items/8",
        "note": "赤穂は赤穂藩の城下町として記載。播磨灘沿岸の地形がわかります。",
    },
    # 奈良
    "nara_todaiji_005": {
        "url": "https://www.digital.archives.go.jp/file/1406104.jpg",
        "title": "天保国絵図 大和国",
        "era": "天保9年（1838年）",
        "source": "国土地理院 古地図コレクション",
        "source_url": "https://kochizu.gsi.go.jp/items/8",
        "note": "奈良は大和国として記載。東大寺・興福寺などの寺院が描かれています。",
    },
    # 京都
    "kyoto_kinkakuji_006": {
        "url": "https://www.digital.archives.go.jp/file/1406104.jpg",
        "title": "天保国絵図 山城国",
        "era": "天保9年（1838年）",
        "source": "国土地理院 古地図コレクション",
        "source_url": "https://kochizu.gsi.go.jp/items/8",
        "note": "京都は山城国として記載。金閣寺周辺の地形が江戸時代の視点でわかります。",
    },
    # 大阪
    "osaka_castle_007": {
        "url": "https://www.digital.archives.go.jp/file/1406104.jpg",
        "title": "天保国絵図 摂津国",
        "era": "天保9年（1838年）",
        "source": "国土地理院 古地図コレクション",
        "source_url": "https://kochizu.gsi.go.jp/items/8",
        "note": "大阪城は摂津国の城下として記載。江戸時代の大坂の街並みがわかります。",
    },
    # 香川
    "takaya_jinja_009": {
        "url": "https://www.digital.archives.go.jp/file/1406104.jpg",
        "title": "天保国絵図 讃岐国",
        "era": "天保9年（1838年）",
        "source": "国土地理院 古地図コレクション",
        "source_url": "https://kochizu.gsi.go.jp/items/8",
        "note": "讃岐国（香川県）の絵図。高屋神社がある稲積山周辺の地形が確認できます。",
    },
}

def show_old_map_image(spot):
    """古地図画像をカード内に表示する"""
    spot_id = spot.get("id", "")
    map_info = OLD_MAP_IMAGES.get(spot_id)

    if not map_info:
        # OSMスポットや未登録スポットは何も表示しない
        return

    st.markdown("---")
    st.markdown("### 🗺 江戸時代の古地図")

    # 古地図リンクボタン（画像直接表示は外部サイト制限のためリンクで対応）
    st.markdown(
        f"""
        <div style="background:rgba(240,220,180,0.40);border-radius:14px;
        padding:16px 20px;margin:8px 0;
        border:1px solid rgba(180,140,80,0.55);">

        <div style="font-size:18px;font-weight:700;color:#3a2000;margin-bottom:10px;">
        📜 {map_info['title']}</div>

        <div style="font-size:14px;color:#5a3a00;line-height:1.8;margin-bottom:8px;">
        ⏳ <b>作成年：</b>{map_info['era']}<br>
        📍 <b>{spot['name']}</b>はこの時代から記録に残る歴史的な地点です。<br>
        📝 {map_info['note']}
        </div>

        <div style="background:rgba(255,240,200,0.60);border-radius:10px;
        padding:10px 14px;font-size:13px;color:#5a3a00;line-height:1.8;">
        🔍 <b>江戸時代の絵図を見るには</b><br>
        国立公文書館デジタルアーカイブ（digital.archives.go.jp）で<br>
        「元禄国絵図」または「天保国絵図」と検索してください。<br>
        播磨・大和・山城・摂津・讃岐国の絵図が無料で閲覧できます。
        </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.caption("📖 出典：国立公文書館デジタルアーカイブ ／ パブリックドメイン（著作権保護期間満了）")

# ============================================================
# ■ スポットデータ
# ============================================================
SPOT_DATA_BUILTIN = [
    {
        "id": "takamikura_001", "name": "高御位神社", "name_kana": "たかみくらじんじゃ",
        "category": "shrine", "priority": 1, "wiki_title": "高御位山",
        "lat": 34.8418, "lon": 134.8682, "altitude": 304,
        "prefecture": "兵庫県", "city": "加古川市",
        "description": "山全体がご神体の山岳信仰の聖地。縄文・弥生時代から山岳崇拝の地。山頂の岩場（磐座）に鎮座。播磨アルプスの最高峰（304m）。",
        "main_detail": (
            "✨ ご祭神：高御産巣日神（たかみむすびのかみ）\n"
            "　　創造と縁結びを司る、天地開闢の神様です。\n\n"
            "🏔 播磨アルプスの最高峰（304m）\n"
            "　　山全体がご神体。眼下に播磨平野と瀬戸内海が広がります。\n\n"
            "🪨 磐座（いわくら）について\n"
            "　　山頂に鎮座する巨大な岩が御神体。縄文時代から崇められてきました。\n\n"
            "👁 見晴らしのポイント\n"
            "　　晴れた日は淡路島・四国山地まで見渡せます。\n\n"
            "🌿 境内の草花\n"
            "　　春はツツジ、秋は紅葉が山肌を彩ります。"
        ),
        "urban_legend": "古来より霊山として知られる高御位山。山頂の磐座には不思議な力が宿るという言い伝えが残る。",
        "urban_legend_detail": "かつて修験者たちがこの山に籠もり、磐座の前で何日も祈りを捧げたという記録が残っています。\n\n満月の夜には山頂から青白い光が見えることがあると、地域の古老たちが語り継いできました。\n\n⚠️ AIエンターテイメント情報です。史実とは異なる場合があります。",
        "powerspot": "山頂の磐座は古代から祈りの場。播磨平野を見渡す大地のエネルギーが集まるとされる聖地。",
        "powerspot_detail": "磐座（いわくら）は古代から神が降り立つ場として信仰されてきました。\n地脈（龍脈）が交差する地点とされ、強い「気」を感じる方も多いと言われます。\n朝日が昇る方角に向かって手を合わせると、特別なパワーを受け取れるとされています。\n\n⚠️ AIエンターテイメント情報です。",
        "festival": "元旦：初日の出参拝（多くの参拝者が訪れる）",
        "festival_detail": "🌅 元旦　初日の出参拝\n　　毎年多くの参拝者が山頂を目指します。\n🌸 春分・秋分　特別祈祷\n☀️ 夏至　山頂観察会\n🍂 10月（体育の日前後）　秋の例大祭\n\n※日程は毎年変動します。最新情報は地元観光協会でご確認ください。",
        "healing_text": "📸 撮影スポット情報",
        "healing_detail": (
            "📍 おすすめ撮影ポイント\n\n"
            "🌅 山頂からの夜明け\n"
            "　　日の出の時間帯に播磨平野を染める朝日は絶景。\n"
            "　　東向きで撮影すると逆光を避けられます。\n\n"
            "🌊 磐座と空\n"
            "　　山頂の巨大な岩と青空のコントラストが映えます。\n"
            "　　広角レンズがおすすめ。\n\n"
            "🌿 ツツジの季節（4〜5月）\n"
            "　　山肌を彩るピンクのツツジと山並みの写真が人気。\n\n"
            "🍂 紅葉シーズン（11月）\n"
            "　　赤・黄・緑のグラデーションが楽しめます。"
        ),
        "old_map_description": "江戸時代の播磨国絵図にも記された神聖な山。古来より地域の人々の信仰を集めてきた。",
        "old_map_detail": "📜 元禄国絵図（1702年）に播磨国として記載。\n🗺 明治以降の地形図と比較すると参拝道の変遷が読み取れます。\n🔍 国立公文書館デジタルアーカイブで「元禄国絵図」「天保国絵図」を検索すると閲覧できます。",
        "cloud_info": "播磨平野を一望できる山頂からの雲の観察に最適な場所です。",
        "cloud_detail": "☁️ 積乱雲：夏の午後に南西から発達。雷雨の前兆。\n🌤 層積雲：朝のうちに漂う雲。晴天のサイン。\n🌫 高層雲：薄いベール状。翌日の雨の予兆。\n\n⚠️ 正確な天気予報は気象庁等でご確認ください。",
        "photo_url": "", "photo_credit": "",
        "trust_score": 1.0, "approved": True,
        "location_limited": True,
        "location_limited_content": "山頂限定：磐座のご神気を感じる特別なパワースポット情報が解放されました。",
    },
    {
        "id": "ikarugatera_008", "name": "斑鳩寺", "name_kana": "いかるがでら",
        "category": "temple", "priority": 2, "wiki_title": "斑鳩寺",
        "lat": 34.837339, "lon": 134.575457, "altitude": 20,
        "prefecture": "兵庫県", "city": "揖保郡太子町",
        "description": "聖徳太子ゆかりの古刹。法隆寺の荘園として栄え、国宝・重要文化財を多数保有する播磨の名刹。",
        "main_detail": "🛕 聖徳太子ゆかりの古刹\n\n📿 国宝・重要文化財\n　　太子が建立したと伝わる由緒ある寺院。多数の文化財を所蔵。\n\n🌸 太子町のシンボル\n　　太子町の名前の由来となった聖徳太子ゆかりの地。\n\n📜 播磨の法隆寺\n　　法隆寺の荘園として奈良時代から栄えた格式ある寺。\n\n🍂 四季の境内\n　　春の桜・秋の紅葉が美しい静かな境内。",
        "urban_legend": "聖徳太子が自ら彫ったとされる仏像が秘仏として今も祀られているという伝説が残る。",
        "urban_legend_detail": "太子が建立した際、自らの姿を刻んだとされる秘仏が奥殿に安置されており、夜になると輝くという言い伝えが地元に残ります。\n\n⚠️ AIエンターテイメント情報です。",
        "powerspot": "聖徳太子の霊力が宿る地。1400年の信仰が積み重なったパワースポットとして多くの参拝者が訪れます。",
        "powerspot_detail": "聖徳太子が開いたとされる境内には、太子の霊気が今も漂うと言われています。\n\n⚠️ AIエンターテイメント情報です。",
        "festival": "春：花まつり（4月8日）・秋の特別公開",
        "festival_detail": "【4月8日】花まつり\n【秋季】文化財特別公開\n【毎月22日】太子忌\n※詳細は斑鳩寺までお問い合わせください。",
        "healing_text": "📸 撮影スポット情報",
        "healing_detail": "📍 おすすめ撮影ポイント\n\n🌸 三重塔と桜（3月下旬〜4月）\n　　古い塔と桜のコントラストが美しい。\n\n🍂 紅葉と伽藍（11月）\n　　静かな境内に紅葉が映える。",
        "old_map_description": "推古天皇元年（593年）聖徳太子が建立したと伝わる。江戸時代には法隆寺の末寺として栄えた。",
        "old_map_detail": "📜 元禄国絵図（1702年）の播磨国・揖保郡に記載。593年創建と伝わる古刹。\n播磨国の重要な寺院として江戸時代の絵図にも記されています。\n🔍 国立公文書館デジタルアーカイブで「元禄国絵図」「天保国絵図」を検索すると閲覧できます。",
        "cloud_info": "揖保川沿いの平野に位置し、周囲の山並みと雲の眺めが美しいスポットです。",
        "cloud_detail": "揖保川流域の平野から見渡す空は開放的です。\n⚠️ 正確な天気予報は気象庁等でご確認ください。",
        "trust_score": 0.9, "photo_url": "", "photo_credit": "",
        "approved": True, "location_limited": True, "location_limited_content": "現地限定🛕 斑鳩寺へようこそ！聖徳太子ゆかりの古刹です。三重塔と仁王門は必見。太子の面影を感じながらゆっくり境内をお歩きください。",
    },
    {
        "id": "takaya_jinja_009", "name": "高屋神社", "name_kana": "たかやじんじゃ",
        "category": "shrine", "priority": 1, "wiki_title": "高屋神社",
        "lat": 34.160608, "lon": 133.654837, "altitude": 404,
        "prefecture": "香川県", "city": "観音寺市",
        "description": "天空の鳥居で有名な神社。標高404mの稲積山山頂に鎮座し、眼下に讃岐平野と瀬戸内海を一望できる絶景スポット。",
        "main_detail": "⛩ 天空の鳥居で大人気の神社\n\n🌤 標高404m・稲積山山頂に鎮座\n　　鳥居の向こうに讃岐平野が広がる絶景。\n\n🌊 瀬戸内海の眺望\n　　晴れた日には瀬戸内海の島々まで見渡せます。\n\n📸 インスタ映えスポット\n　　天空の鳥居は日本有数のフォトスポットとして有名。\n\n🚗 アクセス\n　　麓から徒歩約1時間または車でほぼ山頂まで行けます。",
        "urban_legend": "天空の鳥居の向こうに吸い込まれると別の世界へ行けるという言い伝えが地元の若者の間で語られている。",
        "urban_legend_detail": "山頂の鳥居は雲の上に浮かぶように見えることから、鳥居をくぐると天界と地上の境界を越えると言われています。\n\n⚠️ AIエンターテイメント情報です。",
        "powerspot": "天空に最も近い神社として、空と大地のエネルギーが交差する特別な場所。願いが天に届きやすいとされます。",
        "powerspot_detail": "標高404mの山頂に位置し、空に最も近い場所で参拝できます。\n\n⚠️ AIエンターテイメント情報です。",
        "festival": "春・秋の例大祭・初詣（年始は多くの参拝者が訪れる）",
        "festival_detail": "【1月1日〜3日】初詣\n【春・秋】例大祭\n※詳細は観音寺市観光協会でご確認ください。",
        "healing_text": "📸 撮影スポット情報",
        "healing_detail": "📍 おすすめ撮影ポイント\n\n☁️ 天空の鳥居（雲海の日）\n　　雲が出た日の朝に鳥居と雲海を撮影！絶景。\n\n🌅 夕日と鳥居\n　　夕暮れ時に鳥居越しに沈む夕日は格別。\n\n🌊 山頂からの讃岐平野\n　　360度の絶景パノラマは必撮。",
        "old_map_description": "創建年代不詳の古社。稲積山に鎮座し、古くから讃岐の人々の信仰を集めてきた。",
        "old_map_detail": "📜 天保国絵図（1838年）の讃岐国に記載。稲積山山頂に鎮座する古社。\n江戸時代の讃岐国の絵図にも記された地域の守護神。近年は天空の鳥居として全国的に有名に。\n🔍 国立公文書館デジタルアーカイブで「元禄国絵図」「天保国絵図」を検索すると閲覧できます。",
        "cloud_info": "標高404mの山頂から見渡す讃岐平野と瀬戸内海。雲海が発生する日は特別な絶景が楽しめます。",
        "cloud_detail": "☁️ 雲海（秋〜冬の早朝）：讃岐平野を覆う雲海は絶景。\n🌤 積乱雲：夏の午後に瀬戸内海方面から発達します。\n⚠️ 正確な天気予報は気象庁等でご確認ください。",
        "trust_score": 1.0, "photo_url": "", "photo_credit": "",
        "approved": True, "location_limited": True, "location_limited_content": "現地限定⛩ 天空の鳥居・高屋神社へようこそ！標高404mの山頂から讃岐平野と瀬戸内海を見渡す絶景をお楽しみください。ここでしか撮れない写真をぜひ！",
    },
    {
        "id": "kamo_jinja_010", "name": "賀茂神社", "name_kana": "かもじんじゃ",
        "category": "shrine", "priority": 2, "wiki_title": "賀茂神社_(たつの市)",
        "lat": 34.766021, "lon": 134.502835, "altitude": 50,
        "prefecture": "兵庫県", "city": "たつの市",
        "description": "播磨国の式内社。京都の賀茂神社と同じ神様を祀る由緒ある神社。龍野の地を守る格式高い古社。",
        "main_detail": "⛩ 播磨国の式内社・由緒ある古社\n\n🌿 ご祭神\n　　賀茂別雷命（かもわけいかづちのみこと）。\n　　京都・賀茂神社と同じ神様を祀ります。\n\n🏯 龍野城との関係\n　　龍野藩の鎮守として歴代藩主に崇敬された。\n\n📜 歴史\n　　平安時代の延喜式に記された格式高い神社。",
        "urban_legend": "賀茂神社の境内に流れる清水を飲むと長寿になるという言い伝えが古くから地元に伝わっている。",
        "urban_legend_detail": "境内の湧き水は神の恵みとされ、この水を飲んだ者は百歳まで生きると言い伝えられています。\n\n⚠️ AIエンターテイメント情報です。",
        "powerspot": "京都の賀茂神社と同じ神様が宿る地。雷の神様のパワーが電撃的な運気アップをもたらすとされます。",
        "powerspot_detail": "賀茂別雷命は雷・電気・農業の神様。境内は特に仕事運・縁結びに効果があるとされています。\n\n⚠️ AIエンターテイメント情報です。",
        "festival": "春：例大祭（4月）・秋祭り（10月）",
        "festival_detail": "【4月】春の例大祭\n【10月】秋祭り（神輿渡御が盛大に行われる）\n※詳細はたつの市観光協会でご確認ください。",
        "healing_text": "📸 撮影スポット情報",
        "healing_detail": "📍 おすすめ撮影ポイント\n\n🌸 参道の桜（3月下旬〜4月）\n　　桜並木と鳥居の組み合わせが美しい。\n\n🌿 境内の御神木\n　　樹齢数百年の御神木は存在感抜群。\n\n🍂 秋の紅葉\n　　境内の紅葉が朱塗りの社殿を引き立てます。",
        "old_map_description": "平安時代の延喜式神名帳に記載された式内社。龍野藩の鎮守として歴代藩主に崇敬されてきた。",
        "old_map_detail": "📜 元禄国絵図（1702年）の播磨国・龍野周辺に記載。\n927年成立の延喜式に記された格式高い神社。龍野の地の守護神として崇敬を集めてきた。\n🔍 国立公文書館デジタルアーカイブで「元禄国絵図」「天保国絵図」を検索すると閲覧できます。",
        "cloud_info": "たつの市の平野部に位置し、揖保川と周囲の山々を見渡せる清々しいスポットです。",
        "cloud_detail": "境内の高台から揖保川と周辺の山々が見渡せます。\n⚠️ 正確な天気予報は気象庁等でご確認ください。",
        "trust_score": 0.9, "photo_url": "", "photo_credit": "",
        "approved": True, "location_limited": True, "location_limited_content": "現地限定⛩ 賀茂神社へようこそ！播磨の古社の清らかな空気を感じてください。境内の御神木に手を合わせてご神徳をいただいてください。",
    },
    {
        "id": "kokuzo_do_016",
        "name": "西林寺奥院 虚空蔵堂",
        "name_kana": "さいりんじおくのいん こくうぞうどう",
        "category": "temple",
        "priority": 2,
        "wiki_title": "虚空蔵菩薩",
        "lat": 35.0089,
        "lon": 134.5667,
        "altitude": 250,
        "prefecture": "兵庫県",
        "city": "宍粟市一宮町",
        "description": "宍粟市一宮町下野田に位置する西林寺の奥院。虚空蔵菩薩を本尊とし、無量の福徳と知恵をそなえ、すべての願いをかなえる仏様として信仰を集める。",
        "main_detail": "🛕 西林寺奥院・虚空蔵堂\n\n🙏 ご本尊：虚空蔵菩薩\n　　無量の福徳と知恵をそなえ、すべての願いをかなえる仏様。\n\n🌿 奥院の静寂\n　　山深い宍粟の地に佇む静かな霊場です。\n\n📜 歴史\n　　宍粟の山中に古くから伝わる信仰の地。\n\n🚶 アクセス\n　　宍粟市一宮町下野田。伊和神社周辺エリア。",
        "urban_legend": "虚空蔵菩薩は丑・寅年生まれの守護仏。山深い奥院では不思議な光が見えるという言い伝えが地元に残る。",
        "urban_legend_detail": "虚空蔵菩薩は宇宙の無限の知恵と福徳を持つとされ、古来より学業成就・記憶力向上のご利益で知られています。\n山深い奥院への石段を登ると、別世界に入るような不思議な感覚を覚えると参拝者が語ります。\n\n⚠️ AIエンターテイメント情報です。史実とは異なる場合があります。",
        "powerspot": "虚空蔵菩薩の強大なパワーが宿る山中の霊場。知恵・福徳・記憶力向上のご利益があるとされる。",
        "powerspot_detail": "虚空蔵菩薩は宇宙の無限の智慧を象徴する仏様。山中の清浄な空気の中で参拝すると、特別な加護を受けられると言われています。\n\n⚠️ AIエンターテイメント情報です。",
        "festival": "虚空蔵菩薩縁日・地域の法要",
        "festival_detail": "【毎月13日】虚空蔵菩薩縁日\n【年中】参拝自由\n※詳細は宍粟市観光協会でご確認ください。",
        "healing_text": "📸 撮影スポット情報",
        "healing_detail": "📍 おすすめ撮影ポイント\n\n🌿 奥院への石段\n　　山深い参道の静寂と緑が美しい。\n\n🌉 幸せ橋\n　　境内の幸せ橋は縁結びスポットとして人気。\n\n🍂 秋の紅葉\n　　山中の紅葉が境内を彩ります。\n\n☁️ 山霧の朝\n　　早朝に山霧が漂う幻想的な光景。",
        "old_map_description": "宍粟の山中に古くから伝わる虚空蔵菩薩の霊場。江戸時代の播磨国絵図にも宍粟郡として記載された地域。",
        "old_map_detail": "📜 元禄国絵図（1702年）の播磨国・宍粟郡に記載された地域に位置します。\n🔍 国立公文書館デジタルアーカイブで「元禄国絵図」を検索すると閲覧できます。",
        "cloud_info": "宍粟の山中に位置し、山霧や雲の変化が美しいスポットです。",
        "cloud_detail": "山中のため雲の動きが間近に観察できます。\n⚠️ 正確な天気予報は気象庁等でご確認ください。",
        "photo_url": "https://raw.githubusercontent.com/landscapingyama-afk/kanko-ar-app/main/images/sairinji.jpg",
        "photo_credit": "提供：ゆう様",
        "photo_url2": "https://raw.githubusercontent.com/landscapingyama-afk/kanko-ar-app/main/images/sairinjisiawasehasi.jpg",
        "photo_credit2": "提供：ゆう様（幸せ橋）",
        "trust_score": 0.85,
        "approved": True,
        "location_limited": True, "location_limited_content": "現地限定🛕 西林寺奥院 虚空蔵堂へようこそ！虚空蔵菩薩のご加護が満ちる霊場です。幸せ橋を渡って願いを込めてお参りください。"
    },
    {
        "id": "iwa_jinja_017",
        "name": "伊和神社",
        "name_kana": "いわじんじゃ",
        "category": "shrine",
        "priority": 1,
        "wiki_title": "伊和神社",
        "lat": 35.0412,
        "lon": 134.5748,
        "altitude": 200,
        "prefecture": "兵庫県",
        "city": "宍粟市一宮町",
        "description": "播磨国一宮。大己貴神を祀る式内名神大社で旧国幣中社。本殿が北向きという珍しい神社。農工商業・縁結び・病気平癒など多くのご神徳を持つ播磨三大社の一つ。",
        "main_detail": "⛩ 播磨国一宮・式内名神大社\n\n🙏 ご祭神：大己貴神（おおなむちのかみ）\n　　農・工・商業の神、縁結びの神、病気平癒の神。\n\n🦢 北向きの本殿\n　　白鶴が北を向いて眠っていたため北向きに建立。非常に珍しい。\n\n📜 播磨三大社\n　　海神社・粒坐天照神社と並ぶ播磨三大社の一つ。\n\n🎋 秋季大祭\n　　毎年10月15・16日。5台の屋台の練り合わせが有名。\n\n🅿️ 駐車場\n　　普通車180台（無料）",
        "urban_legend": "欽明天皇の時代、豪族・伊和恒郷に大己貴神から「我を祀れ」との神託があり、一夜にして木々が群生し白鶴2羽が石の上で北向きに眠っていたという神秘的な創建伝説が残る。",
        "urban_legend_detail": "神託を受けた伊和恒郷が翌朝社地を探すと、西の野に一夜で木々が群生し、大きな白鶴2羽が鶴石の上で北向きに眠っていました。\nこの不思議な出来事から、北向きの本殿が建てられたとされています。\n境内の「鶴石」は今も残り、神秘的な雰囲気を漂わせています。\n\n⚠️ 創建伝説に基づく内容です。",
        "powerspot": "播磨国一宮として最強クラスのパワースポット。縁結び・病気平癒・産業繁栄のご神徳を持つ大己貴神の総本社。",
        "powerspot_detail": "大己貴神は国造りを成し遂げた偉大な神様。その総本社である伊和神社は播磨随一のパワースポットとされています。\n北向きの本殿から北に向かって祈ると、特別なご加護があると言われています。\n\n⚠️ AIエンターテイメント情報です。",
        "festival": "10月15・16日：秋季大祭（屋台練り合わせ）・春祭り",
        "festival_detail": "【10月15・16日】秋季大祭\n　　5台の屋台の豪快な練り合わせが披露される播磨随一のお祭り。\n【春】春祭り\n【毎月1日・15日】月次祭\n※詳細は伊和神社（0790-72-0075）でご確認ください。",
        "healing_text": "📸 撮影スポット情報",
        "healing_detail": "📍 おすすめ撮影ポイント\n\n⛩ 大鳥居と参道\n　　堂々とした大鳥居と杉並木の参道が荘厳。\n\n🦢 鶴石\n　　創建伝説の白鶴が眠った鶴石。境内奥に鎮座。\n\n🍂 秋の境内（10月）\n　　秋季大祭の屋台と紅葉が美しい。\n\n🌸 春の桜（3〜4月）\n　　境内の桜と社殿のコントラストが美しい。",
        "old_map_description": "成務天皇14年または欽明天皇25年創建と伝わる播磨国一宮。江戸時代の播磨国絵図にも宍粟郡の重要な神社として記載された。",
        "old_map_detail": "📜 元禄国絵図（1702年）の播磨国・宍粟郡に記載。\n延喜式（927年）に名神大社として記された格式高い神社。地名「一宮町」は当社に由来します。\n🔍 国立公文書館デジタルアーカイブで「元禄国絵図」を検索すると閲覧できます。",
        "cloud_info": "宍粟の山々に囲まれた境内から見渡す空は澄んでいて、四季折々の雲が楽しめます。",
        "cloud_detail": "山に囲まれた宍粟盆地の清浄な空気の中、雲の動きが美しく観察できます。\n⚠️ 正確な天気予報は気象庁等でご確認ください。",
        "photo_url": "https://raw.githubusercontent.com/landscapingyama-afk/kanko-ar-app/main/images/iwa_jinjya.jpg?v=2",
        "photo_credit": "提供：ゆう様",
        "photo_position": "bottom",
        "trust_score": 1.0,
        "approved": True,
        "location_limited": True, "location_limited_content": "現地限定⛩ 播磨国一宮・伊和神社へようこそ！大己貴神のご神気が満ちる聖地です。北向きの本殿と鶴石をぜひご覧ください。"
    },
    {
        "id": "yoi_jinja_018",
        "name": "與位神社",
        "name_kana": "よいじんじゃ",
        "category": "shrine",
        "priority": 2,
        "wiki_title": "與位神社",
        "lat": 34.9953,
        "lon": 134.5547,
        "altitude": 130,
        "prefecture": "兵庫県",
        "city": "宍粟市山崎町",
        "description": "宍粟市山崎町与位に鎮座する式内社。素戔嗚命を祀り、伊和神社・子勝神社と合わせて「伊和三社」と称される由緒ある古社。中国自動車道をまたぐ朱塗の橋が目印。",
        "main_detail": "⛩ 式内社・伊和三社の一つ\n\n🙏 ご祭神：素戔嗚命（すさのおのみこと）\n　　嵐・海・農業の神。ヤマタノオロチ退治で有名な英雄神。\n\n🌉 朱塗の橋\n　　中国自動車道をまたぐ朱塗の橋のそばに鎮座する珍しい立地。\n\n📜 伊和三社\n　　伊和神社・與位神社・子勝神社（合祀）の三社は深い関係を持つ。\n\n🏠 家内安全・商売繁盛\n　　地域の守護神として崇敬を集める。",
        "urban_legend": "伊和大神が国土経営の際、父の素戔嗚命を與位大神として與位山の地に奉斎したという神話が伝わる。與位山には不思議な力が宿るという言い伝えがある。",
        "urban_legend_detail": "大己貴神（伊和大神）が国造りを終えた後、父・素戔嗚命をこの地に祀ったとされています。\n中国自動車道をまたぐ朱塗の橋は現代と古代をつなぐ象徴とも言われ、橋を渡ると別世界に入るような感覚を覚えると参拝者が語ります。\n\n⚠️ AIエンターテイメント情報です。史実とは異なる場合があります。",
        "powerspot": "素戔嗚命の強大なパワーが宿る伊和三社の一つ。家内安全・商売繁盛・厄除けのご神徳があるとされる。",
        "powerspot_detail": "素戔嗚命は荒ぶる神でありながら、農業や医療の神としても崇められています。\nヤマタノオロチを退治した英雄神のパワーが厄除けに効果があると言われています。\n\n⚠️ AIエンターテイメント情報です。",
        "festival": "秋祭り・例大祭",
        "festival_detail": "【秋季】例大祭\n　　地域の伝統的なお祭りが行われます。\n※詳細は宍粟市観光協会でご確認ください。",
        "healing_text": "📸 撮影スポット情報",
        "healing_detail": "📍 おすすめ撮影ポイント\n\n🌉 朱塗の橋と大鳥居\n　　中国自動車道をまたぐ珍しい朱塗の橋が絵になります。\n\n⛩ 境内の静寂\n　　落ち着いた雰囲気の境内でゆっくり参拝できます。\n\n🍂 秋の紅葉\n　　境内周辺の紅葉が美しい季節。",
        "old_map_description": "平安時代の延喜式神名帳に記された式内社。伊和三社の一つとして古くから宍粟の人々の信仰を集めてきた。",
        "old_map_detail": "📜 元禄国絵図（1702年）の播磨国・宍粟郡に記載。\n延喜式（927年）に「與比神社」として記された古社。伊和神社と深い関係を持つ伊和三社の一つ。\n🔍 国立公文書館デジタルアーカイブで「元禄国絵図」を検索すると閲覧できます。",
        "cloud_info": "揖保川沿いの与位周辺から見渡す宍粟の山々と空の眺めが美しいスポットです。",
        "cloud_detail": "揖保川と山々に囲まれた宍粟の清浄な空気の中、四季折々の雲が楽しめます。\n⚠️ 正確な天気予報は気象庁等でご確認ください。",
        "photo_url": "https://raw.githubusercontent.com/landscapingyama-afk/kanko-ar-app/main/images/yoi_jinjya.jpg",
        "photo_credit": "提供：ゆう様",
        "photo_position": "bottom",
        "trust_score": 0.9,
        "approved": True,
        "location_limited": True, "location_limited_content": "現地限定⛩ 與位神社へようこそ！延喜式に記された古社の清浄な空気を感じてください。揖保川の流れと山々の眺めが心を癒してくれます。"
    },
    {
        "id": "wakasano_tenman_012",
        "name": "若狭野天満宮",
        "name_kana": "わかさのてんまんぐう",
        "category": "shrine",
        "priority": 2,
        "wiki_title": "若狭野天満宮",
        "lat": 34.768611,
        "lon": 134.447222,
        "altitude": 50,
        "prefecture": "兵庫県",
        "city": "相生市",
        "description": "アジサイ神社として知られる天満宮。梅雨の時期に約200株のアジサイが境内を彩る播磨の花の名所。菅原道真公を祀る。",
        "main_detail": "⛩ 菅原道真公を祀る天満宮\n\n💠 アジサイ神社の別名\n　　境内には約200株のアジサイが咲き誇ります。\n\n🌸 見頃：6月上旬〜7月上旬\n　　梅雨の時期に様々な色のアジサイが鮮やかに咲きます。\n\n📜 和泉式部伝説\n　　書写山参詣の帰途、娘の小式部を若狭野に訪ねた式部伝説の地。\n\n🚌 アクセス\n　　国道2号沿いに位置。相生駅からバスでアクセス可能。",
        "urban_legend": "和泉式部が書写山参詣の帰路にこの地を訪れたという伝説が残る。天満宮の神木には不思議な力が宿るという言い伝えがある。",
        "urban_legend_detail": "平安の女流歌人・和泉式部がこの地を訪れた際、天満宮の神前で詠んだ歌が石碑に刻まれています。\n雨の日に境内を歩くと、菅原道真公の御霊が宿るアジサイが一層輝くと伝えられています。\n\n⚠️ AIエンターテイメント情報です。史実とは異なる場合があります。",
        "powerspot": "学問の神様・菅原道真公を祀るパワースポット。試験合格・学業成就を願う参拝者が多く訪れます。",
        "powerspot_detail": "菅原道真公の強い御神徳が宿る境内。梅雨の時期にはアジサイの精気とあいまって、特別なパワーが満ちるとされています。\n\n⚠️ AIエンターテイメント情報です。",
        "festival": "6〜7月：アジサイの時期（見頃）・1月25日：初天神",
        "festival_detail": "【6月上旬〜7月上旬】アジサイの見頃\n　　約200株のアジサイが境内を彩ります。\n【1月25日】初天神\n　　年始最初の天神の縁日。\n【毎月25日】天神縁日\n※詳細は相生市観光協会でご確認ください。",
        "healing_text": "📸 撮影スポット情報",
        "healing_detail": "📍 おすすめ撮影ポイント\n\n💠 アジサイと社殿（6月）\n　　青・紫・ピンクのアジサイと朱色の社殿のコントラストが美しい。\n\n🌧 雨上がりの朝\n　　水滴が光るアジサイは格別の美しさ。\n\n🌿 参道のアジサイ\n　　参道両側に咲き誇るアジサイのトンネルが人気。",
        "old_map_description": "相生市若狭野に鎮座する古社。江戸時代の播磨国絵図にも記された由緒ある天満宮。",
        "old_map_detail": "📜 元禄国絵図（1702年）の播磨国・相生周辺に記載。\n菅原道真公ゆかりの天満宮として、江戸時代から地域の人々の信仰を集めてきた。\n🔍 国立公文書館デジタルアーカイブで「元禄国絵図」を検索すると閲覧できます。",
        "cloud_info": "相生市の丘の上に位置し、播磨灘を見渡せる清々しいスポットです。",
        "cloud_detail": "境内から相生湾と播磨灘が望めます。梅雨の時期の曇り空もアジサイを引き立てます。\n⚠️ 正確な天気予報は気象庁等でご確認ください。",
        "trust_score": 0.9,
        "photo_url": "https://raw.githubusercontent.com/landscapingyama-afk/kanko-ar-app/main/images/wakasanotennmannguu.jpg",
        "photo_credit": "提供：ゆう様",
        "approved": True,
        "location_limited": True,
        "location_limited_content": "現地限定：梅雨の境内では約200株のアジサイが咲き誇ります。菅原道真公のご神前でぜひ学業成就をお祈りください。",
    },
    {
        "id": "izanagi_jingu_013",
        "name": "伊弉諾神宮",
        "name_kana": "いざなぎじんぐう",
        "category": "shrine",
        "priority": 1,
        "wiki_title": "伊弉諾神宮",
        "lat": 34.459967,
        "lon": 134.852439,
        "altitude": 5,
        "prefecture": "兵庫県",
        "city": "淡路市",
        "description": "日本最古の神社。古事記・日本書紀の国生み神話に登場する伊弉諾大神・伊弉冉大神を祀る淡路国一宮。兵庫県唯一の神宮。",
        "main_detail": "⛩ 日本最古の神社・淡路国一宮\n\n📜 国生み神話の聖地\n　　伊弉諾・伊弉冉の二神が日本列島を生んだ神話の地。\n\n🌳 夫婦の大楠\n　　県指定天然記念物。縁結び・夫婦円満のご神木。\n\n☀️ 陽の道しるべ\n　　神宮を中心に伊勢・出雲・諏訪など有名神社が配置されるパワースポット。\n\n🏛 格式\n　　延喜式名神大社・旧官幣大社。兵庫県唯一の神宮号。",
        "urban_legend": "伊弉諾神宮を中心に、伊勢神宮・出雲大社・諏訪大社など日本の有名神社が計算されたように配置されているという神秘的な伝説がある。",
        "urban_legend_detail": "「陽の道しるべ」と呼ばれるモニュメントが示すように、春分・秋分の日の出は伊勢神宮から昇り対馬の海神神社に沈みます。\n夏至には諏訪大社から出雲大社へ、冬至には熊野那智大社から高千穂へと太陽が移動する。これは古代人が計算して神社を配置したのではないかと言われています。\n\n⚠️ これは研究者の間でも議論のある説です。AIエンターテイメント情報も含みます。",
        "powerspot": "日本最古の神社として最強クラスのパワースポット。縁結び・夫婦円満・国家安泰のご神徳を持つ伊弉諾・伊弉冉の二神が宿る聖地。",
        "powerspot_detail": "国生みの神様が余生を過ごされた「幽宮（かくりのみや）」跡に創建された神社。\n夫婦の大楠は縁結び・夫婦円満の最強パワースポットとして知られています。\n境内に立つだけで神々しいエネルギーを感じると多くの参拝者が語ります。\n\n⚠️ AIエンターテイメント情報です。",
        "festival": "春分・秋分：陽の道しるべ祭・1月1日：歳旦祭",
        "festival_detail": "【1月1日】歳旦祭（年始の大祭）\n【春分の日】陽の道しるべ特別祈祷\n【秋分の日】陽の道しるべ特別祈祷\n【毎月1日・11日・21日】月次祭\n※詳細は伊弉諾神宮公式サイトでご確認ください。",
        "healing_text": "📸 撮影スポット情報",
        "healing_detail": "📍 おすすめ撮影ポイント\n\n🌳 夫婦の大楠\n　　樹齢推定900年の巨木。見上げると圧倒される存在感。\n\n⛩ 参道の朝の光\n　　早朝に差し込む光が神秘的な雰囲気を醸し出します。\n\n🌸 春の境内（3〜4月）\n　　桜と社殿のコントラストが美しい。\n\n☀️ 陽の道しるべモニュメント\n　　春分・秋分の日の出方向が刻まれた石碑。",
        "old_map_description": "古事記・日本書紀に記された日本最古の神社。江戸時代から全国の崇敬を集めた淡路国一宮。",
        "old_map_detail": "📜 天保国絵図（1838年）の淡路国に記載。\n延喜式（927年）にも名神大社として記された由緒ある神社。江戸時代には徳島藩主・蜂須賀氏が保護し東・西神門が建立された。\n🔍 国立公文書館デジタルアーカイブで「天保国絵図」を検索すると閲覧できます。",
        "cloud_info": "淡路島の平野部に位置し、周囲の田園風景と空の眺めが美しいスポットです。",
        "cloud_detail": "境内から見渡す淡路の空は澄んでいて、四季折々の雲が楽しめます。\n⚠️ 正確な天気予報は気象庁等でご確認ください。",
        "photo_url": "https://raw.githubusercontent.com/landscapingyama-afk/kanko-ar-app/main/images/izanagi_jingu.jpg",
        "photo_credit": "提供：ゆう様",
        "trust_score": 1.0,
        "approved": True,
        "location_limited": True, "location_limited_content": "現地限定⛩ 伊弉諾神宮へようこそ！日本最古の神社・国生み神話の聖地に立っています。夫婦の大楠のご神気をぜひ感じてみてください。"
    },
    {
        "id": "iwatsuhime_011", "name": "伊和都比売神社", "name_kana": "いわつひめじんじゃ",
        "category": "shrine", "priority": 2, "wiki_title": "伊和都比売神社",
        "lat": 34.727571, "lon": 134.408226, "altitude": 5,
        "prefecture": "兵庫県", "city": "赤穂市",
        "description": "播磨国の式内社。海辺に鎮座する縁結び・航海安全の神社。赤穂の地を守る由緒ある古社。",
        "main_detail": "⛩ 播磨国式内社・海辺の古社\n\n💕 ご神徳\n　　縁結び・夫婦円満・航海安全で知られます。\n\n🌊 瀬戸内海を望む\n　　海辺に鎮座し、瀬戸内海を一望できる神社。\n\n🌸 赤穂の歴史\n　　赤穂藩（忠臣蔵の地）の守護神として崇敬された。\n\n🦢 白鷺伝説\n　　神の使いとして白鷺が舞い降りたという伝説が残る。",
        "urban_legend": "満月の夜に海の方から白い光が神社に向かって流れてくるのを見た漁師の話が地元に伝わっている。",
        "urban_legend_detail": "満月の夜、海面に白い光の道が現れ神社まで続くのを見た漁師が多くいたと伝えられています。\n\n⚠️ AIエンターテイメント情報です。",
        "powerspot": "海と陸の境界に鎮座する縁結びの神様。二つの世界をつなぐ場所として、特別な縁を引き寄せるパワースポットとされます。",
        "powerspot_detail": "海辺に鎮座する伊和都比売命は縁結びの神様として知られています。潮風の中で手を合わせると良縁・夫婦円満のご利益があると言われます。\n\n⚠️ AIエンターテイメント情報です。",
        "festival": "春：例大祭（5月）・夏：海の神輿（7〜8月）",
        "festival_detail": "【5月】春の例大祭\n【7〜8月】夏祭り・海への神輿渡御\n※詳細は赤穂市観光協会でご確認ください。",
        "healing_text": "📸 撮影スポット情報",
        "healing_detail": "📍 おすすめ撮影ポイント\n\n🌅 海と鳥居（夕景）\n　　夕日に染まる瀬戸内海と鳥居のシルエットが絶景。\n\n🌊 海辺の参道\n　　波の音と潮風の中を歩く参道が風情豊か。\n\n🦢 白鷺が来る季節\n　　白鷺が舞い降りる瞬間を狙ってみましょう。",
        "old_map_description": "平安時代の延喜式神名帳に記載された式内社。海辺に鎮座し、古くから航海者・漁師に崇拝されてきた。",
        "old_map_detail": "📜 元禄国絵図（1702年）の播磨国・赤穂周辺に記載。\n927年成立の延喜式に記された古社。播磨灘を望む立地から瀬戸内の船乗りたちの信仰を集めてきた。\n🔍 国立公文書館デジタルアーカイブで「元禄国絵図」「天保国絵図」を検索すると閲覧できます。",
        "cloud_info": "瀬戸内海に面した境内から望む空と海の眺めは格別。播磨灘の雲の変化が楽しめます。",
        "cloud_detail": "☁️ 海霧（春〜初夏）：朝に瀬戸内海から霧が流れ込む幻想的な光景。\n🌤 入道雲（夏）：播磨灘に発達する入道雲は壮観。\n⚠️ 正確な天気予報は気象庁等でご確認ください。",
        "trust_score": 0.9, "photo_url": "", "photo_credit": "",
        "approved": True, "location_limited": True, "location_limited_content": "現地限定⛩ 伊和都比売神社へようこそ！相生湾を見守る女神の社です。海からの清らかな風と波音を感じながら参拝してください。",
    },
    # ★ 追加スポット（播磨・たつの市エリア）
]

DUMMY_DATA = {
    "urban_legend": "この地には古くから不思議な言い伝えが残っています。（サンプルデータ）",
    "urban_legend_detail": "APIキーを設定すると、Claude AIがその土地固有の都市伝説を生成します。\n⚠️ AIエンターテイメント情報です。",
    "powerspot": "大地のエネルギーが集まる特別な場所です。（サンプルデータ）",
    "powerspot_detail": "APIキーを設定するとパワースポット情報をAIが生成します。\n⚠️ AIエンターテイメント情報です。",
    "festival": "年間を通じて様々な祭事が行われています。（サンプルデータ）",
    "festival_detail": "APIキーを設定すると行事・祭りの詳細が表示されます。",
    "healing_sound": "📸 撮影スポット情報（サンプルデータ）",
    "healing_detail": "APIキーを設定するとその場所の撮影スポット情報が表示されます。",
    "old_map": "江戸時代の古地図に記された歴史ある場所です。（サンプルデータ）",
    "old_map_detail": "APIキーを設定すると古地図・歴史情報が表示されます。",
    "cloud": "積乱雲：夏の午後に発生しやすい雲です。（サンプルデータ）",
    "cloud_detail": "⚠️ 正確な天気予報は気象庁等でご確認ください。",
    "main_detail": "詳細情報を読み込めませんでした。（サンプルデータ）",
}

CATEGORY_STYLE = {
    "shrine":    {"icon":"⛩",  "line_color":"#FF9900"},
    "mountain":  {"icon":"🏔",  "line_color":"#44AA44"},
    "castle":    {"icon":"🏯",  "line_color":"#9966CC"},
    "temple":    {"icon":"🛕",  "line_color":"#FF88AA"},
    "historical":{"icon":"🏛",  "line_color":"#AA8844"},
    "default":   {"icon":"📍",  "line_color":"#4488FF"},
}
CATEGORY_PRIORITY = ["shrine","temple","mountain","castle","historical","default"]

MODES = {
    "🗺️ メイン案内":    {"key":"main",        "font":"Noto Sans JP",  "bg":"rgba(255,160,185,0.78)","pin_color":"#FFE8F0","icon":"⛩"},
    "🔮 都市伝説":      {"key":"urban_legend", "font":"Yuji Syuku",    "bg":"rgba(180,100,160,0.82)","pin_color":"#F5D0F0","icon":"🔮"},
    "📸 撮影スポット":  {"key":"healing",      "font":"Noto Serif JP", "bg":"rgba(255,170,200,0.76)","pin_color":"#FFE8F5","icon":"📸"},
    "⚡ パワースポット": {"key":"powerspot",    "font":"M PLUS 1p",    "bg":"rgba(255,140,160,0.78)","pin_color":"#FFE0E8","icon":"⚡"},
    "🎋 行事案内":      {"key":"festival",     "font":"Kosugi Maru",  "bg":"rgba(255,155,175,0.78)","pin_color":"#FFE5EC","icon":"🎋"},
    "📜 歴史案内":      {"key":"old_map",      "font":"Kaisei Decol", "bg":"rgba(240,155,170,0.80)","pin_color":"#FFE8E0","icon":"📜"},
    "🌤 雲判定":        {"key":"cloud",        "font":"Kosugi Maru",  "bg":"rgba(160,210,235,0.76)","pin_color":"#E8F8FF","icon":"🌤"},
    "🌙 夜モード":      {"key":"night",        "font":"Noto Sans JP",  "bg":"rgba(10,10,40,0.75)",  "pin_color":"#8888FF","icon":"🌙"},
    "🎴 おみくじ":      {"key":"omikuji",      "font":"Kaisei Decol",  "bg":"rgba(200,140,160,0.82)","pin_color":"#FFE8F0","icon":"🎴"},
}
FONT_CLASS = {
    "Noto Sans JP":"font-noto-sans","Yuji Syuku":"font-yuji-syuku",
    "Noto Serif JP":"font-noto-serif","M PLUS 1p":"font-mplus1p",
    "Kosugi Maru":"font-kosugi-maru","Kaisei Decol":"font-kaisei-decol",
}
# 翻訳機能無効のため削除
GSI_TILES = {
    "標準地図":       {"url":"https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png",          "attr":"国土地理院","max_zoom":18},
    "写真（空中写真）":{"url":"https://cyberjapandata.gsi.go.jp/xyz/seamlessphoto/{z}/{x}/{y}.jpg","attr":"国土地理院","max_zoom":18},
    "淡色地図":       {"url":"https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png",          "attr":"国土地理院","max_zoom":18},
    "陰影起伏図":     {"url":"https://cyberjapandata.gsi.go.jp/xyz/hillshademap/{z}/{x}/{y}.png",  "attr":"国土地理院","max_zoom":16},
}
OSM_TILE = {"url":"https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png","attr":"© OpenStreetMap contributors","max_zoom":19}

def haversine_km(lat1,lon1,lat2,lon2):
    R=6371.0; dlat=math.radians(lat2-lat1); dlon=math.radians(lon2-lon1)
    a=math.sin(dlat/2)**2+math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlon/2)**2
    return R*2*math.asin(math.sqrt(max(0.0,min(1.0,a))))
def bearing_deg(lat1,lon1,lat2,lon2):
    dlon=math.radians(lon2-lon1)
    x=math.sin(dlon)*math.cos(math.radians(lat2))
    y=math.cos(math.radians(lat1))*math.sin(math.radians(lat2))-math.sin(math.radians(lat1))*math.cos(math.radians(lat2))*math.cos(dlon)
    return (math.degrees(math.atan2(x,y))+360)%360
def deg_to_dir(deg):
    return ["北","北東","東","南東","南","南西","西","北西"][int((deg+22.5)/45)%8]
def dist_label(km):
    return f"{int(km*1000)}m" if km<1.0 else f"{km:.1f}km"
def opacity_by_dist(km):
    if km<0.5: return 1.0
    if km<1.0: return 0.90
    if km<3.0: return 0.78
    return 0.62

def filter_spots(spots, ulat, ulon):
    cands=[(sp,haversine_km(ulat,ulon,sp["lat"],sp["lon"]),bearing_deg(ulat,ulon,sp["lat"],sp["lon"]))
           for sp in spots if sp.get("approved")]
    cands.sort(key=lambda x:(x[1],CATEGORY_PRIORITY.index(x[0].get("category","default"))
                              if x[0].get("category","default") in CATEGORY_PRIORITY else 99))
    result,seen=[],set()
    for max_km,max_n,cats in [
        (0.5,  3, None),(2.0,  5, None),(5.0,  3, None),
        (50.0, 5, None),(100.0,2, ["mountain"]),
    ]:
        tier=[(sp,d,b) for sp,d,b in cands if sp["id"] not in seen and d<=max_km
              and (cats is None or sp.get("category") in cats)]
        tier.sort(key=lambda x:(CATEGORY_PRIORITY.index(x[0].get("category","default"))
                                 if x[0].get("category","default") in CATEGORY_PRIORITY else 99,x[1]))
        for item in tier[:max_n]:
            if item[0]["id"] not in seen:
                seen.add(item[0]["id"]); result.append(item)
    return result

def get_content(spot, mode_key, lang="ja"):
    try:
        field_map = {
            "main":         ("description","main_detail"),
            "urban_legend": ("urban_legend","urban_legend_detail"),
            "powerspot":    ("powerspot","powerspot_detail"),
            "healing":      ("healing_text","healing_detail"),
            "festival":     ("festival","festival_detail"),
            "old_map":      ("old_map_description","old_map_detail"),
            "cloud":        ("cloud_info","cloud_detail"),
            "night":        ("description","main_detail"),
        }
        if mode_key in field_map:
            sk,dk=field_map[mode_key]
            summary=spot.get(sk) or DUMMY_DATA.get(mode_key,"データなし")
            detail=spot.get(dk) or DUMMY_DATA.get(f"{mode_key}_detail","詳細なし")
            if mode_key in ("main","night") and spot.get("wiki_title"):
                wiki_text=fetch_wikipedia(spot["id"],spot["wiki_title"])
                if wiki_text and len(wiki_text)>20:
                    detail=detail+f"\n\n📖 Wikipedia より\n{wiki_text[:160]}…\n（出典: Wikipedia CC BY-SA）"
            if lang!="ja":
                summary=translate_deepl(summary,lang); detail=translate_deepl(detail,lang)
            return {"summary":summary,"detail":detail,"is_dummy":not bool(spot.get(sk))}
        return {"summary":DUMMY_DATA.get(mode_key,"データなし"),"detail":DUMMY_DATA.get(f"{mode_key}_detail","詳細なし"),"is_dummy":True}
    except Exception:
        return {"summary":"情報を読み込めませんでした。","detail":"管理者にお問い合わせください。","is_dummy":True}

def smooth_heading(new_val, buf):
    buf.append(new_val); return sum(buf)/len(buf)
def smooth_gps(new_lat, new_lon, prev_lat, prev_lon):
    if prev_lat is None: return new_lat, new_lon
    if haversine_km(prev_lat,prev_lon,new_lat,new_lon)*1000<20: return prev_lat, prev_lon
    return new_lat, new_lon


SENSOR_JS = """
<div id="sensor-panel" style="background:rgba(255,255,255,0.55);border-radius:14px;padding:14px 16px;
  font-family:'Noto Sans JP',sans-serif;font-size:13px;color:#2a4a7a;
  border:1px solid rgba(100,150,220,0.35);backdrop-filter:blur(8px);margin-bottom:6px;">
  <div style="font-weight:700;font-size:15px;margin-bottom:8px;">📡 センサー状態</div>
  <div><span id="gps-icon">🔵</span> <b>GPS：</b><span id="gps-status">未取得</span></div>
  <div style="font-size:12px;margin-top:4px;">緯度：<span id="disp-lat">--</span>　経度：<span id="disp-lon">--</span></div>
  <div style="font-size:12px;">精度：<span id="disp-acc">--</span>m　速度：<span id="disp-speed">--</span>km/h</div>
  <div style="margin-top:6px;"><span id="compass-icon">🔵</span> <b>コンパス：</b><span id="compass-status">待機中</span></div>
  <div style="font-size:12px;">方位：<span id="disp-heading">--</span>°（<span id="disp-dir">--</span>）</div>
  <div id="walk-warning" style="display:none;background:rgba(255,180,50,0.25);border:1.5px solid rgba(255,150,30,0.6);
    border-radius:8px;padding:6px 10px;margin-top:6px;font-weight:700;color:#7a3a00;font-size:12px;">
    ⚠️ 速度が高いです。立ち止まってください。</div>
  <div style="margin-top:10px;display:flex;gap:6px;flex-wrap:wrap;">
    <button onclick="startSensors()" style="background:linear-gradient(135deg,#6aaaf0,#4888e0);color:#fff;
      border:none;border-radius:8px;padding:8px 14px;font-size:13px;font-weight:700;cursor:pointer;">📡 GPS取得開始</button>
    <button onclick="calibrateCompass()" style="background:linear-gradient(135deg,#f0a0c0,#d870a0);color:#fff;
      border:none;border-radius:8px;padding:8px 14px;font-size:13px;font-weight:700;cursor:pointer;">🧭 コンパス校正</button>
  </div>
  <div id="calib-guide" style="display:none;background:rgba(220,180,240,0.35);border-radius:10px;
    padding:8px 12px;margin-top:8px;font-size:12px;color:#4a2a6a;">
    🔄 スマホを空中で<b>ゆっくり8の字</b>を描くように5〜10秒動かしてください。
    <button onclick="document.getElementById('calib-guide').style.display='none'"
      style="margin-left:8px;background:rgba(100,60,140,0.2);border:1px solid rgba(100,60,140,0.4);
      border-radius:6px;padding:2px 8px;font-size:11px;cursor:pointer;color:#4a2a6a;">✅ 完了</button>
  </div>
  <div id="send-status" style="font-size:10px;color:#8a9aaa;margin-top:6px;"></div>
</div>
<script>
let headingBuf=[],prevLat=null,prevLon=null,watchId=null,lastSend=0;
function toDir(d){return["北","北東","東","南東","南","南西","西","北西"][Math.round(d/45)%8];}
function avgHdg(buf){
  let s=0,c=0;for(const h of buf){s+=Math.sin(h*Math.PI/180);c+=Math.cos(h*Math.PI/180);}
  return((Math.atan2(s/buf.length,c/buf.length)*180/Math.PI)+360)%360;}
function sendToStreamlit(lat,lon,hdg,spd){
  const now=Date.now();if(now-lastSend<2000)return;lastSend=now;
  const u=new URL(window.parent.location.href);
  u.searchParams.set("gps_lat",lat.toFixed(6));u.searchParams.set("gps_lon",lon.toFixed(6));
  u.searchParams.set("gps_heading",hdg.toFixed(1));u.searchParams.set("gps_speed",spd.toFixed(1));
  u.searchParams.set("gps_active","1");
  window.parent.history.replaceState(null,"",u.toString());
  document.getElementById("send-status").textContent="✅ "+new Date().toLocaleTimeString()+" 送信済み";}
function startSensors(){
  if(!navigator.geolocation){document.getElementById("gps-status").textContent="非対応";document.getElementById("gps-icon").textContent="🔴";return;}
  if(watchId!==null)navigator.geolocation.clearWatch(watchId);
  watchId=navigator.geolocation.watchPosition(function(p){
    let lat=p.coords.latitude,lon=p.coords.longitude;
    const acc=p.coords.accuracy,spd=p.coords.speed?p.coords.speed*3.6:0;
    if(prevLat!==null){const dl=lat-prevLat,dl2=lon-prevLon;if(Math.sqrt(dl*dl+dl2*dl2)*111320<20){lat=prevLat;lon=prevLon;}}
    prevLat=lat;prevLon=lon;
    document.getElementById("gps-icon").textContent="🟢";
    document.getElementById("gps-status").textContent="取得中（±"+acc.toFixed(0)+"m）";
    document.getElementById("disp-lat").textContent=lat.toFixed(5);
    document.getElementById("disp-lon").textContent=lon.toFixed(5);
    document.getElementById("disp-acc").textContent=acc.toFixed(0);
    document.getElementById("disp-speed").textContent=spd.toFixed(1);
    document.getElementById("walk-warning").style.display=spd>=5?"block":"none";
    sendToStreamlit(lat,lon,headingBuf.length>0?avgHdg(headingBuf):0,spd);
  },function(e){document.getElementById("gps-icon").textContent="🔴";
    document.getElementById("gps-status").textContent=["","許可が必要です","取得できません","タイムアウト"][e.code]||"エラー";
  },{enableHighAccuracy:true,maximumAge:2000,timeout:10000});
  startCompass();}
function startCompass(){
  if(typeof DeviceOrientationEvent!=="undefined"&&typeof DeviceOrientationEvent.requestPermission==="function"){
    DeviceOrientationEvent.requestPermission().then(s=>{if(s==="granted")listenOri();
    else{document.getElementById("compass-icon").textContent="🔴";document.getElementById("compass-status").textContent="許可が必要";}}).catch(()=>{});}
  else listenOri();}
function listenOri(){window.addEventListener("deviceorientationabsolute",handleOri,true);window.addEventListener("deviceorientation",handleOri,true);}
function handleOri(e){
  let h=null;if(e.webkitCompassHeading!==undefined)h=e.webkitCompassHeading;
  else if(e.alpha!==null)h=(360-e.alpha)%360;
  if(h===null)return;
  headingBuf.push(h);if(headingBuf.length>10)headingBuf.shift();
  const sm=avgHdg(headingBuf);
  document.getElementById("compass-icon").textContent="🟢";
  document.getElementById("compass-status").textContent="取得中";
  document.getElementById("disp-heading").textContent=sm.toFixed(0);
  document.getElementById("disp-dir").textContent=toDir(Math.round(sm/45)*45%360);}
function calibrateCompass(){
  const g=document.getElementById("calib-guide");
  g.style.display=g.style.display==="none"?"block":"none";
  headingBuf=[];document.getElementById("compass-status").textContent="校正中…";}
</script>
"""

def build_map(user_lat, user_lon, heading, tile_name, zoom, mode_cfg, visible_spots):
    pin_color=mode_cfg["pin_color"]
    if tile_name=="OpenStreetMap":
        t=OSM_TILE; m=folium.Map(location=[user_lat,user_lon],zoom_start=zoom,tiles=t["url"],attr=t["attr"])
    else:
        t=GSI_TILES[tile_name]; m=folium.Map(location=[user_lat,user_lon],zoom_start=zoom,tiles=t["url"],attr=t["attr"],max_zoom=t["max_zoom"])
    for r_km,color,label in [(0.5,"#FF88AA","500m"),(2.0,"#88AAFF","2km"),(5.0,"#AADDFF","5km")]:
        folium.Circle([user_lat,user_lon],radius=r_km*1000,color=color,fill=True,fill_color=color,fill_opacity=0.05,weight=1.2,opacity=0.45,tooltip=label).add_to(m)
    fan=[[user_lat,user_lon]]
    for a in range(int(heading-30),int(heading+31),3):
        r=math.radians(a)
        fan.append([user_lat+(700/111320)*math.cos(r),user_lon+(700/(111320*math.cos(math.radians(user_lat))))*math.sin(r)])
    fan.append([user_lat,user_lon])
    folium.Polygon(fan,color="#FF99BB",fill=True,fill_color="#FF99BB",fill_opacity=0.22,weight=1.5,opacity=0.55).add_to(m)
    folium.Marker([user_lat,user_lon],
        popup=folium.Popup(f"<b>📍 現在地</b><br>{user_lat:.5f}, {user_lon:.5f}",max_width=200),
        tooltip="📍 現在地",
        icon=folium.DivIcon(html=f'<div style="width:36px;height:36px;background:#FF88AA;border:3px solid #FFF;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:18px;box-shadow:0 2px 8px rgba(0,0,0,0.4);transform:translate(-18px,-18px);">🧭</div>',icon_size=(36,36),icon_anchor=(18,18))).add_to(m)
    for sp,dist_km,brg in visible_spots:
        cat=sp.get("category","default"); sty=CATEGORY_STYLE.get(cat,CATEGORY_STYLE["default"]); opac=opacity_by_dist(dist_km)
        content=get_content(sp,mode_cfg["key"])
        folium.PolyLine([[user_lat,user_lon],[sp["lat"],sp["lon"]]],color=sty["line_color"],weight=1.6,opacity=opac*0.5,dash_array="6").add_to(m)
        folium.Marker([sp["lat"],sp["lon"]],
            popup=folium.Popup(f'<div style="font-family:sans-serif;min-width:180px;"><b>{sty["icon"]} {sp["name"]}</b><br><span style="font-size:11px;color:#888;">{sp.get("name_kana","")} / {sp["prefecture"]} {sp["city"]}</span><br><hr style="margin:3px 0"><span style="font-size:12px;">📏 {dist_label(dist_km)} / 🧭 {deg_to_dir(brg)}</span><br><span style="font-size:13px;">{content["summary"][:55]}{"…" if len(content["summary"])>55 else ""}</span></div>',max_width=260),
            tooltip=f'{sty["icon"]} {sp["name"]} ({dist_label(dist_km)})',
            icon=folium.DivIcon(html=f'<div style="background:{pin_color};border:2.5px solid #FF88AA;border-radius:50%;width:32px;height:32px;display:flex;align-items:center;justify-content:center;font-size:16px;box-shadow:0 2px 6px rgba(0,0,0,0.35);opacity:{opac};transform:translate(-16px,-32px);">{sty["icon"]}</div>',icon_size=(32,32),icon_anchor=(16,32))).add_to(m)
    MiniMap(toggle_display=True,tile_layer="CartoDB positron").add_to(m)
    return m

GLOBAL_CSS = "<style>\n@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;700&family=Noto+Serif+JP:wght@400;700&family=Yuji+Syuku&family=M+PLUS+1p:wght@700&family=Kosugi+Maru&family=Kaisei+Decol:wght@400;700&display=swap');\n#MainMenu,footer,header{visibility:hidden;}.stApp{background:linear-gradient(160deg,#c8e8fa 0%,#b8d0f5 22%,#c0dcf8 44%,#bbd4f8 66%,#cce6fb 85%,#c4dff8 100%)!important;}.block-container{padding-top:1rem!important;padding-bottom:1rem!important;max-width:900px!important;}.ar-card{border-radius:18px;padding:18px 22px;margin:10px 0;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border:1px solid rgba(255,255,255,0.60);box-shadow:0 5px 28px rgba(160,80,120,0.18);animation:fadeInUp 0.5s ease both;color:#1a0010!important;font-size:18px!important;font-weight:500;}.ar-card-title{font-size:23px;font-weight:800;color:#0a0008;margin-bottom:7px;display:flex;align-items:center;gap:8px;letter-spacing:0.04em;}.ar-card-kana{font-size:15px;color:#2a0a1a;font-weight:500;margin-bottom:10px;}.ar-card-summary{overflow:hidden;display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;font-size:17px;line-height:1.75;color:#1a0010;font-weight:500;margin-top:6px;}.ar-card-detail{font-size:16px;line-height:1.95;color:#0a0008;font-weight:500;white-space:pre-wrap;margin:12px -4px -4px;padding:14px 18px;background:rgba(0,0,0,0.08);border-radius:0 0 14px 14px;border-top:1px solid rgba(100,50,70,0.25);}.ar-detail-label{font-size:14px;color:#2a0a1a;font-weight:700;margin-top:10px;padding-top:7px;border-top:1px dashed rgba(100,50,70,0.30);}.ar-badge{display:inline-flex;align-items:center;gap:4px;background:rgba(0,0,0,0.12);border:1px solid rgba(100,50,70,0.35);border-radius:20px;padding:4px 12px;font-size:14px;margin:3px 4px 3px 0;color:#0a0008;font-weight:700;}.ar-disclaimer{font-size:13px;font-weight:500;margin-top:8px;color:#2a0a1a;}.ar-fallback-badge{display:inline-block;background:rgba(0,0,0,0.10);border:1px solid rgba(100,50,70,0.30);border-radius:8px;padding:2px 10px;font-size:13px;color:#2a0a1a;font-weight:600;margin-bottom:6px;}.wiki-badge{display:inline-block;background:rgba(60,120,200,0.15);border:1px solid rgba(60,120,200,0.35);border-radius:6px;padding:1px 8px;font-size:11px;color:#1a3a7a;font-weight:600;margin-left:6px;}.osm-badge{display:inline-block;background:rgba(60,180,80,0.15);border:1px solid rgba(60,180,80,0.35);border-radius:6px;padding:1px 8px;font-size:11px;color:#1a5a2a;font-weight:600;margin-left:4px;}@keyframes fadeInUp{from{opacity:0;transform:translateY(10px);}to{opacity:1;transform:translateY(0);}}.app-header{text-align:center;padding:14px 0 6px;animation:fadeInUp 0.4s ease both;}.app-header h1{font-family:'Kaisei Decol',serif;font-size:27px;color:#2a4a7a;text-shadow:0 0 22px rgba(140,180,255,0.45),0 1px 3px rgba(60,90,160,0.25);margin:0;letter-spacing:0.08em;}.app-header p{color:#4a6a9a;font-size:13px;margin:5px 0 0;}.safety-warning{background:rgba(255,255,255,0.62);border:1.5px solid rgba(100,140,200,0.55);border-radius:14px;padding:16px 20px;margin:10px 0 18px;animation:fadeInUp 0.5s ease both;text-align:center;backdrop-filter:blur(10px);}.safety-warning p{color:#2a4a7a;font-size:15px;font-weight:700;margin:0;line-height:1.65;}.info-panel{background:rgba(255,255,255,0.44);border-radius:10px;padding:10px 14px;color:#2a4a7a;font-size:13px;line-height:2.0;border:1px solid rgba(120,160,220,0.28);}.ar-compass{background:rgba(255,255,255,0.40);border-radius:12px;padding:10px 14px;margin-top:8px;color:#2a4a7a;font-size:13px;text-align:center;border:1px solid rgba(120,160,220,0.32);backdrop-filter:blur(6px);}.mode-title-bar{border-radius:12px;padding:10px 16px;margin-bottom:10px;border:1px solid rgba(255,255,255,0.55);text-align:center;color:#1a0010;font-size:18px;font-weight:700;animation:fadeInUp 0.3s ease both;backdrop-filter:blur(10px);}.location-limited-card{border-radius:14px;padding:14px 18px;margin:8px 0;background:rgba(200,230,255,0.60);border:1.5px solid rgba(120,180,240,0.55);backdrop-filter:blur(10px);}.lookaround-card{background:rgba(255,255,255,0.38);border-radius:14px;padding:14px 18px;margin:8px 0;border:1px solid rgba(120,160,220,0.35);color:#2a4a7a;font-size:15px;line-height:1.9;backdrop-filter:blur(6px);}.lookaround-card h4{color:#2a4a7a;margin:0 0 10px;font-size:17px;font-weight:700;}.map-placeholder{background:rgba(255,255,255,0.40);border:1px solid rgba(100,150,210,0.32);border-radius:14px;padding:20px;text-align:center;color:#3a5a8a;font-size:14px;margin:8px 0;}.share-card{background:rgba(255,255,255,0.42);border-radius:12px;padding:12px 16px;margin:8px 0;border:1px solid rgba(120,160,220,0.30);color:#2a4a7a;font-size:13px;}.cloud-result{border-radius:14px;padding:16px 18px;margin:8px 0;background:rgba(160,210,235,0.55);border:1px solid rgba(100,180,220,0.45);color:#1a3a5a;font-size:16px;}.report-form{background:rgba(255,255,255,0.42);border-radius:14px;padding:16px 18px;margin:8px 0;border:1px solid rgba(120,160,220,0.30);color:#2a4a7a;font-size:15px;}.app-footer{text-align:center;color:rgba(50,80,140,0.65);font-size:11px;padding:22px 0 10px;line-height:1.9;}.phase7-badge{display:inline-block;background:rgba(100,160,255,0.22);border:1px solid rgba(100,160,255,0.50);border-radius:8px;padding:2px 10px;font-size:12px;color:#2a4a9a;}.sensor-active-badge{display:inline-block;background:rgba(60,180,100,0.25);border:1px solid rgba(60,180,100,0.55);border-radius:8px;padding:2px 10px;font-size:12px;color:#1a6a3a;}.sensor-manual-badge{display:inline-block;background:rgba(100,140,220,0.20);border:1px solid rgba(100,140,220,0.45);border-radius:8px;padding:2px 10px;font-size:12px;color:#3a5a9a;}.gps-auto-note{background:rgba(60,180,100,0.15);border:1px solid rgba(60,180,100,0.40);border-radius:10px;padding:10px 14px;color:#1a5a30;font-size:13px;margin-bottom:8px;line-height:1.7;}.ar-view-container{background:rgba(10,15,40,0.90);border-radius:18px;padding:16px;margin:8px 0;border:1px solid rgba(100,150,255,0.35);box-shadow:0 4px 24px rgba(0,20,80,0.4);}.ar-horizon-bar{background:rgba(0,0,0,0.55);border-radius:10px;padding:10px 14px;margin-bottom:12px;border:1px solid rgba(80,120,200,0.4);font-family:'Noto Sans JP',sans-serif;font-size:14px;color:#AAD4FF;text-align:center;letter-spacing:0.05em;line-height:1.8;}.ar-compass-rose{text-align:center;font-size:13px;color:#88AAFF;margin-top:4px;font-family:'Noto Sans JP',sans-serif;}.omikuji-result{border-radius:20px;padding:24px 20px;margin:12px 0;text-align:center;backdrop-filter:blur(10px);}.font-noto-sans{font-family:'Noto Sans JP',sans-serif;}.font-yuji-syuku{font-family:'Yuji Syuku',serif;}.font-noto-serif{font-family:'Noto Serif JP',serif;}.font-mplus1p{font-family:'M PLUS 1p',sans-serif;font-weight:700;}.font-kosugi-maru{font-family:'Kosugi Maru',sans-serif;}.font-kaisei-decol{font-family:'Kaisei Decol',serif;}.stButton>button{background:linear-gradient(135deg,#6aaaf0 0%,#4888e0 100%)!important;color:#FFFFFF!important;border:none!important;border-radius:10px!important;font-weight:700!important;box-shadow:0 2px 10px rgba(80,130,220,0.32)!important;}hr{border-color:rgba(100,150,210,0.22)!important;}details summary{color:#2a4a7a!important;font-size:15px;}\n</style>"

def init_session():
    defaults = {
        "safety_shown":False,"map_zoom":13,
        "preset_lat":34.8330,"preset_lon":134.8620,
        "heading_buf":[],"prev_lat":None,"prev_lon":None,
        "sensor_mode":"manual","selected_lang":"ja",
        "osm_spots":[],"osm_loaded":False,
        "night_mode":False,"selected_area":"播磨エリア",
        "omikuji_result": None,
        "selected_spot_id": SPOT_DATA_BUILTIN[0]["id"] if SPOT_DATA_BUILTIN else None,
        "osm_center_lat": None,
        "osm_center_lon": None,
        "map_selected_spot_id": None,
        "prev_mode": None,
    }
    for k,v in defaults.items():
        if k not in st.session_state: st.session_state[k]=v

def get_gps_from_params():
    try:
        p=st.query_params
        if p.get("gps_active")!="1": return None,None,None,None
        lat=float(p.get("gps_lat","0")); lon=float(p.get("gps_lon","0"))
        hdg=float(p.get("gps_heading","0")); spd=float(p.get("gps_speed","0"))
        if lat==0.0 and lon==0.0: return None,None,None,None
        return lat,lon,hdg,spd
    except Exception: return None,None,None,None

# ============================================================
# ■ ARカードレンダリング（★古地図表示追加済み）
# ============================================================
def render_spot_card(spot, mode_cfg, dist_km, brg, expanded, lang="ja"):
    mode_key=mode_cfg["key"]
    content=get_content(spot,mode_key,lang)
    fc=FONT_CLASS.get(mode_cfg["font"],"font-noto-sans")
    opac=opacity_by_dist(dist_km)
    cat_icon=CATEGORY_STYLE.get(spot.get("category","default"),CATEGORY_STYLE["default"])["icon"]
    is_osm=spot.get("id","").startswith("osm_")
    disclaimer=""
    if mode_key in ("urban_legend","powerspot"):
        disclaimer="⚠️ このコンテンツはAIが生成したエンターテイメント情報です。史実とは異なる場合があります。"
    elif mode_key=="cloud":
        disclaimer="⚠️ 雲の分析はAIによるものです。正確な天気予報は気象庁等をご確認ください。"
    label_map={"main":"🔍 見どころ・詳細情報","urban_legend":"📖 詳しい言い伝え",
               "powerspot":"✨ パワースポット詳細","healing":"📸 撮影スポット詳細",
               "festival":"🗓 行事・祭り日程","old_map":"📜 歴史・古地図情報",
               "cloud":"☁️ 雲と天気の詳細","night":"🌙 夜の見どころ"}
    fb='<span class="ar-fallback-badge">📡 サンプルデータ</span>' if content["is_dummy"] and not is_osm else ""
    osm_badge='<span class="osm-badge">OSM</span>' if is_osm else ""
    wiki_badge='<span class="wiki-badge">Wikipedia</span>' if spot.get("wiki_title") and not is_osm else ""

    # OSMスポットは詳細展開・免責・サマリーを非表示にしてシンプル表示
    if is_osm:
        det = ""
        disclaimer = ""
        summary_html = '<div class="ar-card-summary" style="color:rgba(255,255,255,0.7);font-size:14px;">📍 周辺スポット（OpenStreetMapデータ）</div>'
    else:
        det=(f'<div class="ar-detail-label">▼ {label_map.get(mode_key,"📋 詳細情報")}</div>'
             f'<div class="ar-card-detail">{content["detail"]}</div>') if expanded else ""
        summary_html = f'<div class="ar-card-summary">{content["summary"]}</div>'

    # 写真表示（photo_urlがある場合）
    photo_url = spot.get("photo_url","")
    photo_credit = spot.get("photo_credit","")

    html=(f'<div class="ar-card {fc}" style="background:{mode_cfg["bg"]};opacity:{opac};">'
          + fb + osm_badge + wiki_badge
          + f'<div class="ar-card-title">{cat_icon} {spot["name"]}</div>'
          + f'<div class="ar-card-kana">{spot.get("name_kana","")} ／ {spot["prefecture"]} {spot["city"]}</div>'
          + f'<span class="ar-badge">📏 {dist_label(dist_km)}</span>'
          + f'<span class="ar-badge">🧭 {deg_to_dir(brg)} {int(brg)}°</span>'
          + f'<span class="ar-badge">🏔 {spot["altitude"]}m</span>'
          + summary_html
          + det
          + "</div>")
    st.markdown(html, unsafe_allow_html=True)

    # 写真があれば表示（小さめサイズ）
    if photo_url and not is_osm and isinstance(photo_url, str) and photo_url.startswith("http"):
        photo_pos = spot.get("photo_position", "center")
        st.markdown(
            f'''<div style="margin:6px 0 8px 0;text-align:center;">
            <img src="{photo_url}"
            style="width:60%;max-width:240px;max-height:160px;
            object-fit:cover;object-position:{photo_pos};border-radius:10px;display:inline-block;
            box-shadow:0 2px 8px rgba(0,0,0,0.2);"
            onerror="this.style.display='none'"/>
            <div style="font-size:11px;color:#5a6a8a;text-align:right;
            padding:2px 8px;">📷 {photo_credit}</div>
            </div>''',
            unsafe_allow_html=True
        )

    # 2枚目の写真があれば表示
    photo_url2 = spot.get("photo_url2", "")
    photo_credit2 = spot.get("photo_credit2", "")
    if photo_url2 and not is_osm and isinstance(photo_url2, str) and photo_url2.startswith("http"):
        st.markdown(
            f'''<div style="margin:2px 0 8px 0;text-align:center;">
            <img src="{photo_url2}"
            style="width:60%;max-width:240px;max-height:160px;
            object-fit:cover;border-radius:10px;display:inline-block;
            box-shadow:0 2px 8px rgba(0,0,0,0.2);"
            onerror="this.style.display='none'"/>
            <div style="font-size:11px;color:#5a6a8a;text-align:right;
            padding:2px 8px;">📷 {photo_credit2}</div>
            </div>''',
            unsafe_allow_html=True
        )

    # ★ 古地図モードの時だけ古地図画像カードを表示
    if mode_key == "old_map" and expanded:
        show_old_map_image(spot)


import random

OMIKUJI_DATA = {
    "大吉": {"prob":0.50,"color":"#FFD700","bg":"rgba(255,200,50,0.25)","border":"rgba(255,200,50,0.70)","messages":["素晴らしい！最高の運気です！✨","人生最高！全てがうまくいく予感！🌟","毎日が楽しみですね！輝く未来が待っています！🌸"],"kotowaza":[("天は自ら助くる者を助く。人は成功を命じることはできない。努力してこそ成功を手にすることができる。","ベンジャミン・フランクリン（1790年没）"),("夢なき者に理想なし。理想なき者に計画なし。計画なき者に実行なし。実行なき者に成功なし。","吉田松陰（1859年没）"),("知っているだけでは十分ではない。行わなければならない。望むだけでは十分ではない。実行しなければならない。","ゲーテ（1832年没）"),("不可能という言葉は愚か者の辞書にのみ存在する。","ナポレオン・ボナパルト（1821年没）"),("最大の危険は目標が高すぎて届かないことではない。低すぎる目標に到達して満足してしまうことである。","ミケランジェロ（1564年没）"),("積小為大。小を積みて大となす。","二宮尊徳（1856年没）"),("始めることが最も重要な部分である。なぜなら始めることで、その仕事の半分は終わったも同然だからである。","プラトン（紀元前348年没）"),("我々は繰り返し行うことの結果である。ゆえに卓越とは一度の行為ではなく習慣である。","アリストテレス（紀元前322年没）"),
            ("人は城、人は石垣、人は堀。情けは味方、仇は敵なり。","武田信玄（1573年没）"),
            ("成功とは熱意を失わずに失敗を重ねることである。","チャーチル（1965年没）"),
            ("人はどう生きるべきか。愛することのみが答えである。","トルストイ（1910年没）"),
            ("仁に過ぎれば弱くなる。義に過ぎれば固くなる。礼に過ぎれば諂いとなる。","伊達政宗（1636年没）"),
            ("楽しみは苦しみの種、苦しみは楽しみの種。","貝原益軒（1714年没）"),
            ("発明とは地味なものだ。あきらめは許されない。忍耐、どんなことにも負けないねばりが必要である。","エジソン（1931年没）"),
            ("人生に失敗した人の多くは、諦めたときに自分がどれほど成功に近づいていたか気づかなかった人たちだ。","エジソン（1931年没）"),
            ("今日なしうることだけに全力を注げ。そうすれば明日は一段の進歩を見るだろう。","アイザック・ニュートン（1727年没）"),
            ("人生とは素晴らしく興味の多いところです。色々な事が起こりますが、大抵は予想しなかったことです。","アレクサンダー・グラハム・ベル（1922年没）"),
            ("未来を考えない者に未来はない。","ヘンリー・フォード（1947年没）"),
            ("人生は気高いもの。自然から授かったこの宝石を人は磨く。輝く光がその労に報いてくれるまで。","アルフレッド・ノーベル（1896年没）")]},
    "吉":   {"prob":0.25,"color":"#FF88AA","bg":"rgba(255,136,170,0.20)","border":"rgba(255,136,170,0.60)","messages":["良い運気が流れています！前向きに進もう！💪","幸運があなたのそばにいます！🍀","今日も素敵な一日になりそうです！🌺"],"kotowaza":[("自分を信じよ、そうすれば生き方がわかる。","ゲーテ（1832年没）"),("継続は力なり。","エジソンの精神より"),("人を愛する者は、人にも愛される。","孔子（紀元前479年没）"),("進むべき道を行け。そこに道を作れ。","エマーソン（1882年没）"),("千里の道も一歩から。","老子（紀元前の哲人）"),("学ぶことによって心は若返る。学びは魂を老いから守る。","レオナルド・ダ・ヴィンチ（1519年没）"),("敬天愛人。天を敬い、人を愛する。","西郷隆盛（1877年没）"),("天は人の上に人を造らず、人の下に人を造らず。","福沢諭吉（1901年没）"),("知識は力なり。しかし知識を活用してこそ真の力となる。","フランシス・ベーコン（1626年没）")]},
    "中吉": {"prob":0.15,"color":"#88AAFF","bg":"rgba(136,170,255,0.20)","border":"rgba(136,170,255,0.60)","messages":["着実に前進しています！この調子で！🚶","コツコツと積み上げる今日が大切です！📚","穏やかで幸せな日々が続きます！🌿"],"kotowaza":[("平和な心があれば、全ては豊かである。","キケロ（紀元前43年没）"),("忍耐は苦い。しかしその実は甘い。","ルソー（1778年没）"),("急がば回れ。","シェイクスピアの精神に通じる格言"),("知足者富。足るを知る者は富む。","老子（紀元前の哲人）"),("唯一の真の知恵とは、自分が何も知らないことを知ることである。","ソクラテス（紀元前399年没）"),("人の生はその人の思考の色に染まる。だから魂を善き思いで満たしなさい。","マルクス・アウレリウス（180年没）"),("運とは備えある者に機会が訪れたものである。","セネカ（65年没）"),("人に何かを教えることはできない。その人が自らの中に答えを見つける手助けができるだけである。","ガリレオ・ガリレイ（1642年没）"),("我思う、ゆえに我あり。","ルネ・デカルト（1650年没）")]},
    "小吉": {"prob":0.07,"color":"#88DDAA","bg":"rgba(136,220,170,0.20)","border":"rgba(136,220,170,0.60)","messages":["小さな幸せが積み重なっています！🌱","今は準備の時！必ず花開きます！🌸","丁寧に過ごすことで運気が上がります！✨"],"kotowaza":[("現在こそが唯一の現実である。","ヘラクレイトス（紀元前475年頃没）"),("彼を知り己を知れば百戦危うからず。","孫子（紀元前の兵法家）"),("天の時は地の利に如かず。地の利は人の和に如かず。","孟子（紀元前289年没）"),("人を悩ませるのは出来事そのものではない。出来事についての考え方である。","エピクテトス（135年没）"),("私が遠くを見ることができたのは巨人の肩の上に立っていたからである。","アイザック・ニュートン（1727年没）"),("失われた時間は二度と戻らない。だから今日できることを明日に延ばすな。","ベンジャミン・フランクリン（1790年没）"),("世の人は我を何とも言わば言え。我がなす事は我のみぞ知る。","坂本龍馬（1867年没）"),("太陽を遮らないでくれ。権力者の前でも自由を失わなかった精神を表す言葉。","ディオゲネス（紀元前323年没）")]},
    "末吉": {"prob":0.02,"color":"#AAAAAA","bg":"rgba(180,180,180,0.20)","border":"rgba(180,180,180,0.60)","messages":["今は嵐の前の静けさ。必ず晴れ間が来ます！☀️","どんな状況も学びのチャンスです！📖","今日の努力が明日の幸せを作ります！💪"],"kotowaza":[("人生とは重荷を背負いて遠き道を行くがごとし。急ぐべからず。不自由を常と思えば不足なし。","徳川家康（1616年没）"),("冬来たりなば春遠からじ。","シェリー（1822年没）"),("苦難の中にこそ、真の幸福の種がある。","ルソー（1778年没）"),("涙の後には必ず笑顔が来る。","ヴィクトル・ユゴー（1885年没）"),("賽は投げられた。もはや後戻りはできない。","ユリウス・カエサル（紀元前44年没）"),("小さなことに忠実な者は大きなことにも忠実である。","パスカル（1662年没）"),("人間の不幸のほとんどは、静かに一人で部屋にいられないことから生じる。","パスカル（1662年没）")]},
    "大凶": {"prob":0.01,"color":"#FF6666","bg":"rgba(255,100,100,0.18)","border":"rgba(255,100,100,0.55)","messages":["大凶は大吉への入り口！ここからが逆転劇の始まりです！🔥","どん底から這い上がれるのが本物の強さです！💎","大凶を引いたあなたは超レア！特別な存在です！⭐"],"kotowaza":[("どんなに暗い夜も、夜明けは来る。","ヴィクトル・ユゴー（1885年没）"),("七転び八起き、これが人生だ。","日本の古い教え"),("最大の失敗は挑戦しないことだ。","エジソン（1931年没）"),("時は金なり。しかし時間は金よりも貴重である。","ベンジャミン・フランクリン（1790年没）"),("これを知る者はこれを好む者に如かず。これを好む者はこれを楽しむ者に如かず。","孔子（紀元前479年没）")]}
}

def draw_omikuji():
    r=random.random(); cumulative=0.0
    for result,data in OMIKUJI_DATA.items():
        cumulative+=data["prob"]
        if r<cumulative: return result
    return "大吉"

def render_omikuji(spot_name):
    st.markdown(f'<div style="background:rgba(200,140,160,0.30);border-radius:16px;padding:20px;margin:10px 0;border:1px solid rgba(200,140,160,0.50);text-align:center;backdrop-filter:blur(8px);"><div style="font-family:\'Kaisei Decol\',serif;font-size:22px;color:#3a1a2a;font-weight:700;margin-bottom:6px;">🎴 おみくじ</div><div style="font-size:15px;color:#5a2a3a;margin-bottom:14px;">{spot_name}でおみくじを引いてみましょう！</div></div>',unsafe_allow_html=True)
    col1,col2,col3=st.columns([1,2,1])
    with col2:
        shake=st.button("🎋 おみくじ筒を振る！",type="primary",use_container_width=True,key="omikuji_btn")
    if shake or st.session_state.get("omikuji_result"):
        if shake:
            result=draw_omikuji(); st.session_state.omikuji_result=result
        else:
            result=st.session_state.omikuji_result
        data=OMIKUJI_DATA[result]; msg=random.choice(data["messages"]); koto,source=random.choice(data["kotowaza"])
        st.markdown(f'<div style="background:{data["bg"]};border:2px solid {data["border"]};border-radius:20px;padding:24px 20px;margin:12px 0;text-align:center;backdrop-filter:blur(10px);animation:fadeInUp 0.6s ease both;"><div style="font-family:\'Kaisei Decol\',serif;font-size:52px;color:{data["color"]};font-weight:700;text-shadow:0 0 20px {data["color"]};margin-bottom:10px;letter-spacing:0.1em;">{result}</div><div style="font-size:18px;color:#3a1a2a;font-weight:700;margin-bottom:18px;">{msg}</div><div style="border-top:1px dashed rgba(100,60,80,0.3);margin:16px 0;padding-top:16px;"><div style="font-size:17px;font-family:\'Kaisei Decol\',serif;color:#2a1a2a;line-height:1.8;margin-bottom:8px;">「{koto}」</div><div style="font-size:13px;color:rgba(80,40,60,0.75);">— {source}</div></div></div>',unsafe_allow_html=True)
        col4,col5,col6=st.columns([1,2,1])
        with col5:
            if st.button("🔄 もう一度引く",use_container_width=True,key="omikuji_retry"):
                st.session_state.omikuji_result=None; st.rerun()
        st.markdown('<div style="font-size:11px;color:rgba(100,60,80,0.65);text-align:center;margin-top:8px;">※ おみくじはエンターテイメントです。参考程度にお楽しみください。</div>',unsafe_allow_html=True)

def render_lookaround_nav(visible_spots, heading):
    if not visible_spots: return
    lines=[]
    for sp,dist_km,brg in visible_spots[:5]:
        icon=CATEGORY_STYLE.get(sp.get("category","default"),CATEGORY_STYLE["default"])["icon"]
        rel=(brg-heading+360)%360
        arrow=("↑ 正面" if rel<30 or rel>330 else "↗ 右前方" if rel<90 else "→ 右方" if rel<150 else "↓ 後方" if rel<210 else "← 左方" if rel<270 else "↖ 左前方")
        lines.append(f'<b>{icon} {sp["name"]}</b> — {arrow} {dist_label(dist_km)}<span style="opacity:0.7;font-size:13px;"> ({deg_to_dir(brg)}方向)</span>')
    st.markdown('<div class="lookaround-card font-noto-sans"><h4>🗺️ 見下ろしナビ</h4>'+"<br>".join(lines)+"</div>",unsafe_allow_html=True)

def render_ar_view(visible_spots, heading, user_lat, user_lon):
    COMPASS_CHARS={0:"北",45:"北東",90:"東",135:"南東",180:"南",225:"南西",270:"西",315:"北西"}
    spot_positions={}
    for sp,dist_km,brg in visible_spots[:6]:
        icon=CATEGORY_STYLE.get(sp.get("category","default"),CATEGORY_STYLE["default"])["icon"]
        spot_positions[brg]=f"{icon}{sp['name'][:4]}"
    def make_horizon():
        segs=[]
        for offset in range(-30,31,3):
            angle=(heading+offset+360)%360; label=""
            for deg,name in COMPASS_CHARS.items():
                if abs((angle-deg+360)%360)<2: label=f"【{name}】"; break
            for sp_brg,sp_label in spot_positions.items():
                if abs((angle-sp_brg+360)%360)<3: label=f"▼{sp_label}"; break
            if not label: label="·" if offset%15==0 else " "
            segs.append(label)
        return "  ".join(segs)
    cx,cy,r=130,130,110; rings_svg=""
    for ring_r,ring_color,ring_label in [(35,"rgba(255,80,120,0.4)","0.5km"),(65,"rgba(80,140,255,0.3)","2km"),(100,"rgba(80,200,255,0.2)","5km")]:
        rings_svg+=(f'<circle cx="{cx}" cy="{cy}" r="{ring_r}" fill="none" stroke="{ring_color}" stroke-width="1" stroke-dasharray="4,4"/><text x="{cx+ring_r+2}" y="{cy-3}" font-size="9" fill="{ring_color}" font-family="sans-serif">{ring_label}</text>')
    compass_lines=""
    for angle_deg,label in [(0,"N"),(45,"NE"),(90,"E"),(135,"SE"),(180,"S"),(225,"SW"),(270,"W"),(315,"NW")]:
        rad=math.radians(angle_deg-90); x1=cx+int(r*0.85*math.cos(rad)); y1=cy+int(r*0.85*math.sin(rad)); x2=cx+int(r*0.95*math.cos(rad)); y2=cy+int(r*0.95*math.sin(rad)); lx=cx+int((r+14)*math.cos(rad)); ly=cy+int((r+14)*math.sin(rad))
        compass_lines+=(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="rgba(100,160,255,0.6)" stroke-width="1"/><text x="{lx}" y="{ly+4}" font-size="10" fill="#88AAFF" text-anchor="middle" font-family="sans-serif" font-weight="bold">{label}</text>')
    arrow_rad=math.radians(heading-90); arrow_len=70; ax=cx+int(arrow_len*math.cos(arrow_rad)); ay=cy+int(arrow_len*math.sin(arrow_rad))
    arrow_svg=(f'<line x1="{cx}" y1="{cy}" x2="{ax}" y2="{ay}" stroke="#FF88AA" stroke-width="2.5" stroke-linecap="round"/><circle cx="{ax}" cy="{ay}" r="4" fill="#FF88AA" opacity="0.9"/>')
    max_dist=5.0; spots_svg=""
    for sp,dist_km,brg in visible_spots[:6]:
        cat=sp.get("category","default"); plot_r=min(dist_km/max_dist,1.0)*95; sp_rad=math.radians(brg-90); sx=cx+int(plot_r*math.cos(sp_rad)); sy=cy+int(plot_r*math.sin(sp_rad))
        dot_color={"shrine":"#FFD700","mountain":"#88FF88","castle":"#CC88FF","temple":"#FF88AA"}.get(cat,"#88CCFF"); name_short=sp["name"][:4]
        spots_svg+=(f'<circle cx="{sx}" cy="{sy}" r="6" fill="{dot_color}" opacity="0.9" stroke="rgba(255,255,255,0.6)" stroke-width="1.5"/><text x="{sx}" y="{sy-10}" font-size="10" fill="{dot_color}" text-anchor="middle" font-family="sans-serif" font-weight="bold">{name_short}</text><text x="{sx}" y="{sy+20}" font-size="9" fill="rgba(200,220,255,0.8)" text-anchor="middle" font-family="sans-serif">{dist_label(dist_km)}</text>')
    center_svg=(f'<circle cx="{cx}" cy="{cy}" r="7" fill="#FF88AA" stroke="white" stroke-width="2" opacity="0.95"/><text x="{cx}" y="{cy+20}" font-size="10" fill="#FF88AA" text-anchor="middle" font-family="sans-serif">現在地</text>')
    svg=(f'<svg width="260" height="260" xmlns="http://www.w3.org/2000/svg"><defs><radialGradient id="rg"><stop offset="0%" stop-color="rgba(20,40,100,0.85)"/><stop offset="100%" stop-color="rgba(5,10,40,0.95)"/></radialGradient></defs><circle cx="{cx}" cy="{cy}" r="{r}" fill="url(#rg)" stroke="rgba(100,160,255,0.5)" stroke-width="2"/>'+rings_svg+compass_lines+spots_svg+arrow_svg+center_svg+f'</svg>')
    legend="　".join(['<span style="color:'+{"shrine":"#FFD700","mountain":"#88FF88","castle":"#CC88FF","temple":"#FF88AA"}.get(sp.get("category","default"),"#88CCFF")+';">'+CATEGORY_STYLE.get(sp.get("category","default"),CATEGORY_STYLE["default"])["icon"]+sp["name"][:4]+'</span>' for sp,_,_ in visible_spots[:4]])
    st.markdown(f'<div class="ar-view-container"><div style="color:#88AAFF;font-size:14px;font-weight:700;margin-bottom:8px;">🎯 簡易ARビュー　🧭 {heading:.0f}°（{deg_to_dir(heading)}）</div><div class="ar-horizon-bar">{make_horizon()}</div><div style="text-align:center;">{svg}</div><div class="ar-compass-rose">● 現在地　{legend}</div></div>',unsafe_allow_html=True)

def main():
    init_session(); init_db()
    st.markdown(GLOBAL_CSS, unsafe_allow_html=True)
    # エラー表示を非表示
    st.markdown('<style>div[data-testid="stException"],div[class*="stException"]{display:none!important;height:0!important;overflow:hidden!important;margin:0!important;padding:0!important;}</style>', unsafe_allow_html=True)
    st.markdown('''<div class="app-header">
        <h1 style="font-family:'Kaisei Decol',serif;font-size:24px;
        background:linear-gradient(135deg,#2a4a7a,#6a4a9a,#2a6a9a);
        -webkit-background-clip:text;-webkit-text-fill-color:transparent;
        background-clip:text;
        text-shadow:none;
        letter-spacing:0.06em;font-weight:700;white-space:nowrap;
        filter:drop-shadow(0 2px 4px rgba(80,60,140,0.3));">
        ✦ 観光スポットナビ ✦</h1>
        <p style="font-family:'Kosugi Maru',sans-serif;font-size:13px;color:#4a6a9a;
        letter-spacing:0.05em;">
        GPS連動・都市伝説・おみくじ・雲判定…<br>旅をもっと楽しくする10の機能</p>
        </div>''', unsafe_allow_html=True)

    if not st.session_state.safety_shown:
        st.markdown('<div class="safety-warning"><p>⚠️ 歩きながらの使用は危険です。<br>必ず立ち止まってご使用ください。</p></div>',unsafe_allow_html=True)
        if st.button("✅ 確認しました",type="primary",use_container_width=True):
            st.session_state.safety_shown=True; st.rerun()
        st.stop()

    gps_lat,gps_lon,gps_heading,gps_speed=get_gps_from_params()
    gps_active=gps_lat is not None
    if gps_active:
        buf=deque(st.session_state.heading_buf,maxlen=10)
        gps_heading=smooth_heading(gps_heading,buf); st.session_state.heading_buf=list(buf)
        gps_lat,gps_lon=smooth_gps(gps_lat,gps_lon,st.session_state.prev_lat,st.session_state.prev_lon)
        st.session_state.prev_lat=gps_lat; st.session_state.prev_lon=gps_lon

    col_ctrl,col_main=st.columns([1,2],gap="medium")

    with col_ctrl:
        st.components.v1.html(SENSOR_JS,height=320,scrolling=False)
        if gps_active: st.markdown('<div class="sensor-active-badge">🟢 GPS・コンパス取得中</div>',unsafe_allow_html=True)
        else: st.markdown('<div class="sensor-manual-badge">🎛 手動シミュレータ</div>',unsafe_allow_html=True)
        st.markdown("---")

        with st.expander("⚙️ 表示設定（タップで開く）",expanded=False):
            night_mode=st.toggle("🌙 夜モード",value=st.session_state.night_mode); st.session_state.night_mode=night_mode
            st.markdown("---")
            selected_area = st.session_state.get("selected_area", "播磨エリア")
            # ★ 登録済みスポット数を表示
            SHISO_CITIES = ("宍粟市一宮町", "宍粟市山崎町", "宍粟市波賀町", "宍粟市千種町")
            harima_spots = [s for s in SPOT_DATA_BUILTIN if s.get("prefecture")=="兵庫県" and s.get("city") not in ("淡路市",) and s.get("city") not in SHISO_CITIES]
            shiso_spots  = [s for s in SPOT_DATA_BUILTIN if s.get("city") in SHISO_CITIES]
            kansai_spots = [s for s in SPOT_DATA_BUILTIN if s.get("prefecture") in ("奈良県","京都府","大阪府")]
            kagawa_spots = [s for s in SPOT_DATA_BUILTIN if s.get("prefecture")=="香川県"]
            awaji_spots  = [s for s in SPOT_DATA_BUILTIN if s.get("city")=="淡路市"]
            total = len(SPOT_DATA_BUILTIN)
            st.markdown(
                f'''<div style="background:rgba(100,160,255,0.15);border-radius:10px;padding:8px 12px;
                font-size:12px;color:#2a4a7a;margin-bottom:8px;line-height:1.8;">
                📊 <b>登録済みスポット：{total}件</b><br>
                　播磨：{len(harima_spots)}件 ／ 宍粟：{len(shiso_spots)}件 ／ 関西：{len(kansai_spots)}件<br>
                　淡路：{len(awaji_spots)}件 ／ 香川：{len(kagawa_spots)}件
                </div>''',
                unsafe_allow_html=True
            )

            # ★ エリア別カテゴリ表示
            st.markdown("**📍 エリアから探す**")

            def _spot_button(sp, key_prefix):
                is_selected = st.session_state.get("selected_spot_id") == sp["id"]
                btn_label = f"✅ {sp['name']}" if is_selected else sp["name"]
                if st.button(btn_label, use_container_width=True, key=f"{key_prefix}_{sp['id']}"):
                    st.session_state.preset_lat = sp["lat"]
                    st.session_state.preset_lon = sp["lon"]
                    st.session_state.selected_spot_id = sp["id"]
                    st.session_state.osm_loaded = False
                    st.session_state.osm_spots = []
                    st.session_state.osm_center_lat = sp["lat"]
                    st.session_state.osm_center_lon = sp["lon"]
                    st.rerun()

            # 播磨エリア（宍粟を除く）
            with st.expander("🗾 播磨エリア（兵庫県）", expanded=False):
                categories = {
                    "⛩ 神社": [s for s in harima_spots if s.get("category")=="shrine"],
                    "🛕 寺院": [s for s in harima_spots if s.get("category")=="temple"],
                    "🏯 城・史跡": [s for s in harima_spots if s.get("category") in ("castle","historical")],
                    "🏔 山・自然": [s for s in harima_spots if s.get("category")=="mountain"],
                }
                for cat_label, spots in categories.items():
                    if not spots: continue
                    st.markdown(f'<div style="font-size:12px;color:#3a5a8a;font-weight:700;margin:4px 0 2px;">{cat_label}</div>', unsafe_allow_html=True)
                    for sp in spots:
                        _spot_button(sp, f"sp_h_{cat_label[:2]}")

            # ★ 宍粟市エリア（独立）
            with st.expander("🗾 宍粟市エリア（兵庫県）", expanded=False):
                shiso_cats = {
                    "⛩ 神社": [s for s in shiso_spots if s.get("category")=="shrine"],
                    "🛕 寺院": [s for s in shiso_spots if s.get("category")=="temple"],
                    "🏔 山・自然": [s for s in shiso_spots if s.get("category")=="mountain"],
                }
                for cat_label, spots in shiso_cats.items():
                    if not spots: continue
                    st.markdown(f'<div style="font-size:12px;color:#3a5a8a;font-weight:700;margin:4px 0 2px;">{cat_label}</div>', unsafe_allow_html=True)
                    for sp in spots:
                        _spot_button(sp, f"sp_s_{cat_label[:2]}")

            # 関西エリア
            with st.expander("🗾 関西エリア（奈良・京都・大阪）", expanded=False):
                kansai_prefs = {"奈良県":"🦌 奈良", "京都府":"⛩ 京都", "大阪府":"🏯 大阪"}
                for pref, pref_label in kansai_prefs.items():
                    pref_spots = [s for s in SPOT_DATA_BUILTIN if s.get("prefecture")==pref]
                    if not pref_spots: continue
                    st.markdown(f'<div style="font-size:12px;color:#3a5a8a;font-weight:700;margin:4px 0 2px;">{pref_label}</div>', unsafe_allow_html=True)
                    for sp in pref_spots:
                        _spot_button(sp, f"sp_k_{pref_label[:2]}")

            # 淡路島エリア
            with st.expander("🗾 淡路島エリア", expanded=False):
                for sp in awaji_spots:
                    _spot_button(sp, "sp_a")

            # 香川エリア
            with st.expander("🗾 香川エリア", expanded=False):
                for sp in kagawa_spots:
                    _spot_button(sp, "sp_g")
            st.markdown("---")
            if gps_active:
                st.markdown('<div class="gps-auto-note">🟢 <b>GPS自動取得中です</b><br>現在地・向きはスマホのセンサーから自動で入力されています。</div>',unsafe_allow_html=True)
                sim_lat=gps_lat; sim_lon=gps_lon; sim_heading=gps_heading
                # GPS取得中もプリセット選択を維持する（リセットしない）
            else:
                st.markdown('<div style="font-size:12px;color:#3a5a8a;background:rgba(200,220,255,0.3);border-radius:8px;padding:6px 10px;margin-bottom:6px;">💻 パソコン・GPS未取得時はスライダーで場所を模擬できます</div>',unsafe_allow_html=True)
                sim_lat=st.slider("📍 緯度",33.50,35.70,
                    float(max(33.50,min(35.70,st.session_state.preset_lat))),
                    0.0005,format="%.4f")
                sim_lon=st.slider("📍 経度",133.00,136.00,
                    float(max(133.00,min(136.00,st.session_state.preset_lon))),
                    0.0005,format="%.4f")
                sim_heading=st.slider("🧭 向き（方位角）",0,359,45,1)
            st.markdown("---")
            st.markdown("**🌐 表示言語**")
            lang_ja = st.toggle("🇺🇸 英語表示", value=False)
            selected_lang = "EN" if lang_ja else "ja"
            st.markdown("---")
            st.markdown("**🗺️ 地図タイル**")
            tile_opts=["標準地図","写真（空中写真）","淡色地図","陰影起伏図","OpenStreetMap"]
            tile_name=st.selectbox("タイル",tile_opts,index=0,label_visibility="collapsed")
            map_zoom=st.slider("🔍 ズーム",10,17,st.session_state.map_zoom,1); st.session_state.map_zoom=map_zoom
            st.markdown("---")
            st.markdown('<div style="color:#3a5a8a;font-size:13px;margin-bottom:4px;">📡 表示モード</div>',unsafe_allow_html=True)
            mode_label=st.radio("モード",list(MODES.keys()),index=0,label_visibility="collapsed")
            mode_cfg=MODES[mode_label]
            # モード切替時に地図選択をリセット
            if "prev_mode" not in st.session_state:
                st.session_state.prev_mode = mode_label
            elif st.session_state.prev_mode != mode_label:
                st.session_state.map_selected_spot_id = None
                st.session_state.prev_mode = mode_label
            st.markdown("---")
            show_detail=st.toggle("🔍 詳細情報を表示",value=False)
            st.markdown("---")
            use_osm=st.toggle("🌐 周辺スポット自動取得（OSM）",value=False)

        all_spots=list(SPOT_DATA_BUILTIN)
        if use_osm:
            osm_lat=st.session_state.osm_center_lat or sim_lat; osm_lon=st.session_state.osm_center_lon or sim_lon
            if not st.session_state.osm_loaded:
                with st.spinner("OpenStreetMapから周辺スポットを取得中..."):
                    osm=fetch_overpass_spots(osm_lat,osm_lon,3000); st.session_state.osm_spots=osm; st.session_state.osm_loaded=True
            if st.session_state.osm_spots:
                all_spots=all_spots+st.session_state.osm_spots
                st.markdown(f'<div style="font-size:11px;color:#2a7a3a;">🌐 OSM: {len(st.session_state.osm_spots)}件追加<br>© OpenStreetMap contributors</div>',unsafe_allow_html=True)
        else:
            st.session_state.osm_loaded=False

        selected_id=st.session_state.get("selected_spot_id")
        if selected_id:
            # プリセット選択中：選択スポットのみ表示
            selected_spot=next((sp for sp in all_spots if sp.get("id")==selected_id),None)
            if selected_spot:
                dist=haversine_km(sim_lat,sim_lon,selected_spot["lat"],selected_spot["lon"])
                brg=bearing_deg(sim_lat,sim_lon,selected_spot["lat"],selected_spot["lon"])
                visible_spots=[(selected_spot,dist,brg)]
                # 周辺スポットONの場合はそのスポット周辺のOSMスポットを追加
                if use_osm and st.session_state.osm_spots:
                    osm_near=filter_spots(st.session_state.osm_spots,selected_spot["lat"],selected_spot["lon"])
                    visible_spots=visible_spots+osm_near
            else:
                visible_spots=filter_spots(all_spots,sim_lat,sim_lon)
        else:
            # プリセット未選択：GPS現在地から近い順に表示
            visible_spots=filter_spots(all_spots,sim_lat,sim_lon)
            if use_osm and st.session_state.osm_spots:
                visible_spots=visible_spots+filter_spots(st.session_state.osm_spots,sim_lat,sim_lon)

        nearest=visible_spots[0] if visible_spots else None
        sensor_badge='<span class="sensor-active-badge">🟢 GPS</span>' if gps_active else '<span class="sensor-manual-badge">🎛 手動</span>'
        if nearest:
            sp0,d0,_=nearest
            st.markdown(f'<div class="info-panel">{sensor_badge}<br>📍 {sim_lat:.4f}, {sim_lon:.4f}<br>🧭 {sim_heading:.0f}°（{deg_to_dir(sim_heading)}）<br>📡 {len(visible_spots)}件<br>📏 最寄り：{sp0["name"]} {dist_label(d0)}</div>',unsafe_allow_html=True)
        else:
            st.markdown(f'<div class="info-panel">{sensor_badge}<br>📍 {sim_lat:.4f}, {sim_lon:.4f}<br>📡 スポットなし</div>',unsafe_allow_html=True)

    with col_main:
        st.markdown(f'<div class="mode-title-bar" style="background:{mode_cfg["bg"]};">{mode_cfg["icon"]} {mode_label}</div>',unsafe_allow_html=True)
        map_key=f"kanko_map_{tile_name[:2]}_{map_zoom}_{round(map_center_lat,3)}_{round(map_center_lon,3)}_{st.session_state.get('selected_spot_id','none')}"
        map_data={}; map_ok=False
        try:
            # スポット選択時はそのスポットを、GPS ON・未選択時は現在地を、それ以外はデフォルトをマップ中心にする
            selected_id_for_map = st.session_state.get("selected_spot_id")
            selected_spot_for_map = next((sp for sp in all_spots if sp.get("id")==selected_id_for_map), None) if selected_id_for_map else None
            if selected_spot_for_map:
                map_center_lat = selected_spot_for_map["lat"]
                map_center_lon = selected_spot_for_map["lon"]
            elif gps_active:
                map_center_lat = gps_lat
                map_center_lon = gps_lon
            else:
                map_center_lat = sim_lat
                map_center_lon = sim_lon
            fmap=build_map(map_center_lat,map_center_lon,sim_heading,tile_name,map_zoom,mode_cfg,visible_spots)
            map_data=st_folium(fmap,width="100%",height=380,returned_objects=["last_clicked"],key=map_key); map_ok=True
        except Exception: pass
        # 地図ピンタップでスポット特定
        if map_data and map_data.get("last_clicked"):
            c = map_data["last_clicked"]
            click_lat = c.get("lat", 0)
            click_lng = c.get("lng", 0)
            nearest_spot = None
            nearest_dist = 999
            for sp, dist_km, brg in visible_spots:
                sp_dist = haversine_km(click_lat, click_lng, sp["lat"], sp["lon"])
                if sp_dist < 0.2 and sp_dist < nearest_dist:
                    nearest_dist = sp_dist
                    nearest_spot = sp
            if nearest_spot:
                if st.session_state.get("map_selected_spot_id") != nearest_spot["id"]:
                    st.session_state.map_selected_spot_id = nearest_spot["id"]
                    st.session_state.selected_spot_id = nearest_spot["id"]
                    st.session_state.preset_lat = nearest_spot["lat"]
                    st.session_state.preset_lon = nearest_spot["lon"]
                    st.rerun()

        if not map_ok:
            st.markdown('<div class="map-placeholder">🗺️ 地図の読み込みに失敗しました。F5で再読み込みしてください。</div>',unsafe_allow_html=True)

        # 地図タップで選択されたスポットの案内を表示
        map_selected_id = st.session_state.get("map_selected_spot_id")
        if map_selected_id:
            map_selected_spot = next((sp for sp in SPOT_DATA_BUILTIN if sp.get("id") == map_selected_id), None)
            if map_selected_spot:
                st.markdown(
                    f'''<div style="background:rgba(255,255,255,0.70);border-radius:14px;
                    padding:12px 16px;margin:6px 0;border:2px solid rgba(100,150,220,0.55);
                    backdrop-filter:blur(10px);">
                    <div style="font-size:13px;color:#2a4a7a;font-weight:700;margin-bottom:4px;">
                    📍 地図で選択中：{map_selected_spot["name"]}
                    </div>
                    <div style="font-size:12px;color:#4a6a9a;">
                    タップして案内を見るには下の案内カードをご覧ください
                    </div>
                    </div>''',
                    unsafe_allow_html=True
                )
                # 選択スポットをプリセットにも反映
                if st.session_state.get("selected_spot_id") != map_selected_id:
                    st.session_state.selected_spot_id = map_selected_id
                    st.session_state.preset_lat = map_selected_spot["lat"]
                    st.session_state.preset_lon = map_selected_spot["lon"]
                col_clear1, col_clear2 = st.columns([3,1])
                with col_clear2:
                    if st.button("✕ 選択解除", key=f"clear_map_{map_selected_id}"):
                        st.session_state.map_selected_spot_id = None
                        st.session_state.selected_spot_id = None
                        st.rerun()

        sensor_lbl="🟢 GPS・コンパス取得中" if gps_active else "🎛 手動シミュレータ"

        # 簡易ARビューは後実装で対応

        if mode_cfg["key"]=="omikuji":
            spot_name=visible_spots[0][0]["name"] if visible_spots else "播磨エリア"
            render_omikuji(spot_name); st.markdown("---")

        if not visible_spots:
            st.markdown('<div class="ar-card font-noto-sans" style="background:rgba(100,150,220,0.55);text-align:center;">📭 この範囲にスポットがありません</div>',unsafe_allow_html=True)
        else:
            for sp,dist_km,brg in visible_spots:
                render_spot_card(sp,mode_cfg,dist_km,brg,show_detail,selected_lang)
                if sp.get("location_limited") and dist_km<0.3:
                    st.markdown(f'<div class="location-limited-card">🌟 <strong style="color:#2a4a8a;">現地限定コンテンツ解放！</strong><br><span style="font-size:16px;color:#2a4060;">{sp["location_limited_content"]}</span></div>',unsafe_allow_html=True)

                # ★ 現地限定写真アップロード（GPS確認済み・半径100m以内のみ）
                is_osm = sp.get("id","").startswith("osm_")
                if gps_active and dist_km < 0.1 and not is_osm:
                    st.markdown(
                        f'''<div style="background:rgba(255,240,200,0.60);border-radius:14px;
                        padding:14px 18px;margin:8px 0;
                        border:1px solid rgba(200,160,50,0.55);">
                        <div style="font-size:16px;font-weight:700;color:#3a2000;margin-bottom:6px;">
                        【現地限定】📸 あなたの写真を追加</div>
                        <div style="font-size:13px;color:#5a3a00;margin-bottom:8px;">
                        ✅ GPS確認済み：<b>{sp["name"]}</b>から<b>{int(dist_km*1000)}m</b>以内
                        </div>
                        </div>''',
                        unsafe_allow_html=True
                    )
                    st.markdown(
                        '<div style="font-size:12px;color:#5a3a00;margin-bottom:4px;">📁 ファイルから写真を選んでください（カメラ撮影はファイル保存後にご利用ください）</div>',
                        unsafe_allow_html=True
                    )
                    user_photo = st.file_uploader(
                        "写真を選ぶ",
                        type=["jpg","jpeg","png"],
                        key=f"photo_{sp['id']}_{mode_cfg['key']}",
                        label_visibility="collapsed",
                        accept_multiple_files=False,
                    )
                    if user_photo:
                        with st.spinner("写真を確認中..."):
                            # Geminiで不適切写真チェック
                            api_key = get_secret("GEMINI_API_KEY")
                            is_safe = True
                            check_msg = ""
                            if api_key:
                                try:
                                    import base64
                                    img_b64 = base64.b64encode(user_photo.read()).decode()
                                    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
                                    payload = {"contents":[{"parts":[
                                        {"text": f'この画像は観光地「{sp["name"]}」で撮影された観光写真として適切ですか？不適切なコンテンツ（暴力・性的・個人情報など）が含まれていますか？「適切」または「不適切：理由」のみで答えてください。'},
                                        {"inline_data": {"mime_type": "image/jpeg", "data": img_b64}}
                                    ]}]}
                                    r = requests.post(url, json=payload, timeout=15)
                                    if r.status_code == 200:
                                        result_text = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                                        if "不適切" in result_text:
                                            is_safe = False
                                            check_msg = result_text
                                    user_photo.seek(0)
                                except Exception:
                                    user_photo.seek(0)

                        if is_safe:
                            st.image(user_photo, caption=f"📍 {sp['name']}で撮影", use_column_width=True)
                            st.success("✅ 写真を表示しました！ページを閉じると写真は消えます。")
                            st.info("📮 この写真を永久保存したい場合は下の「問題を報告する」から「📸 写真を提供する」を選んで送信してください。")
                        else:
                            st.error(f"⛔ この写真は投稿できません。{check_msg}")

                    st.markdown(
                        '''<div style="font-size:11px;color:#7a5a20;margin-top:4px;">
                        ※ 現地で撮影した写真のみ投稿できます<br>
                        ※ ページを閉じると写真は消えます<br>
                        ※ 不適切な写真は自動でブロックされます
                        </div>''',
                        unsafe_allow_html=True
                    )

        if mode_cfg["key"]=="cloud":
            st.markdown("---")
            today_count=cloud_usage_today(); remaining=3-today_count
            st.markdown(f'<div class="cloud-result">☁️ <b>雲判定モード</b>　本日残り：{remaining}/3回<br><span style="font-size:14px;">空の写真をアップロードすると雲の種類を判定します。</span><br><span style="font-size:12px;opacity:0.8;">⚠️ 雲の分析はAIによるものです。正確な天気予報は気象庁等でご確認ください。</span></div>',unsafe_allow_html=True)
            st.markdown(
                '<div style="font-size:12px;color:#5a3a00;background:rgba(255,240,200,0.6);border-radius:8px;padding:6px 10px;margin-bottom:6px;">📱 事前にスマホのカメラで空を撮影し、カメラロールに保存してからファイルを選択してください</div>',
                unsafe_allow_html=True
            )
            uploaded=st.file_uploader("☁️ 空の写真をアップロード",type=["jpg","jpeg","png"],label_visibility="collapsed",accept_multiple_files=False)
            if uploaded and remaining>0:
                with st.spinner("雲を分析中..."):
                    result=analyze_cloud_gemini(uploaded.read())
                if result.get("is_dummy"):
                    st.markdown(f'<div class="cloud-result">📡 {result["description"]}</div>',unsafe_allow_html=True)
                else:
                    st.markdown(f'<div class="cloud-result">☁️ <b>{result.get("cloud_type","不明")}</b><br><br>{result.get("description","")}<br><br>🌱 <b>この雲が生まれた理由</b><br>{result.get("formation_reason","")}<br><br>🌤 <b>空からのメッセージ</b><br>{result.get("weather_hint","")}<br><br>🔭 <b>空を味わうヒント</b><br>{result.get("observation_tips","")}<br><br><span style="font-size:12px;opacity:0.8;">⚠️ 雲の分析はAIによるものです。正確な天気予報は気象庁等でご確認ください。</span></div>',unsafe_allow_html=True)
            elif uploaded and remaining<=0:
                st.warning("本日の雲判定上限（3回）に達しました。明日またお試しください。")

        if visible_spots:
            st.markdown("---")
            # 選択中スポットまたは最寄りスポットでシェア
            selected_id_for_share = st.session_state.get("selected_spot_id")
            if selected_id_for_share:
                sp_share_list = [(sp,d,b) for sp,d,b in visible_spots if sp.get("id")==selected_id_for_share]
                sp_share, d_share, _ = sp_share_list[0] if sp_share_list else visible_spots[0]
            else:
                sp_share, d_share, _ = visible_spots[0]
            share_text=make_share_text(sp_share,mode_cfg,d_share)
            st.markdown('<div class="share-card"><b>📤 SNSシェア</b><br><span style="font-size:12px;color:#4a6a9a;">ボタンをタップして投稿できます。投稿前に内容を編集することもできます。</span></div>',unsafe_allow_html=True)
            st.text_area("シェアテキスト",value=share_text,height=180,label_visibility="collapsed")
            import urllib.parse
            encoded = urllib.parse.quote(share_text)
            x_url = f"https://twitter.com/intent/tweet?text={encoded}"
            fb_url = f"https://www.facebook.com/sharer/sharer.php?u=https%3A%2F%2Fkanko-ar-harima.streamlit.app&quote={encoded}"
            line_url = f"https://line.me/R/share?text={encoded}"
            # JavaScriptでwindow.openを使って外部ウィンドウで開く
            st.markdown(
                f'''<div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:8px;">
                <a href="{x_url}" target="_blank" rel="noopener noreferrer"
                onclick="window.open('{x_url}','_blank','width=600,height=400');return false;"
                style="display:inline-block;background:#000000;color:#FFF;text-decoration:none;
                padding:10px 18px;border-radius:10px;font-weight:700;font-size:14px;cursor:pointer;">
                𝕏 Xに投稿</a>
                <a href="{fb_url}" target="_blank" rel="noopener noreferrer"
                onclick="window.open('{fb_url}','_blank','width=600,height=400');return false;"
                style="display:inline-block;background:#1877F2;color:#FFF;text-decoration:none;
                padding:10px 18px;border-radius:10px;font-weight:700;font-size:14px;cursor:pointer;">
                📘 Facebookに投稿</a>
                <a href="{line_url}" target="_blank" rel="noopener noreferrer"
                onclick="window.open('{line_url}','_blank','width=600,height=400');return false;"
                style="display:inline-block;background:#06C755;color:#FFF;text-decoration:none;
                padding:10px 18px;border-radius:10px;font-weight:700;font-size:14px;cursor:pointer;">
                💬 LINEで送る</a>
                </div>
                <div style="font-size:11px;color:#4a6a9a;margin-top:6px;">
                ※ ボタンをタップするとSNSアプリが開きます
                </div>''',
                unsafe_allow_html=True
            )

        st.markdown("---")
        with st.expander("⚠️ 問題を報告する・写真を提供する"):
            st.markdown('<div class="report-form"><b>📝 問題報告・写真提供フォーム</b><br><span style="font-size:13px;">情報の誤りをご報告ください。写真を提供される場合はメール送信時に添付してください。</span></div>',unsafe_allow_html=True)
            report_spot=st.text_input("スポット名（任意）",placeholder="例：高御位神社")
            report_type=st.selectbox("種類",["情報が間違っている","地図の位置がずれている","表示が崩れている","📸 写真を提供する","その他"])
            report_detail=st.text_area("詳細を教えてください",height=80,placeholder="詳細をお書きください（任意）")
            report_photo = None
            report_nickname = ""
            if report_type == "📸 写真を提供する":
                st.markdown(
                    '''<div style="background:rgba(255,240,200,0.60);border-radius:10px;
                    padding:10px 14px;margin-bottom:8px;font-size:13px;color:#5a3a00;line-height:1.8;">
                    📸 <b>写真の送り方</b><br>
                    ① ニックネームを入力してください（アプリ内の写真クレジットに表示されます）<br>
                    ② 下の「メールアプリで送信する」をタップ<br>
                    ③ メールアプリが開いたら📎アイコンをタップ<br>
                    ④ カメラロールから写真を選んで添付して送信！<br>
                    <span style="font-size:11px;color:#7a5a20;">
                    ※ ご提供いただいた写真は、管理者が確認後、アプリの観光地案内に掲載させていただく場合があります。<br>
                    ※ みなさんの写真でアプリを一緒に作り上げましょう！
                    </span>
                    </div>''',
                    unsafe_allow_html=True
                )
                report_nickname = st.text_input(
                    "📛 ニックネーム（任意）",
                    placeholder="例：やまさん、Kさん、匿名希望 など",
                    help="アプリ内の写真クレジット（「提供：〇〇様」）に表示されます。空欄の場合は「匿名」となります。"
                )
                st.markdown(
                    f'''<div style="background:rgba(200,230,255,0.50);border-radius:8px;
                    padding:8px 12px;font-size:12px;color:#2a4a7a;margin-bottom:6px;">
                    📷 アプリ内表示例：<b>提供：{report_nickname or "匿名"}様</b>
                    </div>''',
                    unsafe_allow_html=True
                )
                report_photo = st.file_uploader("写真プレビュー（任意）",type=["jpg","jpeg","png"],key="report_photo",label_visibility="collapsed")
                if report_photo:
                    st.image(report_photo, caption="プレビュー（メールには自動添付されません）", use_column_width=True)
                    st.caption("※ メールアプリで写真を手動添付してください。")
            # メールボタンを常に表示（入力内容をリアルタイムで反映）
            subject = urllib.parse.quote(f"【観光スポットナビ報告】{report_type}")
            nickname_line = f"ニックネーム：{report_nickname or '匿名'}\n" if report_type == "📸 写真を提供する" else ""
            body = urllib.parse.quote(
                f"種類：{report_type}\n"
                f"{nickname_line}"
                f"スポット名：{report_spot or '未記入'}\n"
                f"詳細：{report_detail or '未記入'}\n"
                f"\n※ 観光スポットナビアプリからの報告"
            )
            mailto = f"mailto:landscaping.yama@gmail.com?subject={subject}&body={body}"
            st.markdown(
                f'''<div style="margin-top:8px;">
                <a href="{mailto}"
                style="display:inline-block;background:linear-gradient(135deg,#4888e0,#2060c0);
                color:#FFF;text-decoration:none;padding:12px 24px;border-radius:10px;
                font-weight:700;font-size:15px;box-shadow:0 2px 8px rgba(70,130,220,0.35);">
                📧 メールアプリで送信する
                </a>
                <div style="font-size:12px;color:#4a6a9a;margin-top:8px;">
                ※ タップするとメールアプリが開きます。内容を確認して送信してください。
                </div>
                </div>''',
                unsafe_allow_html=True
            )

    if st.session_state.get("night_mode",False):
        st.markdown("""<style>.stApp{background:linear-gradient(160deg,#050510 0%,#0a0a28 40%,#080818 100%)!important;}.app-header h1{color:#8888FF!important;}.info-panel,.ar-compass,.lookaround-card,.share-card,.report-form{background:rgba(20,20,60,0.65)!important;color:#CCCCFF!important;}.ar-card{color:#EEEEFF!important;}.ar-card-title{color:#FFFFFF!important;}.mode-title-bar{color:#FFFFFF!important;}</style>""",unsafe_allow_html=True)

    now=datetime.now().strftime("%Y年%m月%d日 %H:%M")
    st.markdown(f'<div class="app-footer">観光スポットナビ ／ 播磨・関西・香川エリア<br>Wikipedia API：CC BY-SA ／ 地図：国土地理院・OpenStreetMap contributors<br>最終更新：{now}</div>',unsafe_allow_html=True)

if __name__=="__main__":
    main()
