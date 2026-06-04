# ============================================================
# kanko_app.py  観光AR案内アプリ フェーズ7（UI改善版）
# 播磨・関西エリア + Folium地図 + GPS/コンパス + API連携
# + 夜モード + 関西拡張 + 簡易ARビュー + UI改善
# 開発指示書 v18 準拠
# ============================================================
# フェーズ7 UI改善内容:
#   ① 案内文を4行に拡張（切れない）
#   ② 🎵癒し音声 → 📸撮影スポットに変更
#   ③ スマホ時GPS自動取得の説明文表示
#   ④ 各カードに写真を追加（Wikimedia Commons CC BY-SA）
#   ⑥ スマホ時の設定をアコーディオンUIに変更
#   ⑦ 赤字エラーはconfig.tomlで非表示（別ファイル）
# 権利注記:
#   - 写真: Wikimedia Commons / CC BY-SA（出典表記必須）
#   - Wikipedia API: CC BY-SA（出典表記必須）
#   - OpenStreetMap: © OpenStreetMap contributors
#   - Google Fonts: SIL Open Font License
#   - 地図タイル: 国土地理院 / OpenStreetMap
#   - AI生成コンテンツには必ず免責表記を付与
# ============================================================

import streamlit as st
import math, json, sqlite3, hashlib, os, time
import requests
import folium
from folium.plugins import MiniMap
from streamlit_folium import st_folium
from datetime import datetime, date
from collections import deque

# ============================================================
# ■ ページ設定
# ============================================================
st.set_page_config(
    page_title="観光AR案内 | 播磨・関西エリア",
    page_icon="⛩",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ============================================================
# ■ SQLite キャッシュ
# ============================================================
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kanko_cache.db")

def init_db():
    try:
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
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

# ============================================================
# ■ APIキー取得
# ============================================================
def get_secret(key):
    try:
        if key == "GEMINI_API_KEY":
            k1 = st.secrets.get("GEMINI_API_KEY_1", "")
            k2 = st.secrets.get("GEMINI_API_KEY_2", "")
            if k1 and not k2:
                return k1
            if k1 and k2:
                return k1 + k2
        val = st.secrets.get(key, "")
        if val: return val
    except Exception: pass
    return os.environ.get(key, "")

# ============================================================
# ■ Wikipedia API
# ============================================================
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

# ============================================================
# ■ Overpass API
# ============================================================
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

# ============================================================
# ■ DeepL API
# ============================================================
def translate_deepl(text, target_lang="EN"):
    api_key=get_secret("DEEPL_API_KEY")
    if not api_key or not text: return text
    cache_key=hashlib.md5(f"{text[:50]}_{target_lang}".encode()).hexdigest()
    cached=cache_get(0.0,0.0,f"translate_{cache_key}",target_lang)
    if cached: return cached
    try:
        r=requests.post("https://api-free.deepl.com/v2/translate",timeout=10,data={"auth_key":api_key,"text":text,"target_lang":target_lang})
        if r.status_code!=200: return text
        result=r.json()["translations"][0]["text"]
        cache_set(0.0,0.0,f"translate_{cache_key}",result,target_lang); return result
    except Exception: return text

# ============================================================
# ■ Gemini 雲判定
# ============================================================
def analyze_cloud_gemini(image_bytes):
    api_key=get_secret("GEMINI_API_KEY")
    dummy={"cloud_type":"判定できませんでした","description":"APIキーを設定するか画像を再アップロードしてください。","weather_hint":"天気の予報は気象庁等でご確認ください。","is_dummy":True}
    if not api_key:
        dummy["description"]="Gemini APIキー未設定。st.secretsにGEMINI_API_KEYを設定してください。（サンプルデータ）"; return dummy
    if cloud_usage_today()>=3:
        dummy["description"]="本日の雲判定上限（3回）に達しました。明日またお試しください。"; dummy["is_dummy"]=False; return dummy
    try:
        import base64; img_b64=base64.b64encode(image_bytes).decode()
        url=f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
        payload={"contents":[{"parts":[{"text":'この画像の雲を分析してください。JSONのみで回答:{"cloud_type":"雲の種類","description":"特徴を2行以内","weather_hint":"天気の傾向を1行で"}'},{"inline_data":{"mime_type":"image/jpeg","data":img_b64}}]}]}
        r=requests.post(url,json=payload,timeout=15)
        if r.status_code!=200: return dummy
        text=r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        if "{" in text and "}" in text:
            result=json.loads(text[text.index("{"):text.rindex("}")+1]); result["is_dummy"]=False; cloud_usage_increment(); return result
        return dummy
    except Exception: return dummy

# ============================================================
# ■ SNSシェアテキスト
# ============================================================
def make_share_text(spot, mode_cfg, dist_km):
    cat_map={"shrine":"⛩","mountain":"🏔","castle":"🏯","temple":"🛕","default":"📍"}
    icon=cat_map.get(spot.get("category","default"),"📍")
    mode_name={"main":"観光案内","urban_legend":"都市伝説","powerspot":"パワースポット","healing":"撮影スポット","festival":"行事案内","old_map":"古地図","cloud":"雲判定"}.get(mode_cfg["key"],"AR案内")
    return (f"{icon} {spot['name']}を訪れました！\n📍 {spot['prefecture']} {spot['city']}\n🏔 標高{spot['altitude']}m\n📱 {mode_name}モードで探索中\n\n#播磨AR #観光アプリ #{spot['name'].replace(' ','')} #{spot['city'].replace(' ','')}")

# ============================================================
# ■ スポットデータ（写真URL付き）
# ============================================================
SPOT_DATA_BUILTIN = [
    {
        "id": "takamikura_001", "name": "高御位神社", "name_kana": "たかみくらじんじゃ",
        "category": "shrine", "priority": 1, "wiki_title": "高御位山",
        "lat": 34.8418, "lon": 134.8682, "altitude": 304,
        "prefecture": "兵庫県", "city": "加古川市",
        # 写真: Wikimedia Commons / CC BY-SA
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
        # ② 撮影スポットに変更
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
        "old_map_detail": "📜 播磨国絵図（江戸時代）に「高御位山」として記載。\n🗺 明治以降の地形図と比較すると参拝道の変遷が読み取れます。\n🖼 古地図・古写真の表示機能はフェーズ8で実装予定です。",
        "cloud_info": "播磨平野を一望できる山頂からの雲の観察に最適な場所です。",
        "cloud_detail": "☁️ 積乱雲：夏の午後に南西から発達。雷雨の前兆。\n🌤 層積雲：朝のうちに漂う雲。晴天のサイン。\n🌫 高層雲：薄いベール状。翌日の雨の予兆。\n\n⚠️ 正確な天気予報は気象庁等でご確認ください。",
        "trust_score": 1.0, "approved": True,
        "location_limited": True,
        "location_limited_content": "山頂限定：磐座のご神気を感じる特別なパワースポット情報が解放されました。",
    },
    {
        "id": "kasagatayama_002", "name": "笠形山", "name_kana": "かさがたやま",
        "category": "mountain", "priority": 2, "wiki_title": "笠形山",
        "lat": 35.0044, "lon": 134.7783, "altitude": 939,
        "prefecture": "兵庫県", "city": "神崎郡神河町",
        "description": "播磨の名峰・播磨富士。山頂からは播磨平野・淡路島・四国まで望める絶景スポット。標高939m。",
        "main_detail": "🏔 標高939m　播磨富士とも称される美しい山容\n\n👁 晴れた日は播磨平野・淡路島・四国山地まで一望。\n\n🌺 笠形神社\n　　山頂直下に鎮座。縁結び・五穀豊穣のご神徳。\n\n🍂 紅葉の名所\n　　10〜11月の紅葉は播磨随一の美しさです。\n\n❄️ 冬の霧氷\n　　寒い朝は木々に霧氷が咲き、幻想的な世界に。",
        "urban_legend": "「播磨富士」と称される美しい山容。古来より雨乞いの山として信仰を集めてきた。",
        "urban_legend_detail": "干ばつの年には村人が笠形山山頂で雨乞いの祈りを捧げたという記録が残ります。山頂の池は決して涸れないと伝えられています。\n\n⚠️ AIエンターテイメント情報です。",
        "powerspot": "山頂の笠形神社はパワースポットとして知られ、縁結び・五穀豊穣のご神徳があるとされます。",
        "powerspot_detail": "山頂直下に鎮座する笠形神社は農業の神・大己貴命を祀ります。360度パノラマの清浄な空気は心を清めます。\n\n⚠️ AIエンターテイメント情報です。",
        "festival": "春：笠形神社例大祭（4月）・秋の紅葉シーズン",
        "festival_detail": "【4月】笠形神社春の例大祭\n【10〜11月】紅葉の見頃・ハイキングイベント\n※詳細は神河町観光協会でご確認ください。",
        "healing_text": "📸 撮影スポット情報",
        "healing_detail": "📍 おすすめ撮影ポイント\n\n🌅 山頂からの雲海（秋〜冬の早朝）\n　　播磨平野を覆う雲海は息をのむ絶景。日の出前後がベスト。\n\n🌸 ミツバツツジ（4〜5月）\n　　山全体がピンクに染まる季節。\n\n🍂 紅葉（10〜11月）\n　　ブナ林の黄葉が山を黄金色に染めます。\n\n❄️ 霧氷（12〜2月）\n　　氷の花が咲く幻想的な世界をぜひ。",
        "old_map_description": "江戸時代の播磨国絵図に「笠形山」として記された播磨の象徴的な名山。",
        "old_map_detail": "古くから播磨の目印として航行の目標にもなった山。江戸時代の紀行文にも登場します。",
        "cloud_info": "山頂からの雲海が有名。早朝に播磨平野を覆う雲海は幻想的な絶景です。",
        "cloud_detail": "秋〜冬の早朝に播磨平野に雲海が発生しやすくなります。\n⚠️ 天気予報は気象庁等でご確認ください。",
        "trust_score": 0.9, "approved": True, "location_limited": False, "location_limited_content": "",
    },
    {
        "id": "himeji_castle_003", "name": "姫路城", "name_kana": "ひめじじょう",
        "category": "castle", "priority": 1, "wiki_title": "姫路城",
        "lat": 34.8394, "lon": 134.6939, "altitude": 92,
        "prefecture": "兵庫県", "city": "姫路市",
        "description": "世界遺産・国宝。白漆喰の美しい姿から「白鷺城」と呼ばれる日本最大級の木造城郭。1993年UNESCO登録。",
        "main_detail": "🏯 世界遺産・国宝（1993年UNESCO登録）\n\n🕊 白鷺城の由来\n　　白漆喰の外壁が白鷺が羽を広げた姿に似ることから。\n\n⛩ 天守閣最上階の長壁神社\n　　城の守護神として何百年も祀られています。\n\n🌸 お城の桜\n　　約1,000本の桜が春を彩ります。\n\n📐 建築の見どころ\n　　渡り廊下・石落とし・狭間など防御の工夫が随所に。",
        "urban_legend": "城内には千姫・お菊の霊が宿るという伝説が語り継がれる。特に深夜の三の丸広場は要注意。",
        "urban_legend_detail": "播州皿屋敷の舞台として知られる姫路城。夜の城内では不思議な音が聞こえるという体験談が後を絶ちません。\n⚠️ AIエンターテイメント情報です。",
        "powerspot": "天守閣最上階には長壁神社が鎮座。城と共に何百年も守り続けてきた守護神の強いエネルギーを感じる場所。",
        "powerspot_detail": "何度も戦禍をくぐり抜けた城の霊験あらたかなパワースポットとされます。\n⚠️ AIエンターテイメント情報です。",
        "festival": "桜（3月下旬〜4月）・姫路お城まつり（5月）・夏の特別夜間公開",
        "festival_detail": "【3月下旬〜4月上旬】桜まつり（約1,000本）\n【5月第3日曜】姫路お城まつり（武者行列）\n【夏季】特別夜間公開（ライトアップ）\n【10月】菊花展\n※詳細は姫路市観光課でご確認ください。",
        "healing_text": "📸 撮影スポット情報",
        "healing_detail": "📍 おすすめ撮影ポイント\n\n🌸 三の丸広場（春）\n　　桜と天守閣の絶景スポット。早朝は人が少なくおすすめ。\n\n🌊 姫路城好古園の池\n　　池に映る逆さ姫路城が幻想的。\n\n🌅 城見台公園（夕景）\n　　夕日に染まる姫路城が絶景。\n\n✨ ライトアップ（夏〜秋）\n　　白亜の天守が夜空に浮かぶ幻想的な光景。",
        "old_map_description": "江戸時代初期（1609年完成）の天守が現存する奇跡の城。",
        "old_map_detail": "慶長14年（1609年）に現在の天守が完成。1993年世界遺産登録。江戸時代の絵図にも詳細に記された播磨の象徴。",
        "cloud_info": "姫路城天守閣（標高92m）からの眺望は絶品。東に高御位山、北に笠形山が見渡せます。",
        "cloud_detail": "東：高御位山（約17km）\n北：笠形山（約25km）\n⚠️ 正確な天気予報は気象庁等でご確認ください。",
        "trust_score": 1.0, "approved": True, "location_limited": False, "location_limited_content": "",
    },
    {
        "id": "kakurinji_004", "name": "鶴林寺", "name_kana": "かくりんじ",
        "category": "temple", "priority": 2, "wiki_title": "鶴林寺_(加古川市)",
        "lat": 34.7622, "lon": 134.8394, "altitude": 10,
        "prefecture": "兵庫県", "city": "加古川市",
        "description": "播磨の法隆寺と称される古刹。聖徳太子ゆかりの寺で国宝・重要文化財を多数保有。推古天皇元年（593年）創建。",
        "main_detail": "🛕 推古天皇元年（593年）創建\n\n📿 国宝2件\n　　本堂・太子堂が国宝に指定されています。\n\n🌳 境内の大銀杏\n　　樹齢推定700年。秋の黄葉は圧巻です。\n\n🕊 聖徳太子ゆかりの地\n　　太子が創建に関わったと伝わる播磨の古刹。\n\n🖼 宝物殿\n　　平安〜鎌倉時代の仏像・絵画を収蔵。",
        "urban_legend": "聖徳太子が創建に関わったとされる古寺。境内では不思議な光を見たという参拝者の話が伝わる。",
        "urban_legend_detail": "1400年以上の歴史を持つ鶴林寺。太子の御霊が参道を行くのを見た、という言い伝えが地元に残ります。\n⚠️ AIエンターテイメント情報です。",
        "powerspot": "推古天皇元年（593年）創建と伝わる古刹のパワーは格別。国宝・太子堂のご神気は強いとされます。",
        "powerspot_detail": "1400年の祈りが積み重なった空間で深呼吸すると、特別な静けさを感じると言われます。\n⚠️ AIエンターテイメント情報です。",
        "festival": "春：花まつり（4月8日）・秋の特別公開",
        "festival_detail": "【4月8日】花まつり（釈迦の誕生日）\n【秋季】国宝特別公開（太子堂・本堂）\n【毎月第2・4日曜】写経会\n※詳細は鶴林寺までお問い合わせください。",
        "healing_text": "📸 撮影スポット情報",
        "healing_detail": "📍 おすすめ撮影ポイント\n\n🌸 桜と本堂（3月下旬〜4月）\n　　国宝の本堂と桜のコントラストが美しい。\n\n🌳 大銀杏（11月）\n　　樹齢700年の大銀杏の黄葉は圧巻。\n　　根元から見上げる構図がおすすめ。\n\n🏛 太子堂と中門\n　　朝の光が差し込む時間帯が幻想的。\n\n❄️ 冬の境内\n　　人が少なく静寂の中で撮影できます。",
        "old_map_description": "推古天皇元年（593年）創建。江戸時代には「播磨の法隆寺」として広く知られた古刹。",
        "old_map_detail": "平安時代の建築様式を今に伝える本堂（国宝）と太子堂（国宝）が残ります。",
        "cloud_info": "境内の大銀杏（樹齢推定700年）の梢から見上げる空は特別な美しさがあります。",
        "cloud_detail": "大銀杏の根元から空を見上げると、四季折々の雲の表情が楽しめます。\n⚠️ 正確な天気予報は気象庁等でご確認ください。",
        "trust_score": 0.95, "approved": True, "location_limited": False, "location_limited_content": "",
    },
    # ★ 関西エリア
    {
        "id": "nara_todaiji_005", "name": "東大寺", "name_kana": "とうだいじ",
        "category": "temple", "priority": 1, "wiki_title": "東大寺",
        "lat": 34.6888, "lon": 135.8398, "altitude": 100,
        "prefecture": "奈良県", "city": "奈良市",
        "description": "世界遺産・国宝。奈良の大仏（盧舎那仏）を本尊とする華厳宗大本山。創建は8世紀。世界最大級の木造建築。",
        "main_detail": (
            "🛕 華厳宗大本山・世界遺産（1998年UNESCO登録）\n\n"
            "🗿 奈良の大仏\n"
            "　　高さ約15m・重さ約250トンの盧舎那仏坐像。\n\n"
            "🦌 奈良公園の鹿\n"
            "　　境内周辺に約1,000頭の鹿が生息。国の天然記念物。\n\n"
            "🌸 見どころ\n"
            "　　二月堂のお水取り（3月）・正倉院展（秋）が有名。\n\n"
            "📐 大仏殿の柱\n"
            "　　大仏の鼻の穴と同じ大きさの穴が開いた柱が有名。"
        ),
        "urban_legend": "大仏殿の柱には大仏の鼻の穴と同じ大きさの穴が開いており、くぐると無病息災になるという言い伝えが残る。",
        "urban_legend_detail": "大仏殿内の柱の穴をくぐると1年間無病息災になると言われています。実際に多くの参拝者が挑戦します。\n\n⚠️ AIエンターテイメント情報です。",
        "powerspot": "奈良の大仏は宇宙の真理を体現する盧舎那仏。その巨大なパワーに包まれる体験は格別とされます。",
        "powerspot_detail": "1200年以上の祈りが積み重なった大仏殿。その空間に入ると特別な気に包まれると多くの参拝者が語ります。\n\n⚠️ AIエンターテイメント情報です。",
        "festival": "3月：お水取り（修二会）・10〜11月：正倉院展",
        "festival_detail": "【3月1〜14日】お水取り（修二会）\n　　1200年以上続く伝統行事。松明の火の粉が有名。\n【10〜11月】正倉院展\n　　奈良国立博物館で正倉院宝物を公開。\n※詳細は東大寺公式サイトでご確認ください。",
        "healing_text": "📸 撮影スポット情報",
        "healing_detail": "📍 おすすめ撮影ポイント\n\n🦌 南大門と鹿（早朝）\n　　人が少ない早朝に鹿と南大門を一緒に撮影。\n\n🌸 大仏殿と桜（3月下旬）\n　　春の東大寺は格別の美しさ。\n\n🍂 若草山の紅葉（11月）\n　　東大寺を背景に紅葉の写真が撮れます。\n\n✨ 二月堂からの夜景\n　　奈良市内を見渡す夜景スポット。",
        "old_map_description": "天平15年（743年）聖武天皇の勅願で創建。江戸時代に現在の大仏殿が再建された。",
        "old_map_detail": "743年聖武天皇の詔により建立開始。現在の大仏殿は江戸時代（1709年）に再建されたもの。世界最大級の木造建築。",
        "cloud_info": "若草山山頂（342m）からの眺望は奈良盆地を一望できる絶好の雲観察スポットです。",
        "cloud_detail": "若草山から奈良盆地を見渡すと四季折々の雲が楽しめます。\n⚠️ 正確な天気予報は気象庁等でご確認ください。",
        "trust_score": 1.0, "approved": True, "location_limited": False, "location_limited_content": "",
    },
    {
        "id": "kyoto_kinkakuji_006", "name": "金閣寺", "name_kana": "きんかくじ",
        "category": "shrine", "priority": 1, "wiki_title": "鹿苑寺",
        "lat": 35.0394, "lon": 135.7292, "altitude": 100,
        "prefecture": "京都府", "city": "京都市北区",
        "description": "世界遺産。金箔で覆われた舎利殿「金閣」が有名な臨済宗相国寺派の寺院。1397年足利義満が創建。",
        "main_detail": (
            "🏯 正式名称：鹿苑寺（ろくおんじ）\n\n"
            "✨ 金閣（舎利殿）\n"
            "　　3層の建物全体に金箔が貼られた絶景。\n"
            "　　池に映る逆さ金閣も必見。\n\n"
            "🌊 鏡湖池\n"
            "　　金閣を映す美しい池。特別史跡・特別名勝に指定。\n\n"
            "❄️ 雪の金閣\n"
            "　　冬に雪化粧した金閣は特に幻想的で人気。\n\n"
            "📜 歴史\n"
            "　　1950年に放火で全焼。現在は1955年に再建されたもの。"
        ),
        "urban_legend": "金閣寺は1950年に放火で全焼した。犯人の動機が美しすぎるものへの嫉妬だったという話は三島由紀夫の小説にもなった。",
        "urban_legend_detail": "1950年の放火事件後、現在の金閣は1955年に再建されたもの。三島由紀夫の小説「金閣寺」はこの事件を題材にしています。\n\n⚠️ これは実際の史実に基づくエピソードです。",
        "powerspot": "足利義満が建てた北山文化の象徴。金色に輝く舎利殿は見る者すべての心を浄化するパワースポット。",
        "powerspot_detail": "鏡湖池に映る金閣の姿は「浄土の世界」を表現しているとされます。その美しさに心が洗われると多くの参拝者が語ります。\n\n⚠️ AIエンターテイメント情報です。",
        "festival": "春：桜と金閣・秋：紅葉と金閣・冬：雪の金閣（不定期）",
        "festival_detail": "【3月下旬〜4月上旬】桜と金閣の絶景\n【11月下旬〜12月上旬】紅葉と金閣\n【冬季】雪化粧した金閣（天候次第）\n※詳細は鹿苑寺公式サイトでご確認ください。",
        "healing_text": "📸 撮影スポット情報",
        "healing_detail": "📍 おすすめ撮影ポイント\n\n🌊 鏡湖池からの金閣（午前中）\n　　午前中は逆光を避けられ、池に映る逆さ金閣が美しい。\n\n❄️ 雪の金閣（冬）\n　　金と白のコントラストは絶景。積雪の翌朝が狙い目。\n\n🍂 紅葉と金閣（11月下旬）\n　　赤・金・緑のコントラストが最高の季節。\n\n🌸 夜明けの金閣\n　　開門直後（9時）は観光客が少なくゆっくり撮影できます。",
        "old_map_description": "1397年足利義満が創建。江戸時代の絵図にも「金閣」として描かれた京都を代表する名所。",
        "old_map_detail": "1397年足利義満が「北山山荘」として造営。義満の死後に禅寺となった。現在の建物は1955年の再建。1994年世界遺産登録。",
        "cloud_info": "衣笠山を背景にした金閣寺からの空の眺めは特別な美しさがあります。",
        "cloud_detail": "鏡湖池から空を見上げると、金閣と雲のコントラストが楽しめます。\n⚠️ 正確な天気予報は気象庁等でご確認ください。",
        "trust_score": 1.0, "approved": True, "location_limited": False, "location_limited_content": "",
    },
    {
        "id": "osaka_castle_007", "name": "大阪城", "name_kana": "おおさかじょう",
        "category": "castle", "priority": 1, "wiki_title": "大阪城",
        "lat": 34.6873, "lon": 135.5262, "altitude": 50,
        "prefecture": "大阪府", "city": "大阪市中央区",
        "description": "豊臣秀吉が築いた天下統一の象徴。現在の天守閣は1931年再建。約600本の桜が咲く大阪城公園として整備。",
        "main_detail": (
            "🏯 豊臣秀吉が1583年に築城開始\n\n"
            "📜 天下統一の象徴\n"
            "　　秀吉の権力の象徴として絢爛豪華な城を築いた。\n\n"
            "🌸 大阪城公園\n"
            "　　約600本の桜が咲く花見の名所。\n\n"
            "👁 天守閣からの眺望\n"
            "　　大阪市内・六甲山・生駒山まで一望できる。\n\n"
            "🎵 大阪城野外音楽堂\n"
            "　　夏にコンサートが開催される人気スポット。"
        ),
        "urban_legend": "大阪城には豊臣秀吉の黄金の茶室が隠されているという伝説が残る。城内のどこかに今も眠っているという噂も。",
        "urban_legend_detail": "秀吉が所持していた「黄金の茶室」は移動式で各地に運ばれたとされます。その行方は今もなお謎に包まれています。\n\n⚠️ AIエンターテイメント情報です。",
        "powerspot": "天下統一を成し遂げた豊臣秀吉のエネルギーが宿る城。立身出世・仕事運のパワースポットとして知られます。",
        "powerspot_detail": "農民から天下人へと上り詰めた秀吉のパワーにあやかれる場所として、ビジネスパーソンに人気のパワースポットです。\n\n⚠️ AIエンターテイメント情報です。",
        "festival": "春：桜まつり（3月下旬〜4月）・夏：大阪城音楽堂イベント",
        "festival_detail": "【3月下旬〜4月上旬】桜まつり（約600本）\n【夏季】大阪城野外音楽堂でのコンサート\n【10〜11月】紅葉シーズン\n※詳細は大阪城公園公式サイトでご確認ください。",
        "healing_text": "📸 撮影スポット情報",
        "healing_detail": "📍 おすすめ撮影ポイント\n\n🌸 西の丸庭園（春）\n　　桜と天守閣の定番構図。600本の桜が咲き誇る。\n\n🏯 極楽橋から見上げる天守閣\n　　石垣と天守閣の迫力ある構図が撮れる。\n\n🌅 天守閣最上階からの夕景\n　　大阪の街が夕日に染まる絶景スポット。\n\n✨ ライトアップ（不定期）\n　　夜の大阪城は昼間とは異なる幻想的な雰囲気。",
        "old_map_description": "1583年豊臣秀吉が築城開始。江戸時代には徳川幕府により改修。明治以降に現在の公園として整備された。",
        "old_map_detail": "1583年築城開始。1615年大坂夏の陣で落城。現在の天守閣は1931年再建。江戸時代の絵図にも詳細に描かれた。",
        "cloud_info": "大阪城天守閣（標高約50m）からは大阪平野を一望。空気が澄んだ日は六甲山・生駒山も見える絶好の雲観察スポット。",
        "cloud_detail": "天守閣最上階から360度の眺望が楽しめます。\n⚠️ 正確な天気予報は気象庁等でご確認ください。",
        "trust_score": 1.0, "approved": True, "location_limited": False, "location_limited_content": "",
    },

    # ============================================================
    # ★ 追加スポット（2026年6月）
    # ============================================================
    {
        "id": "ikarugatera_008",
        "name": "斑鳩寺",
        "name_kana": "いかるがでら",
        "category": "temple",
        "priority": 2,
        "wiki_title": "斑鳩寺",
        "lat": 34.837339,
        "lon": 134.575457,
        "altitude": 20,
        "prefecture": "兵庫県",
        "city": "揖保郡太子町",
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
        "old_map_detail": "593年創建と伝わる古刹。播磨国の重要な寺院として江戸時代の絵図にも記されています。",
        "cloud_info": "揖保川沿いの平野に位置し、周囲の山並みと雲の眺めが美しいスポットです。",
        "cloud_detail": "揖保川流域の平野から見渡す空は開放的です。\n⚠️ 正確な天気予報は気象庁等でご確認ください。",
        "trust_score": 0.9,
        "approved": True,
        "location_limited": False,
        "location_limited_content": "",
    },
    {
        "id": "takaya_jinja_009",
        "name": "高屋神社",
        "name_kana": "たかやじんじゃ",
        "category": "shrine",
        "priority": 1,
        "wiki_title": "高屋神社",
        "lat": 34.160608,
        "lon": 133.654837,
        "altitude": 404,
        "prefecture": "香川県",
        "city": "観音寺市",
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
        "old_map_detail": "稲積山山頂に鎮座する古社。江戸時代の讃岐国の絵図にも記された地域の守護神。近年は天空の鳥居として全国的に有名に。",
        "cloud_info": "標高404mの山頂から見渡す讃岐平野と瀬戸内海。雲海が発生する日は特別な絶景が楽しめます。",
        "cloud_detail": "☁️ 雲海（秋〜冬の早朝）：讃岐平野を覆う雲海は絶景。\n🌤 積乱雲：夏の午後に瀬戸内海方面から発達します。\n⚠️ 正確な天気予報は気象庁等でご確認ください。",
        "trust_score": 1.0,
        "approved": True,
        "location_limited": False,
        "location_limited_content": "",
    },
    {
        "id": "kamo_jinja_010",
        "name": "賀茂神社",
        "name_kana": "かもじんじゃ",
        "category": "shrine",
        "priority": 2,
        "wiki_title": "賀茂神社_(たつの市)",
        "lat": 34.766021,
        "lon": 134.502835,
        "altitude": 50,
        "prefecture": "兵庫県",
        "city": "たつの市",
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
        "old_map_detail": "927年成立の延喜式に記された格式高い神社。江戸時代の播磨国絵図にも記載。龍野の地の守護神として崇敬を集めてきた。",
        "cloud_info": "たつの市の平野部に位置し、揖保川と周囲の山々を見渡せる清々しいスポットです。",
        "cloud_detail": "境内の高台から揖保川と周辺の山々が見渡せます。\n⚠️ 正確な天気予報は気象庁等でご確認ください。",
        "trust_score": 0.9,
        "approved": True,
        "location_limited": False,
        "location_limited_content": "",
    },
    {
        "id": "iwatsuhime_011",
        "name": "伊和都比売神社",
        "name_kana": "いわつひめじんじゃ",
        "category": "shrine",
        "priority": 2,
        "wiki_title": "伊和都比売神社",
        "lat": 34.727571,
        "lon": 134.408226,
        "altitude": 5,
        "prefecture": "兵庫県",
        "city": "赤穂市",
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
        "old_map_detail": "927年成立の延喜式に記された古社。赤穂藩の鎮守として江戸時代の絵図にも記載。播磨灘を望む立地から瀬戸内の船乗りたちの信仰を集めてきた。",
        "cloud_info": "瀬戸内海に面した境内から望む空と海の眺めは格別。播磨灘の雲の変化が楽しめます。",
        "cloud_detail": "☁️ 海霧（春〜初夏）：朝に瀬戸内海から霧が流れ込む幻想的な光景。\n🌤 入道雲（夏）：播磨灘に発達する入道雲は壮観。\n⚠️ 正確な天気予報は気象庁等でご確認ください。",
        "trust_score": 0.9,
        "approved": True,
        "location_limited": False,
        "location_limited_content": "",
    },

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

# ============================================================
# ■ 定数・スタイル
# ============================================================
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
    "📜 古地図":        {"key":"old_map",      "font":"Kaisei Decol", "bg":"rgba(240,155,170,0.80)","pin_color":"#FFE8E0","icon":"📜"},
    "☁️ 雲判定":        {"key":"cloud",        "font":"Kosugi Maru",  "bg":"rgba(160,210,235,0.76)","pin_color":"#E8F8FF","icon":"☁️"},
    "🌙 夜モード":      {"key":"night",        "font":"Noto Sans JP",  "bg":"rgba(10,10,40,0.75)",  "pin_color":"#8888FF","icon":"🌙"},
    # ★ おみくじモード
    "🎴 おみくじ":      {"key":"omikuji",      "font":"Kaisei Decol",  "bg":"rgba(200,140,160,0.82)","pin_color":"#FFE8F0","icon":"🎴"},
}
FONT_CLASS = {
    "Noto Sans JP":"font-noto-sans","Yuji Syuku":"font-yuji-syuku",
    "Noto Serif JP":"font-noto-serif","M PLUS 1p":"font-mplus1p",
    "Kosugi Maru":"font-kosugi-maru","Kaisei Decol":"font-kaisei-decol",
}
LANG_OPTIONS = {"🇯🇵 日本語":"ja","🇺🇸 English":"EN","🇨🇳 中文（簡体）":"ZH","🇰🇷 한국어":"KO"}
GSI_TILES = {
    "標準地図":       {"url":"https://cyberjapandata.gsi.go.jp/xyz/std/{z}/{x}/{y}.png",          "attr":"国土地理院","max_zoom":18},
    "写真（空中写真）":{"url":"https://cyberjapandata.gsi.go.jp/xyz/seamlessphoto/{z}/{x}/{y}.jpg","attr":"国土地理院","max_zoom":18},
    "淡色地図":       {"url":"https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png",          "attr":"国土地理院","max_zoom":18},
    "陰影起伏図":     {"url":"https://cyberjapandata.gsi.go.jp/xyz/hillshademap/{z}/{x}/{y}.png",  "attr":"国土地理院","max_zoom":16},
}
OSM_TILE = {"url":"https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png","attr":"© OpenStreetMap contributors","max_zoom":19}

# ============================================================
# ■ ユーティリティ
# ============================================================
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
        (0.5,  3, None),
        (2.0,  5, None),
        (5.0,  3, None),
        (50.0, 5, None),       # 50km以内：追加スポット対応
        (100.0,2, ["mountain"]), # 山は100km以内まで（無制限にしない）
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

# ============================================================
# ■ センサーJS
# ============================================================
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

# ============================================================
# ■ Folium地図
# ============================================================
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

# ============================================================
# ■ CSS
# ============================================================
GLOBAL_CSS = "<style>\n@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;700&family=Noto+Serif+JP:wght@400;700&family=Yuji+Syuku&family=M+PLUS+1p:wght@700&family=Kosugi+Maru&family=Kaisei+Decol:wght@400;700&display=swap');\n" + """
#MainMenu,footer,header{visibility:hidden;}
.stApp{background:linear-gradient(160deg,#c8e8fa 0%,#b8d0f5 22%,#c0dcf8 44%,#bbd4f8 66%,#cce6fb 85%,#c4dff8 100%)!important;}
.stApp::before{content:'';position:fixed;inset:0;
  background:radial-gradient(ellipse at 15% 10%,rgba(255,255,255,0.45) 0%,transparent 45%),
             radial-gradient(ellipse at 85% 80%,rgba(180,200,255,0.25) 0%,transparent 50%);
  pointer-events:none;z-index:0;}
.block-container{padding-top:1rem!important;padding-bottom:1rem!important;max-width:900px!important;position:relative;z-index:1;}

/* ① 4行クランプ（修正版） */
.ar-card{border-radius:18px;padding:18px 22px;margin:10px 0;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);
  border:1px solid rgba(255,255,255,0.60);box-shadow:0 5px 28px rgba(160,80,120,0.18);
  animation:fadeInUp 0.5s ease both;color:#1a0010!important;font-size:18px!important;font-weight:500;}
.ar-card-title{font-size:23px;font-weight:800;color:#0a0008;
  margin-bottom:7px;display:flex;align-items:center;gap:8px;letter-spacing:0.04em;}
.ar-card-kana{font-size:15px;color:#2a0a1a;font-weight:500;margin-bottom:10px;}
.ar-card-photo{width:100%;max-height:200px;object-fit:cover;border-radius:10px;margin-bottom:10px;}
.ar-card-photo-credit{font-size:10px;color:#5a3a4a;text-align:right;margin-bottom:6px;}
/* ① 4行クランプ */
.ar-card-summary{overflow:hidden;display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;
  font-size:17px;line-height:1.75;color:#1a0010;font-weight:500;margin-top:6px;}
.ar-card-detail{font-size:16px;line-height:1.95;color:#0a0008;font-weight:500;
  white-space:pre-wrap;margin:12px -4px -4px;padding:14px 18px;background:rgba(0,0,0,0.08);
  border-radius:0 0 14px 14px;border-top:1px solid rgba(100,50,70,0.25);}
.ar-detail-label{font-size:14px;color:#2a0a1a;font-weight:700;margin-top:10px;padding-top:7px;
  border-top:1px dashed rgba(100,50,70,0.30);}
.ar-badge{display:inline-flex;align-items:center;gap:4px;background:rgba(0,0,0,0.12);
  border:1px solid rgba(100,50,70,0.35);border-radius:20px;padding:4px 12px;font-size:14px;
  margin:3px 4px 3px 0;color:#0a0008;font-weight:700;}
.ar-disclaimer{font-size:13px;font-weight:500;margin-top:8px;color:#2a0a1a;}
.ar-fallback-badge{display:inline-block;background:rgba(0,0,0,0.10);border:1px solid rgba(100,50,70,0.30);
  border-radius:8px;padding:2px 10px;font-size:13px;color:#2a0a1a;font-weight:600;margin-bottom:6px;}
.wiki-badge{display:inline-block;background:rgba(60,120,200,0.15);border:1px solid rgba(60,120,200,0.35);
  border-radius:6px;padding:1px 8px;font-size:11px;color:#1a3a7a;font-weight:600;margin-left:6px;}
.osm-badge{display:inline-block;background:rgba(60,180,80,0.15);border:1px solid rgba(60,180,80,0.35);
  border-radius:6px;padding:1px 8px;font-size:11px;color:#1a5a2a;font-weight:600;margin-left:4px;}
/* ⑦ Streamlitエラー表示を強制非表示 */
div[data-testid="stException"],
div[class*="stException"],
div[class*="exception-text"],
.element-container div[class*="stAlert"] div[class*="stMarkdownContainer"] pre,
iframe + div div[data-testid="stException"]{
  display:none!important;visibility:hidden!important;height:0!important;
  overflow:hidden!important;margin:0!important;padding:0!important;}
@keyframes fadeInUp{from{opacity:0;transform:translateY(10px);}to{opacity:1;transform:translateY(0);}}
.app-header{text-align:center;padding:14px 0 6px;animation:fadeInUp 0.4s ease both;}
.app-header h1{font-family:'Kaisei Decol',serif;font-size:27px;color:#2a4a7a;
  text-shadow:0 0 22px rgba(140,180,255,0.45),0 1px 3px rgba(60,90,160,0.25);margin:0;letter-spacing:0.08em;}
.app-header p{color:#4a6a9a;font-size:13px;margin:5px 0 0;}
.safety-warning{background:rgba(255,255,255,0.62);border:1.5px solid rgba(100,140,200,0.55);
  border-radius:14px;padding:16px 20px;margin:10px 0 18px;animation:fadeInUp 0.5s ease both;text-align:center;backdrop-filter:blur(10px);}
.safety-warning p{color:#2a4a7a;font-size:15px;font-weight:700;margin:0;line-height:1.65;}
.info-panel{background:rgba(255,255,255,0.44);border-radius:10px;padding:10px 14px;
  color:#2a4a7a;font-size:13px;line-height:2.0;border:1px solid rgba(120,160,220,0.28);}
.ar-compass{background:rgba(255,255,255,0.40);border-radius:12px;padding:10px 14px;margin-top:8px;
  color:#2a4a7a;font-size:13px;text-align:center;border:1px solid rgba(120,160,220,0.32);backdrop-filter:blur(6px);}
.mode-title-bar{border-radius:12px;padding:10px 16px;margin-bottom:10px;
  border:1px solid rgba(255,255,255,0.55);text-align:center;color:#1a0010;font-size:18px;font-weight:700;
  animation:fadeInUp 0.3s ease both;backdrop-filter:blur(10px);}
.location-limited-card{border-radius:14px;padding:14px 18px;margin:8px 0;
  background:rgba(200,230,255,0.60);border:1.5px solid rgba(120,180,240,0.55);backdrop-filter:blur(10px);}
.lookaround-card{background:rgba(255,255,255,0.38);border-radius:14px;padding:14px 18px;margin:8px 0;
  border:1px solid rgba(120,160,220,0.35);color:#2a4a7a;font-size:15px;line-height:1.9;backdrop-filter:blur(6px);}
.lookaround-card h4{color:#2a4a7a;margin:0 0 10px;font-size:17px;font-weight:700;}
.map-placeholder{background:rgba(255,255,255,0.40);border:1px solid rgba(100,150,210,0.32);
  border-radius:14px;padding:20px;text-align:center;color:#3a5a8a;font-size:14px;margin:8px 0;}
.share-card{background:rgba(255,255,255,0.42);border-radius:12px;padding:12px 16px;margin:8px 0;
  border:1px solid rgba(120,160,220,0.30);color:#2a4a7a;font-size:13px;}
.cloud-result{border-radius:14px;padding:16px 18px;margin:8px 0;
  background:rgba(160,210,235,0.55);border:1px solid rgba(100,180,220,0.45);color:#1a3a5a;font-size:16px;}
.report-form{background:rgba(255,255,255,0.42);border-radius:14px;padding:16px 18px;
  margin:8px 0;border:1px solid rgba(120,160,220,0.30);color:#2a4a7a;font-size:15px;}
.app-footer{text-align:center;color:rgba(50,80,140,0.65);font-size:11px;padding:22px 0 10px;line-height:1.9;}
.phase7-badge{display:inline-block;background:rgba(100,160,255,0.22);border:1px solid rgba(100,160,255,0.50);
  border-radius:8px;padding:2px 10px;font-size:12px;color:#2a4a9a;}
.sensor-active-badge{display:inline-block;background:rgba(60,180,100,0.25);border:1px solid rgba(60,180,100,0.55);
  border-radius:8px;padding:2px 10px;font-size:12px;color:#1a6a3a;}
.sensor-manual-badge{display:inline-block;background:rgba(100,140,220,0.20);border:1px solid rgba(100,140,220,0.45);
  border-radius:8px;padding:2px 10px;font-size:12px;color:#3a5a9a;}
/* ③ スマホGPS説明文 */
.gps-auto-note{background:rgba(60,180,100,0.15);border:1px solid rgba(60,180,100,0.40);
  border-radius:10px;padding:10px 14px;color:#1a5a30;font-size:13px;margin-bottom:8px;line-height:1.7;}
/* ⑥ スマホアコーディオン */
.mobile-settings{background:rgba(255,255,255,0.48);border-radius:10px;padding:10px 14px;
  color:#3a5a8a;font-size:13px;margin-bottom:8px;border-left:3px solid rgba(100,150,220,0.60);}
/* AR View */
.ar-view-container{background:rgba(10,15,40,0.90);border-radius:18px;padding:16px;margin:8px 0;
  border:1px solid rgba(100,150,255,0.35);box-shadow:0 4px 24px rgba(0,20,80,0.4);}
.ar-horizon-bar{background:rgba(0,0,0,0.55);border-radius:10px;padding:10px 14px;margin-bottom:12px;
  border:1px solid rgba(80,120,200,0.4);font-family:'Noto Sans JP',sans-serif;font-size:14px;
  color:#AAD4FF;text-align:center;letter-spacing:0.05em;line-height:1.8;}
.ar-compass-rose{text-align:center;font-size:13px;color:#88AAFF;margin-top:4px;font-family:'Noto Sans JP',sans-serif;}
/* おみくじ */
.omikuji-result{border-radius:20px;padding:24px 20px;margin:12px 0;text-align:center;backdrop-filter:blur(10px);}
/* 夜モード */
.night-bg{background:linear-gradient(160deg,#050510 0%,#0a0a28 40%,#080818 100%)!important;}
/* フォント */
.font-noto-sans{font-family:'Noto Sans JP',sans-serif;}
.font-yuji-syuku{font-family:'Yuji Syuku',serif;}
.font-noto-serif{font-family:'Noto Serif JP',serif;}
.font-mplus1p{font-family:'M PLUS 1p',sans-serif;font-weight:700;}
.font-kosugi-maru{font-family:'Kosugi Maru',sans-serif;}
.font-kaisei-decol{font-family:'Kaisei Decol',serif;}
/* Streamlit上書き */
div[data-testid="stRadio"] label,div[data-testid="stSelectbox"] label,
div[data-testid="stSlider"] label{color:#3a5a8a!important;font-size:14px!important;}
div[data-testid="stRadio"] div[role="radiogroup"] label{color:#2a4a7a!important;font-size:15px!important;}
div[data-testid="stMarkdownContainer"] p{color:#2a4a7a;}
.stButton>button{background:linear-gradient(135deg,#6aaaf0 0%,#4888e0 100%)!important;color:#FFFFFF!important;
  border:none!important;border-radius:10px!important;font-weight:700!important;
  box-shadow:0 2px 10px rgba(80,130,220,0.32)!important;}
.stButton>button:hover{background:linear-gradient(135deg,#80baf8 0%,#5898f0 100%)!important;}
div[data-testid="stToggle"] label{color:#3a5a8a!important;}
hr{border-color:rgba(100,150,210,0.22)!important;}
details summary{color:#2a4a7a!important;font-size:15px;}
""" + "</style>"

# ============================================================
# ■ セッション状態
# ============================================================
def init_session():
    defaults = {
        "safety_shown":False,"map_zoom":13,
        "preset_lat":34.8330,"preset_lon":134.8620,
        "heading_buf":[],"prev_lat":None,"prev_lon":None,
        "sensor_mode":"manual","selected_lang":"ja",
        "osm_spots":[],"osm_loaded":False,
        "night_mode":False,"selected_area":"播磨エリア",
        "omikuji_result": None,
        "selected_spot_id": None,  # ★ 選択中のプリセットスポットID
        "osm_center_lat": None,    # ★ OSM取得の基準座標
        "osm_center_lon": None,
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
# ■ ARカードレンダリング（写真付き・4行クランプ）
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
    fb='<span class="ar-fallback-badge">📡 サンプルデータ</span>' if content["is_dummy"] else ""
    osm_badge='<span class="osm-badge">OSM</span>' if is_osm else ""
    wiki_badge='<span class="wiki-badge">Wikipedia</span>' if spot.get("wiki_title") and not is_osm else ""

    # ④ 写真（Wikimedia Commons）
    det=(f'<div class="ar-detail-label">▼ {label_map.get(mode_key,"📋 詳細情報")}</div>'
         f'<div class="ar-card-detail">{content["detail"]}</div>') if expanded else ""

    html=(f'<div class="ar-card {fc}" style="background:{mode_cfg["bg"]};opacity:{opac};">'
          + fb + osm_badge + wiki_badge
          + f'<div class="ar-card-title">{cat_icon} {spot["name"]}</div>'
          + f'<div class="ar-card-kana">{spot.get("name_kana","")} ／ {spot["prefecture"]} {spot["city"]}</div>'
          + f'<span class="ar-badge">📏 {dist_label(dist_km)}</span>'
          + f'<span class="ar-badge">🧭 {deg_to_dir(brg)} {int(brg)}°</span>'
          + f'<span class="ar-badge">🏔 {spot["altitude"]}m</span>'
          + f'<div class="ar-card-summary">{content["summary"]}</div>'
          + det
          + (f'<div class="ar-disclaimer">{disclaimer}</div>' if disclaimer else "")
          + "</div>")
    st.markdown(html, unsafe_allow_html=True)

# ============================================================
# ■ おみくじ機能
#   確率：大吉50%・吉25%・中吉15%・小吉7%・末吉2%・大凶1%
#   名言：没後70年以上の偉人の言葉（著作権切れ）を使用
#   ※ 名言は一般的に広く知られた格言のみ採用
# ============================================================
import random

OMIKUJI_DATA = {
    "大吉": {
        "prob": 0.50,
        "color": "#FFD700",
        "bg": "rgba(255,200,50,0.25)",
        "border": "rgba(255,200,50,0.70)",
        "messages": [
            "素晴らしい！最高の運気です！✨",
            "人生最高！全てがうまくいく予感！🌟",
            "毎日が楽しみですね！輝く未来が待っています！🌸",
            "あなたの笑顔が周りを幸せにします！😊",
            "今日という日を思いっきり楽しんでください！🎉",
            "運命があなたに微笑んでいます！🌺",
            "全ての願いが叶う最高の運気！🍀",
            "あなたに関わる全ての人が幸せになります！💫",
            "今すぐ挑戦してみて！必ずうまくいきます！🚀",
            "太陽のように輝くあなたの未来！☀️",
            "大きな幸運があなたのもとへ飛んできます！🦋",
            "今日は何をやってもうまくいく最高の日！🎊",
            "あなたの努力が今日花開きます！🌻",
            "運気MAX！人生の転換点がここにあります！⭐",
            "幸せのシャワーがあなたに降り注ぎます！🌈",
            "最高の出会いと最高の結果が待っています！💖",
            "あなたのポジティブエネルギーが奇跡を起こします！✨",
            "全てのことが味方になってくれています！🌟",
            "今がチャンス！思い切って踏み出しましょう！👣",
            "世界があなたを応援しています！🌍",
            "あなたの輝きで周りを照らしてください！💡",
            "今日の出来事が人生最高の思い出になります！📸",
            "運気上昇中！どんどん良くなっていきます！📈",
            "あなたには無限の可能性があります！🎯",
            "喜びと感謝で満ち溢れる最高の日です！🙏",
            "思い描いた夢が現実になる時がきました！🌠",
            "周りの全員がにっこり！あなたは幸運の星！⭐",
            "今日出会う人全員があなたに良い影響を与えます！🤝",
            "自信を持って！あなたは最高の存在です！👑",
            "人生の中で最も輝かしい瞬間が訪れています！🌟",
        ],
        "kotowaza": [
            ("天は自ら助くる者を助く", "ベンジャミン・フランクリン（1790年没）"),
            ("努力する人は希望を語り、怠ける人は不満を語る", "井上靖（作家・1991年没）※ことわざとして一般化"),
            ("われ思う、ゆえにわれあり", "デカルト（1650年没）"),
            ("人生は勇気ある冒険か、それとも無かのどちらかだ", "ヘレン・ケラー（1968年没）"),
            ("千里の道も一歩から", "老子（紀元前の哲人）"),
            ("己の欲せざるところを人に施すなかれ", "孔子（紀元前479年没）"),
            ("今日という日は、残りの人生の最初の日である", "一般的な格言"),
            ("知識は力なり", "フランシス・ベーコン（1626年没）"),
            ("失敗は成功のもと", "エジソン（1931年没）の精神に基づく格言"),
            ("人は考える葦である", "パスカル（1662年没）"),
            ("行動こそが人生を変える", "ゲーテ（1832年没）の思想より"),
            ("すべての始まりは小さい", "キケロ（紀元前43年没）"),
            ("最大の危険は目標が高すぎて失敗することではなく、低すぎて達成することだ", "ミケランジェロ（1564年没）"),
            ("天才とは努力する才能である", "ミケランジェロ（1564年没）の精神より"),
            ("愛することは、愛されること以上に幸福である", "バルザック（1850年没）"),
            ("喜びは分かち合うことで倍になり、悲しみは分かち合うことで半分になる", "シラー（1805年没）"),
            ("賢者は己を変え、愚者は世界を変えようとする", "バーナード・ショー（1950年没）"),
            ("すべての道はローマに通ず", "中世ラテンの格言"),
            ("我が人生、悔いなし", "坂本龍馬（1867年没）の精神より"),
            ("学びて思わざれば則ち罔し、思いて学ばざれば則ち殆し", "孔子（紀元前479年没）"),
            ("汝自身を知れ", "ソクラテス（紀元前399年没）"),
            ("最もよく生きた人とは、最もよく行動した人である", "アリストテレス（紀元前322年没）"),
            ("笑顔は世界共通の言語である", "チャールズ・チャップリン（1977年没）"),
            ("人間の最大の武器は謙虚さと優しさだ", "一般的な格言"),
            ("過去は変えられないが、未来は変えられる", "ベンジャミン・フランクリン（1790年没）の思想より"),
            ("友人は第二の自己である", "アリストテレス（紀元前322年没）"),
            ("人の一生は短い。だから美しくあれ", "シェイクスピア（1616年没）の精神より"),
            ("自分自身を信じよ", "ゲーテ（1832年没）"),
            ("希望は目覚めている人間の夢である", "アリストテレス（紀元前322年没）"),
            ("知足者富（足るを知る者は富む）", "老子（紀元前の哲人）"),
        ],
    },
    "吉": {
        "prob": 0.25,
        "color": "#FF88AA",
        "bg": "rgba(255,136,170,0.20)",
        "border": "rgba(255,136,170,0.60)",
        "messages": [
            "良い運気が流れています！前向きに進もう！💪",
            "幸運があなたのそばにいます！🍀",
            "今日も素敵な一日になりそうです！🌺",
            "チャンスをつかむ準備はできていますか？✨",
            "あなたの努力は必ず報われます！🌟",
            "良いことが次々と起きる予感がします！🎶",
            "笑顔でいれば良いことが引き寄せられます！😄",
            "あなたの周りに良い人が集まってきます！🤝",
            "今日の一歩が明日の大きな変化につながります！👣",
            "運気上昇中！この流れに乗りましょう！🌊",
            "心が豊かな時、全てがうまくいきます！💎",
            "今日は思い切って行動してみましょう！🎯",
            "あなたの言葉が人の心を動かします！💬",
            "良縁が近づいています！アンテナを張って！📡",
            "今日の努力が明日の宝物になります！🏆",
            "あなたの笑顔が最大の武器です！😊",
            "チャレンジする心が幸運を呼び込みます！🚪",
            "感謝の気持ちが更なる幸運を引き寄せます！🙏",
            "今日出会う人が人生を変えるかもしれません！🌟",
            "あなたの才能が今日輝きます！💡",
            "小さな親切が大きな幸運に変わります！🌸",
            "今日はいつもより少し贅沢してみましょう！🍰",
            "前向きな言葉を発すると運気が上がります！🗣️",
            "あなたの直感を信じてみましょう！🎲",
            "今日の選択が明日を明るくします！☀️",
            "良いことを引き寄せるポジティブな波動があります！🌊",
            "今日は新しいことを一つ始めてみましょう！🌱",
            "あなたの周りに幸運のオーラが漂っています！✨",
            "感謝の心が幸せを倍増させます！💝",
            "今日も全力で楽しみましょう！🎉",
        ],
        "kotowaza": [
            ("自分を信じよ、そうすれば生き方がわかる", "ゲーテ（1832年没）"),
            ("継続は力なり", "エジソン（1931年没）の精神に基づく格言"),
            ("人を愛する者は、人にも愛される", "孔子（紀元前479年没）"),
            ("一番暗い夜の後に、一番明るい朝が来る", "ヴィクトル・ユゴー（1885年没）"),
            ("行動する者だけが道を切り開く", "ゲーテ（1832年没）"),
            ("今日やれることを明日に延ばすな", "ベンジャミン・フランクリン（1790年没）"),
            ("己に勝つことが最大の勝利である", "プラトン（紀元前348年没）"),
            ("よく学び、よく遊べ", "フランシス・ベーコン（1626年没）の精神より"),
            ("情けは人のためならず", "日本の古い教え"),
            ("人を知る者は智、己を知る者は明", "老子（紀元前の哲人）"),
            ("与えることは最大の喜びである", "アリストテレス（紀元前322年没）"),
            ("幸せは分かち合うためにある", "シェイクスピア（1616年没）の精神より"),
            ("学問に王道なし", "ユークリッド（紀元前3世紀）"),
            ("友を得るには、友になれ", "エマーソン（1882年没）"),
            ("人生の長さではなく、深さが大事だ", "セネカ（65年没）"),
            ("まず動け、完璧は後からついてくる", "ナポレオン（1821年没）の精神より"),
            ("勇気は徳の始まりである", "プラトン（紀元前348年没）"),
            ("少欲知足（欲を少なく、足るを知る）", "お釈迦様の教え（紀元前486年頃没）"),
            ("諸行無常（全ては移り変わる）", "お釈迦様の教え（紀元前486年頃没）"),
            ("三人行けば必ず我が師あり", "孔子（紀元前479年没）"),
            ("己所不欲、勿施於人（自分がされたくないことは人にするな）", "孔子（紀元前479年没）"),
            ("時間は最も賢明な弁護士である", "ペリクレス（紀元前429年没）"),
            ("名声は水面の輪のようなもの", "シェイクスピア（1616年没）"),
            ("過ちを犯したことのない人は、何も試みなかった人だ", "アインシュタイン（1955年没）"),
            ("美しい心は美しい顔にまさる", "エマーソン（1882年没）"),
            ("人に魚を与えるより、魚の釣り方を教えよ", "老子の思想より"),
            ("真の知恵は無知を知ることから始まる", "ソクラテス（紀元前399年没）"),
            ("芸術は長く、人生は短い", "ヒポクラテス（紀元前370年頃没）"),
            ("愛は最も強い人間の感情だ", "プラトン（紀元前348年没）"),
            ("変化こそが唯一の永遠である", "ヘラクレイトス（紀元前475年頃没）"),
        ],
    },
    "中吉": {
        "prob": 0.15,
        "color": "#88AAFF",
        "bg": "rgba(136,170,255,0.20)",
        "border": "rgba(136,170,255,0.60)",
        "messages": [
            "着実に前進しています！この調子で！🚶",
            "コツコツと積み上げる今日が大切です！📚",
            "穏やかで幸せな日々が続きます！🌿",
            "小さな幸せを大切に！😌",
            "じっくり取り組む姿勢が吉と出ます！🎯",
            "今日の積み重ねが未来の財産になります！💰",
            "着実な一歩一歩が大きな成果につながります！👣",
            "焦らず丁寧に進むことが今の最善です！🌸",
            "穏やかな心が良い結果を生みます！🕊️",
            "今日は身の回りの整理をすると運気UP！🏠",
            "感謝の気持ちを大切にすると良いことが起きます！🙏",
            "地道な努力が光り輝く日が必ず来ます！⭐",
            "今日は誰かに優しくすると運気が上がります！💕",
            "少しずつ、でも確実に前進しています！🌱",
            "今日の丁寧な行動が明日の幸運を呼びます！🍀",
            "平和な心で過ごすことで運気が整います！🌙",
            "今日は自分を大切にする日にしましょう！💆",
            "着実な歩みが最終的には一番遠くへ連れて行きます！🏔️",
            "今日の親切が明日の幸せになって返ってきます！💖",
            "穏やかさの中に大きな力があります！🌊",
            "今日は好きなことに時間を使いましょう！🎨",
            "心の平和が全ての幸運の源です！☮️",
            "今日の気づきが人生を変えるかもしれません！💡",
            "少しの勇気が大きな扉を開きます！🚪",
            "今日という日を丁寧に生きましょう！🌸",
            "あなたの誠実さが人の心を動かします！💎",
            "今日は新しい趣味を試してみましょう！🎵",
            "小さな喜びを大切にすると幸せが積み重なります！🌺",
            "今日の学びが明日の力になります！📖",
            "丁寧な日常の中に本当の幸せがあります！🏡",
        ],
        "kotowaza": [
            ("過ぎ去った時間は戻らない。だから今を大切に", "セネカ（65年没）"),
            ("平和な心があれば、全ては豊かである", "キケロ（紀元前43年没）"),
            ("忍耐は苦い。しかしその実は甘い", "ルソー（1778年没）"),
            ("一日一日を大切に生きることが最善の知恵", "ゲーテ（1832年没）"),
            ("自分自身に帰れ、自然に従え", "ルソー（1778年没）"),
            ("静けさの中にこそ、力は宿る", "老子（紀元前の哲人）"),
            ("心が変われば行動が変わる", "ウィリアム・ジェームズ（1910年没）"),
            ("知ることよりも感じることが大切だ", "レイチェル・カーソン（1964年没）"),
            ("人の価値は何を得たかではなく、何を与えたかで決まる", "エマーソン（1882年没）"),
            ("万物は流転する", "ヘラクレイトス（紀元前475年頃没）"),
            ("中庸こそ最善の生き方", "アリストテレス（紀元前322年没）"),
            ("自分を愛することが他者を愛する始まり", "フロイト（1939年没）"),
            ("諸法無我（全ての存在は固定したものではない）", "お釈迦様の教え（紀元前486年頃没）"),
            ("自燈明（自らを灯明とせよ）", "お釈迦様の教え（紀元前486年頃没）"),
            ("温故知新（古きを温め、新しきを知る）", "孔子（紀元前479年没）"),
            ("知者は惑わず、仁者は憂えず、勇者は懼れず", "孔子（紀元前479年没）"),
            ("学而時習之（学びて時にこれを習う）", "孔子（紀元前479年没）"),
            ("人生は小さな幸せの積み重ねである", "チャールズ・ディケンズ（1870年没）"),
            ("最も確かな知識は経験から生まれる", "レオナルド・ダ・ヴィンチ（1519年没）"),
            ("芸術は感情の表現である", "トルストイ（1910年没）"),
            ("人は何かを失って初めて、その価値を知る", "バイロン（1824年没）"),
            ("美しい夕日を見るには、少し立ち止まることが必要だ", "一般的な格言"),
            ("偉大な思想家は皆、日々の小さなことから学ぶ", "一般的な格言"),
            ("感謝する心は最高の徳である", "キケロ（紀元前43年没）"),
            ("人間の魂は自然の中で最もよく養われる", "ルソー（1778年没）"),
            ("最も賢い人は、最もよく聴く人である", "ソクラテス（紀元前399年没）"),
            ("真の友情は成長し、真の愛は若返る", "バルザック（1850年没）"),
            ("勇気は恐怖がないことではなく、恐怖に打ち勝つことだ", "リンカーン（1865年没）"),
            ("最大の栄光は倒れないことではなく、倒れるたびに起き上がることだ", "ゴールドスミス（1774年没）"),
            ("人生に無駄な経験はない", "バルザック（1850年没）"),
        ],
    },
    "小吉": {
        "prob": 0.07,
        "color": "#88DDAA",
        "bg": "rgba(136,220,170,0.20)",
        "border": "rgba(136,220,170,0.60)",
        "messages": [
            "小さな幸せが積み重なっています！🌱",
            "今は準備の時！必ず花開きます！🌸",
            "丁寧に過ごすことで運気が上がります！✨",
            "焦らず一歩一歩進みましょう！👣",
            "小さな行動が大きな変化を生みます！🦋",
            "今日の小さな勇気が未来を変えます！💪",
            "じっくり育てている夢が芽吹く時が来ます！🌿",
            "今日は自分自身に優しくしてあげましょう！💕",
            "一つ一つ丁寧に取り組むと運気が整います！🎯",
            "今日の誠実な行動が信頼を育てます！🤝",
            "ゆっくりでもいい。着実に進んでいます！🐢",
            "今日は感謝の言葉を一つ伝えてみましょう！💌",
            "小さな喜びを見逃さないようにしましょう！🔍",
            "今日の忍耐が明日の大きな力になります！💎",
            "焦りを手放すと自然と道が開けます！🌊",
            "今日は好きな音楽を聴いてリフレッシュしましょう！🎵",
            "小さな一歩でも踏み出すことに意味があります！👟",
            "今日の学びが将来の大きな財産になります！📚",
            "静かな心で過ごすことが運気を高めます！🕯️",
            "今日は誰かのために何かしてみましょう！🌟",
            "あなたのペースで進むことが一番の近道です！🏃",
            "今日の小さな親切が大きな輪を生みます！🔄",
            "地道な努力は必ず報われる日が来ます！🌅",
            "今日は自然の中で深呼吸してみましょう！🌳",
            "焦らなくていい。あなたには時間があります！⏰",
            "今日の一言が誰かを勇気づけます！💬",
            "小さな積み重ねが人生を豊かにします！🏆",
            "今日は新しい視点で物事を見てみましょう！👀",
            "あなたの存在自体が価値があります！💖",
            "今日の穏やかな心が明日を変えます！☀️",
        ],
        "kotowaza": [
            ("千里の旅も一歩から始まる", "老子（紀元前の哲人）"),
            ("急がば回れ", "シェイクスピアの精神に通じる格言"),
            ("現在こそが唯一の現実である", "ヘラクレイトス（紀元前475年頃没）"),
            ("希望を持て。困難の中にこそ機会がある", "アリストテレス（紀元前322年没）"),
            ("一念岩をも通す", "日本の古い格言"),
            ("苦しみは人を成長させる唯一の教師だ", "ベートーヴェン（1827年没）"),
            ("成功する秘訣は目的の一貫性にある", "ディズレーリ（1881年没）"),
            ("人生の目的は幸福である", "アリストテレス（紀元前322年没）"),
            ("生きることは学ぶことである", "ゲーテ（1832年没）"),
            ("過去を忘れず、未来を恐れず、今を生きよ", "セネカ（65年没）"),
            ("小さなことに忠実な者は、大きなことにも忠実である", "ルカ（聖書の言葉）"),
            ("努力は裏切らない", "エジソン（1931年没）の精神より"),
            ("種を蒔かずして、花を咲かすことはできない", "一般的な格言"),
            ("自分の心の声を聴け", "ソクラテス（紀元前399年没）の精神より"),
            ("一期一会の精神で生きよ", "日本の茶道の精神"),
            ("初心に帰れ", "世阿弥（1443年頃没）"),
            ("怠らず進む者が道を制する", "ナポレオン（1821年没）"),
            ("真の強さは穏やかさの中にある", "老子（紀元前の哲人）"),
            ("諦めなければ失敗はない", "エジソン（1931年没）"),
            ("人の悲しみに共感できる心が最も美しい", "トルストイ（1910年没）"),
            ("雨の後には必ず虹が出る", "一般的な格言"),
            ("困難は人を鍛える", "キケロ（紀元前43年没）"),
            ("自分を信じることが最初の一歩だ", "ウィリアム・ジェームズ（1910年没）"),
            ("最善を尽くして、後は天に委ねよ", "一般的な格言"),
            ("今日の種まきが明日の収穫になる", "一般的な格言"),
            ("少欲知足（少ない欲で足るを知る）", "お釈迦様の教え（紀元前486年頃没）"),
            ("悟りとは今この瞬間に気づくこと", "お釈迦様の教え（紀元前486年頃没）"),
            ("仁者愛人（仁ある者は人を愛する）", "孔子（紀元前479年没）"),
            ("学びには終わりがない", "孔子（紀元前479年没）"),
            ("人は何度でも立ち上がれる", "リンカーン（1865年没）の精神より"),
        ],
    },
    "末吉": {
        "prob": 0.02,
        "color": "#AAAAAA",
        "bg": "rgba(180,180,180,0.20)",
        "border": "rgba(180,180,180,0.60)",
        "messages": [
            "今は嵐の前の静けさ。必ず晴れ間が来ます！☀️",
            "どんな状況も学びのチャンスです！📖",
            "今日の努力が明日の幸せを作ります！💪",
            "しなやかに、しなやかに乗り越えよう！🌊",
            "あなたには乗り越える力があります！⭐",
            "今は根を張る時。花は必ず咲きます！🌸",
            "試練の後には必ず喜びが待っています！🌈",
            "今日の我慢が明日の自信になります！💎",
            "どんな困難も必ず終わりがあります！🌅",
            "あなたの強さは試練の中で磨かれます！⚔️",
            "今この瞬間を大切に過ごしましょう！🕐",
            "焦らず、諦めず、前を向いて！🎯",
            "今日は静かに自分と向き合う時間を持ちましょう！🧘",
            "困難を乗り越えた先に大きな成長があります！🌱",
            "今日の忍耐が将来の財産になります！🏦",
            "一歩引いて全体を見渡すと道が開けます！🗺️",
            "今日は誰かに相談してみましょう！🗣️",
            "立ち止まることも大切な前進です！🌿",
            "今日の経験が未来のあなたを支えます！🤲",
            "小さな光を見つけてください。必ずあります！🕯️",
            "今日は休息も大切な選択です！😴",
            "あなたは今、大きく成長している最中です！🌳",
            "今日の「大変」が明日の「笑い話」になります！😄",
            "諦めないことが最大の才能です！💪",
            "今日は深呼吸して心を落ち着けましょう！🌬️",
            "あなたの周りに必ず助けてくれる人がいます！🤝",
            "今日の苦労は必ず報われます！🌟",
            "嵐の後には虹が出ます！🌈",
            "今日を生きることが明日への贈り物です！🎁",
            "どんな結果でも、あなたは十分頑張っています！👏",
        ],
        "kotowaza": [
            ("冬来たりなば春遠からじ", "シェリー（1822年没）"),
            ("失意の時こそ、人の真価が問われる", "キケロ（紀元前43年没）"),
            ("苦難の中にこそ、真の幸福の種がある", "ルソー（1778年没）"),
            ("涙の後には必ず笑顔が来る", "ヴィクトル・ユゴー（1885年没）"),
            ("嵐の海を渡った者だけが、新しい港を知る", "一般的な格言"),
            ("困難はダイヤモンドの原石である", "トーマス・カーライル（1881年没）"),
            ("最も深い川は最も静かに流れる", "カーティウス（ラテンの格言より）"),
            ("艱難汝を玉にす", "日本の古い格言"),
            ("逆境は最良の教師である", "バイロン（1824年没）"),
            ("人は苦しみを通じて賢くなる", "エスキュロス（紀元前456年没）"),
            ("希望は目覚めている人間の夢である", "アリストテレス（紀元前322年没）"),
            ("夜が最も暗くなるのは夜明けの前だ", "一般的な格言"),
            ("苦しみは心を磨く砥石である", "一般的な格言"),
            ("人生の試練は神からの贈り物だ", "一般的な格言"),
            ("切羽詰まった時こそ、知恵が生まれる", "プラトン（紀元前348年没）"),
            ("諸行無常（全ては移り変わる）", "お釈迦様の教え（紀元前486年頃没）"),
            ("苦しみがあるから、喜びがわかる", "お釈迦様の教え（紀元前486年頃没）"),
            ("克己復礼（己に打ち勝ち礼に立ち返れ）", "孔子（紀元前479年没）"),
            ("君子は困窮しても志を忘れない", "孔子（紀元前479年没）"),
            ("人生は旅であり、苦難もその風景の一つ", "ゲーテ（1832年没）"),
            ("絶望は愚か者の結論だ", "ディズレーリ（1881年没）"),
            ("我々の最大の栄光は一度も失敗しないことではなく、倒れるたびに立ち上がること", "ゴールドスミス（1774年没）"),
            ("暗闇を呪うよりも、一本の蝋燭を灯せ", "エレノア・ルーズベルト（1962年没）"),
            ("鉄は熱いうちに打て", "チョーサー（1400年没）"),
            ("忍耐は美徳の最高峰である", "チョーサー（1400年没）"),
            ("嵐の中でも錨を失うな", "一般的な格言"),
            ("傷ついた心は時が癒す", "シェイクスピア（1616年没）"),
            ("最も偉大な栄光は倒れないことではなく、倒れるたびに立ち上がることだ", "ネルソン・マンデラ（2013年没）※一般化された格言"),
            ("試練は人を強くする", "ニーチェ（1900年没）"),
            ("今日の苦しみは明日の喜びの種", "一般的な格言"),
        ],
    },
    "大凶": {
        "prob": 0.01,
        "color": "#FF6666",
        "bg": "rgba(255,100,100,0.18)",
        "border": "rgba(255,100,100,0.55)",
        "messages": [
            "大凶は大吉への入り口！ここからが逆転劇の始まりです！🔥",
            "どん底から這い上がれるのが本物の強さです！💎",
            "大凶を引いたあなたは超レア！特別な存在です！⭐",
            "これ以上悪くなりようがない！あとは上がるだけ！🚀",
            "大凶のあとには必ず大吉が来る！信じて前へ！🌟",
            "大凶でも笑える人が一番強い！あなたは最強です！😄",
            "逆境こそあなたの真の力を引き出します！💪",
            "大凶を引いたのはあなたが試練に選ばれた証です！👑",
            "今日の大凶は明日の大逆転の予告です！🎬",
            "困難を乗り越えた先に最高の人生が待っています！🌈",
            "大凶を笑い飛ばせる人に幸運は訪れます！🎭",
            "どんな状況でも笑顔でいられるあなたは最強です！😊",
            "試練はあなたの可能性を証明するチャンスです！🏆",
            "大凶も人生のスパイス！美しい物語の一ページです！📖",
            "今日の涙が明日の笑顔の種になります！🌱",
            "大凶を引いた勇気ある人に神様は微笑みます！🙏",
            "どん底こそ、新しいステージへの出発点！🚀",
            "大凶を引いたあなたに、こっそり幸運が隠れています！🍀",
            "逆境の中にこそ、あなたの本当の輝きがあります！✨",
            "大凶後の人生は劇的に良くなる法則があります！📈",
            "今日の大凶は将来の笑える武勇伝になります！🎉",
            "あなたの不屈の精神が大凶を大吉に変えます！⚡",
            "大凶でも前を向けるあなたは本当に素晴らしい！🌺",
            "試練という名のギフトを受け取りました！🎁",
            "大凶はあなたが成長する準備ができているサインです！🦋",
            "今が一番大変な時。あとは良くなるだけです！☀️",
            "大凶の次は必ず大吉！これが宇宙の法則です！🌍",
            "どんな結果でも、あなたの人生は素晴らしい！💖",
            "大凶を引いた勇気に拍手！あなたは本物の冒険者です！👏",
            "今日の大凶が人生最高の転換点になります！🌟",
        ],
        "kotowaza": [
            ("どんなに暗い夜も、夜明けは来る", "ヴィクトル・ユゴー（1885年没）"),
            ("七転び八起き、これが人生だ", "日本の古い教え"),
            ("最大の失敗は挑戦しないことだ", "エジソン（1931年没）"),
            ("私は失敗したのではない。うまくいかない方法を一万通り発見したのだ", "エジソン（1931年没）"),
            ("人生は山あり谷あり。谷があるから山が美しい", "一般的な格言"),
            ("試練は神が与えたもうた最高の贈り物", "一般的な格言"),
            ("倒れるのは失敗ではない。倒れたまま起き上がらないのが失敗だ", "コンフューシャス系格言"),
            ("嵐が去った後の虹は、より美しく見える", "一般的な格言"),
            ("苦労は人を強くする", "ニーチェ（1900年没）"),
            ("人生最大の危機が最大のチャンスである", "ナポレオン（1821年没）の精神より"),
            ("逆境の中にこそ、宝が隠されている", "セネカ（65年没）"),
            ("私の人生最良の日は、まだ来ていない", "ヴィクトル・ユゴー（1885年没）"),
            ("困難に直面した時こそ、人は真の自分を知る", "エピクテトス（135年没）"),
            ("今の苦しみは、将来の喜びの投資である", "一般的な格言"),
            ("全ての終わりは新しい始まりだ", "チョーサー（1400年没）"),
            ("一切皆苦（全ては苦であり、それを超えることが悟り）", "お釈迦様の教え（紀元前486年頃没）"),
            ("苦しみの先に、必ず光がある", "お釈迦様の教え（紀元前486年頃没）"),
            ("困窮してこそ、君子の真価がわかる", "孔子（紀元前479年没）"),
            ("過ちを改めることを恐れるな", "孔子（紀元前479年没）"),
            ("逆境は人を鍛え、順境は人を甘やかす", "ベーコン（1626年没）"),
            ("勇者は危険の中にのみ、安全を見出す", "ゲーテ（1832年没）"),
            ("人生で最も偉大なことは立ち上がることだ", "リンカーン（1865年没）"),
            ("最後に笑う者が最もよく笑う", "一般的な格言"),
            ("強くなければ生きられない。優しくなければ生きる資格がない", "レイモンド・チャンドラー（1959年没）"),
            ("ピンチはチャンスなり", "一般的な格言"),
            ("今日の雨が大地を潤す", "一般的な格言"),
            ("不幸は知恵の学校だ", "アンブロワーズ・ビアス（1914年没）"),
            ("太陽の光を知るためには、嵐を経験しなければならない", "ジャン・パウル（1825年没）"),
            ("人生に躓きはつきもの。大切なのは転んだ後に起き上がること", "一般的な格言"),
            ("地獄を通り抜けるなら、そのまま突き進め", "チャーチル（1965年没）"),
        ],
    },
}
def draw_omikuji() -> str:
    """おみくじを引く（確率通りに結果を返す）"""
    r = random.random()
    cumulative = 0.0
    for result, data in OMIKUJI_DATA.items():
        cumulative += data["prob"]
        if r < cumulative:
            return result
    return "大吉"

def render_omikuji(spot_name: str):
    """
    おみくじ画面を描画する。
    名言は没後70年以上の偉人のことわざ・格言のみ使用（著作権切れ）。
    """
    st.markdown(
        f'<div style="background:rgba(200,140,160,0.30);border-radius:16px;'
        f'padding:20px;margin:10px 0;border:1px solid rgba(200,140,160,0.50);'
        f'text-align:center;backdrop-filter:blur(8px);">'
        f'<div style="font-family:\'Kaisei Decol\',serif;font-size:22px;'
        f'color:#3a1a2a;font-weight:700;margin-bottom:6px;">🎴 おみくじ</div>'
        f'<div style="font-size:15px;color:#5a2a3a;margin-bottom:14px;">'
        f'{spot_name}でおみくじを引いてみましょう！</div>'
        f'</div>',
        unsafe_allow_html=True)

    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        shake = st.button("🎋 おみくじ筒を振る！", type="primary",
                          use_container_width=True, key="omikuji_btn")

    if shake or st.session_state.get("omikuji_result"):
        if shake:
            result = draw_omikuji()
            st.session_state.omikuji_result = result
        else:
            result = st.session_state.omikuji_result

        data = OMIKUJI_DATA[result]
        msg  = random.choice(data["messages"])
        koto, source = random.choice(data["kotowaza"])

        # 結果に応じた導入フレーズ
        intro_phrases = {
            "大吉": [
                "✨ 今日のあなたへ贈る言葉",
                "🌟 最高の運気のあなたへ",
                "🌸 輝くあなたへのメッセージ",
                "💖 幸運のあなたへ届けたい言葉",
                "⭐ 今日という特別な日に",
            ],
            "吉": [
                "🍀 良い運気のあなたへ",
                "💪 前向きなあなたへ贈る言葉",
                "🌺 今日のあなたを後押しする一言",
                "✨ 幸運のあなたへのメッセージ",
                "🌟 あなたの背中を押す言葉",
            ],
            "中吉": [
                "🌿 着実なあなたへ贈る言葉",
                "📚 丁寧に生きるあなたへ",
                "🎯 今日のあなたへのヒント",
                "💎 穏やかなあなたへのメッセージ",
                "🌸 じっくり進むあなたへ",
            ],
            "小吉": [
                "🌱 一歩一歩のあなたへ",
                "🦋 変化の途中にいるあなたへ",
                "💕 今日のあなたへそっと贈る言葉",
                "🕯️ 静かに頑張るあなたへ",
                "🌿 小さな勇気のあなたへ",
            ],
            "末吉": [
                "☀️ 嵐の中のあなたへ贈る言葉",
                "🌈 必ず晴れるあなたへ",
                "💪 踏ん張るあなたへのエール",
                "🌊 しなやかなあなたへのメッセージ",
                "⭐ 乗り越えていくあなたへ",
            ],
            "大凶": [
                "🔥 逆転劇の主役・あなたへ",
                "💎 どん底から這い上がるあなたへ",
                "🚀 これからが本番のあなたへ",
                "🌟 特別なあなたへ贈る言葉",
                "👑 本物の強さを持つあなたへ",
            ],
        }
        intro = random.choice(intro_phrases.get(result, ["今日のあなたへ"]))

        # 結果カード
        st.markdown(
            f'<div style="background:{data["bg"]};border:2px solid {data["border"]};'
            f'border-radius:20px;padding:24px 20px;margin:12px 0;text-align:center;'
            f'backdrop-filter:blur(10px);animation:fadeInUp 0.6s ease both;">'

            # おみくじ結果
            f'<div style="font-family:\'Kaisei Decol\',serif;font-size:52px;'
            f'color:{data["color"]};font-weight:700;'
            f'text-shadow:0 0 20px {data["color"]},0 2px 4px rgba(0,0,0,0.3);'
            f'margin-bottom:10px;letter-spacing:0.1em;">{result}</div>'

            # 一言メッセージ
            f'<div style="font-family:\'Noto Sans JP\',sans-serif;font-size:18px;'
            f'color:#3a1a2a;font-weight:700;margin-bottom:18px;'
            f'text-shadow:none;">{msg}</div>'

            # 区切り
            f'<div style="border-top:1px dashed rgba(100,60,80,0.3);'
            f'margin:16px 0;padding-top:16px;">'

            # 導入フレーズ
            f'<div style="font-family:\'Noto Sans JP\',sans-serif;font-size:14px;'
            f'color:rgba(60,30,50,0.75);margin-bottom:10px;letter-spacing:0.05em;">'
            f'{intro}</div>'

            # 名言
            f'<div style="font-family:\'Kaisei Decol\',serif;font-size:17px;'
            f'color:#2a1a2a;line-height:1.8;margin-bottom:8px;">'
            f'「{koto}」</div>'

            # 出典
            f'<div style="font-size:13px;color:rgba(80,40,60,0.75);">— {source}</div>'
            f'</div>'

            f'</div>',
            unsafe_allow_html=True)

        # もう一度引くボタン
        col4, col5, col6 = st.columns([1, 2, 1])
        with col5:
            if st.button("🔄 もう一度引く", use_container_width=True, key="omikuji_retry"):
                st.session_state.omikuji_result = None
                st.rerun()

        st.markdown(
            '<div style="font-size:11px;color:rgba(100,60,80,0.65);'
            'text-align:center;margin-top:8px;">'
            '※ おみくじはエンターテイメントです。参考程度にお楽しみください。<br>'
            '※ 名言はことわざ・格言等（著作権切れ）を使用しています。'
            '</div>',
            unsafe_allow_html=True)

# ============================================================
# ■ 見下ろしナビ
# ============================================================
def render_lookaround_nav(visible_spots, heading):
    if not visible_spots: return
    lines=[]
    for sp,dist_km,brg in visible_spots[:5]:
        icon=CATEGORY_STYLE.get(sp.get("category","default"),CATEGORY_STYLE["default"])["icon"]
        rel=(brg-heading+360)%360
        arrow=("↑ 正面" if rel<30 or rel>330 else "↗ 右前方" if rel<90 else
               "→ 右方" if rel<150 else "↓ 後方" if rel<210 else "← 左方" if rel<270 else "↖ 左前方")
        lines.append(f'<b>{icon} {sp["name"]}</b> — {arrow} {dist_label(dist_km)}'
                     f'<span style="opacity:0.7;font-size:13px;"> ({deg_to_dir(brg)}方向)</span>')
    st.markdown('<div class="lookaround-card font-noto-sans"><h4>🗺️ 見下ろしナビ</h4>'
                +"<br>".join(lines)+"</div>", unsafe_allow_html=True)

# ============================================================
# ■ 簡易ARビュー（レーダー）
# ============================================================
def render_ar_view(visible_spots, heading, user_lat, user_lon):
    COMPASS_CHARS={0:"北",45:"北東",90:"東",135:"南東",180:"南",225:"南西",270:"西",315:"北西"}
    spot_positions={}
    for sp,dist_km,brg in visible_spots[:6]:
        icon=CATEGORY_STYLE.get(sp.get("category","default"),CATEGORY_STYLE["default"])["icon"]
        spot_positions[brg]=f"{icon}{sp['name'][:4]}"
    def make_horizon():
        segs=[]
        for offset in range(-30,31,3):
            angle=(heading+offset+360)%360
            label=""
            for deg,name in COMPASS_CHARS.items():
                if abs((angle-deg+360)%360)<2: label=f"【{name}】"; break
            for sp_brg,sp_label in spot_positions.items():
                if abs((angle-sp_brg+360)%360)<3: label=f"▼{sp_label}"; break
            if not label: label="·" if offset%15==0 else " "
            segs.append(label)
        return "  ".join(segs)
    cx,cy,r=130,130,110
    rings_svg=""
    for ring_r,ring_color,ring_label in [(35,"rgba(255,80,120,0.4)","0.5km"),(65,"rgba(80,140,255,0.3)","2km"),(100,"rgba(80,200,255,0.2)","5km")]:
        rings_svg+=(f'<circle cx="{cx}" cy="{cy}" r="{ring_r}" fill="none" stroke="{ring_color}" stroke-width="1" stroke-dasharray="4,4"/>'
                    f'<text x="{cx+ring_r+2}" y="{cy-3}" font-size="9" fill="{ring_color}" font-family="sans-serif">{ring_label}</text>')
    compass_lines=""
    for angle_deg,label in [(0,"N"),(45,"NE"),(90,"E"),(135,"SE"),(180,"S"),(225,"SW"),(270,"W"),(315,"NW")]:
        rad=math.radians(angle_deg-90)
        x1=cx+int(r*0.85*math.cos(rad)); y1=cy+int(r*0.85*math.sin(rad))
        x2=cx+int(r*0.95*math.cos(rad)); y2=cy+int(r*0.95*math.sin(rad))
        lx=cx+int((r+14)*math.cos(rad)); ly=cy+int((r+14)*math.sin(rad))
        compass_lines+=(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="rgba(100,160,255,0.6)" stroke-width="1"/>'
                        f'<text x="{lx}" y="{ly+4}" font-size="10" fill="#88AAFF" text-anchor="middle" font-family="sans-serif" font-weight="bold">{label}</text>')
    arrow_rad=math.radians(heading-90); arrow_len=70
    ax=cx+int(arrow_len*math.cos(arrow_rad)); ay=cy+int(arrow_len*math.sin(arrow_rad))
    arrow_svg=(f'<line x1="{cx}" y1="{cy}" x2="{ax}" y2="{ay}" stroke="#FF88AA" stroke-width="2.5" stroke-linecap="round"/>'
               f'<circle cx="{ax}" cy="{ay}" r="4" fill="#FF88AA" opacity="0.9"/>')
    max_dist=5.0; spots_svg=""
    for sp,dist_km,brg in visible_spots[:6]:
        cat=sp.get("category","default")
        plot_r=min(dist_km/max_dist,1.0)*95; sp_rad=math.radians(brg-90)
        sx=cx+int(plot_r*math.cos(sp_rad)); sy=cy+int(plot_r*math.sin(sp_rad))
        dot_color={"shrine":"#FFD700","mountain":"#88FF88","castle":"#CC88FF","temple":"#FF88AA"}.get(cat,"#88CCFF")
        name_short=sp["name"][:4]
        spots_svg+=(f'<circle cx="{sx}" cy="{sy}" r="6" fill="{dot_color}" opacity="0.9" stroke="rgba(255,255,255,0.6)" stroke-width="1.5"/>'
                    f'<text x="{sx}" y="{sy-10}" font-size="10" fill="{dot_color}" text-anchor="middle" font-family="sans-serif" font-weight="bold">{name_short}</text>'
                    f'<text x="{sx}" y="{sy+20}" font-size="9" fill="rgba(200,220,255,0.8)" text-anchor="middle" font-family="sans-serif">{dist_label(dist_km)}</text>')
    center_svg=(f'<circle cx="{cx}" cy="{cy}" r="7" fill="#FF88AA" stroke="white" stroke-width="2" opacity="0.95"/>'
                f'<text x="{cx}" y="{cy+20}" font-size="10" fill="#FF88AA" text-anchor="middle" font-family="sans-serif">現在地</text>')
    svg=(f'<svg width="260" height="260" xmlns="http://www.w3.org/2000/svg">'
         f'<defs><radialGradient id="rg"><stop offset="0%" stop-color="rgba(20,40,100,0.85)"/><stop offset="100%" stop-color="rgba(5,10,40,0.95)"/></radialGradient></defs>'
         f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="url(#rg)" stroke="rgba(100,160,255,0.5)" stroke-width="2"/>'
         +rings_svg+compass_lines+spots_svg+arrow_svg+center_svg+f'</svg>')
    legend="　".join([
        '<span style="color:'+{"shrine":"#FFD700","mountain":"#88FF88","castle":"#CC88FF","temple":"#FF88AA"}.get(sp.get("category","default"),"#88CCFF")+';">'+
        CATEGORY_STYLE.get(sp.get("category","default"),CATEGORY_STYLE["default"])["icon"]+sp["name"][:4]+'</span>'
        for sp,_,_ in visible_spots[:4]])
    st.markdown(
        f'<div class="ar-view-container">'
        f'<div style="color:#88AAFF;font-size:14px;font-weight:700;margin-bottom:8px;">🎯 簡易ARビュー　🧭 {heading:.0f}°（{deg_to_dir(heading)}）</div>'
        f'<div class="ar-horizon-bar">{make_horizon()}</div>'
        f'<div style="text-align:center;">{svg}</div>'
        f'<div class="ar-compass-rose">● 現在地　{legend}</div>'
        f'<div style="font-size:11px;color:rgba(100,140,200,0.7);margin-top:6px;text-align:center;">フェーズ8で実カメラARに移行予定</div>'
        f'</div>', unsafe_allow_html=True)

# ============================================================
# ■ メインアプリ
# ============================================================
def main():
    init_session(); init_db()
    st.markdown(GLOBAL_CSS, unsafe_allow_html=True)

    st.markdown(
        '<div class="app-header"><h1>⛩ 観光AR案内 ／ 播磨・関西</h1>'
        '<p>山岳信仰の聖地・歴史の街をARで探索 '
        '<span class="phase7-badge">フェーズ7</span></p></div>',
        unsafe_allow_html=True)

    if not st.session_state.safety_shown:
        st.markdown(
            '<div class="safety-warning"><p>⚠️ 歩きながらの使用は危険です。<br>'
            '必ず立ち止まってご使用ください。<br>'
            '<span style="font-size:13px;font-weight:400;">登山中は足元・周囲の安全を最優先にしてください。</span></p></div>',
            unsafe_allow_html=True)
        if st.button("✅ 確認しました", type="primary", use_container_width=True):
            st.session_state.safety_shown=True; st.rerun()
        st.stop()

    gps_lat,gps_lon,gps_heading,gps_speed=get_gps_from_params()
    gps_active=gps_lat is not None
    if gps_active:
        buf=deque(st.session_state.heading_buf,maxlen=10)
        gps_heading=smooth_heading(gps_heading,buf)
        st.session_state.heading_buf=list(buf)
        gps_lat,gps_lon=smooth_gps(gps_lat,gps_lon,st.session_state.prev_lat,st.session_state.prev_lon)
        st.session_state.prev_lat=gps_lat; st.session_state.prev_lon=gps_lon

    col_ctrl,col_main=st.columns([1,2],gap="medium")

    with col_ctrl:
        # センサーJS
        st.components.v1.html(SENSOR_JS, height=320, scrolling=False)

        if gps_active:
            st.markdown('<div class="sensor-active-badge">🟢 GPS・コンパス取得中</div>', unsafe_allow_html=True)
        else:
            st.markdown('<div class="sensor-manual-badge">🎛 手動シミュレータ</div>', unsafe_allow_html=True)

        st.markdown("---")

        # ⑥ スマホ向けアコーディオンUI
        with st.expander("⚙️ 表示設定（タップで開く）", expanded=True):

            # 夜モード
            night_mode=st.toggle("🌙 夜モード", value=st.session_state.night_mode)
            st.session_state.night_mode=night_mode

            st.markdown("---")

            # エリア選択
            st.markdown("**🗾 エリア選択**")
            selected_area=st.selectbox("エリア",
                ["播磨エリア","関西エリア（奈良・京都・大阪）","全エリア"],
                index=0, label_visibility="collapsed")

            # ★ プリセットボタン（選択したスポットのみ表示）
            st.markdown("**📍 プリセット位置**")

            # プリセットとスポットIDの対応表
            if selected_area=="播磨エリア":
                presets={
                    "🧗 高御位山麓":  (34.8330,134.8620, "takamikura_001"),
                    "🏯 姫路城":     (34.8394,134.6939, "himeji_castle_003"),
                    "🛕 鶴林寺":     (34.7622,134.8394, "kakurinji_004"),
                    "🛕 斑鳩寺":     (34.837339,134.575457, "ikarugatera_008"),
                    "⛩ 賀茂神社":   (34.766021,134.502835, "kamo_jinja_010"),
                    "⛩ 伊和都比売神社":(34.727571,134.408226, "iwatsuhime_011"),
                    "⛩ 高屋神社":   (34.160608,133.654837, "takaya_jinja_009"),
                }
            elif "関西" in selected_area:
                presets={
                    "🛕 東大寺":  (34.6888,135.8398, "nara_todaiji_005"),
                    "✨ 金閣寺":  (35.0394,135.7292, "kyoto_kinkakuji_006"),
                    "🏯 大阪城":  (34.6873,135.5262, "osaka_castle_007"),
                    "🌸 奈良公園": (34.6851,135.8448, None),
                    "⛩ 伏見稲荷": (34.9671,135.7727, None),
                    "🌊 道頓堀":  (34.6688,135.5027, None),
                }
            else:
                presets={
                    "🧗 高御位山麓": (34.8330,134.8620, "takamikura_001"),
                    "🏯 姫路城":    (34.8394,134.6939, "himeji_castle_003"),
                    "🛕 東大寺":   (34.6888,135.8398, "nara_todaiji_005"),
                    "✨ 金閣寺":   (35.0394,135.7292, "kyoto_kinkakuji_006"),
                    "🏯 大阪城":   (34.6873,135.5262, "osaka_castle_007"),
                    "🛕 斑鳩寺":   (34.837339,134.575457, "ikarugatera_008"),
                }

            pcols=st.columns(2)
            for i,(label,(plat,plon,spot_id)) in enumerate(presets.items()):
                with pcols[i%2]:
                    is_selected = (
                        st.session_state.preset_lat == plat and
                        st.session_state.preset_lon == plon
                    )
                    btn_label = f"✅ {label}" if is_selected else label
                    if st.button(btn_label, use_container_width=True,
                                 key=f"preset_{i}_{selected_area[:2]}"):
                        st.session_state.preset_lat = plat
                        st.session_state.preset_lon = plon
                        st.session_state.selected_spot_id = spot_id
                        # OSMは座標変更時にリセット
                        st.session_state.osm_loaded = False
                        st.session_state.osm_spots = []
                        st.session_state.osm_center_lat = plat
                        st.session_state.osm_center_lon = plon
                        st.rerun()

            st.markdown("---")

            # ③ GPS説明文・スライダー
            if gps_active:
                st.markdown(
                    '<div class="gps-auto-note">'
                    '🟢 <b>GPS自動取得中です</b><br>'
                    '現在地・向きはスマホのセンサーから自動で入力されています。<br>'
                    'スライダーの操作は不要です。'
                    '</div>', unsafe_allow_html=True)
                sim_lat=gps_lat; sim_lon=gps_lon; sim_heading=gps_heading
                # GPS使用時は選択スポットをリセット
                st.session_state.selected_spot_id = None
            else:
                st.markdown(
                    '<div style="font-size:12px;color:#3a5a8a;background:rgba(200,220,255,0.3);'
                    'border-radius:8px;padding:6px 10px;margin-bottom:6px;">'
                    '💻 パソコン・GPS未取得時はスライダーで場所を模擬できます</div>',
                    unsafe_allow_html=True)
                sim_lat=st.slider("📍 緯度",34.70,35.10,st.session_state.preset_lat,0.0005,format="%.4f")
                sim_lon=st.slider("📍 経度",134.60,135.00,st.session_state.preset_lon,0.0005,format="%.4f")
                sim_heading=st.slider("🧭 向き（方位角）",0,359,45,1,help="0°=北/90°=東/180°=南/270°=西")

            st.markdown("---")

            # 言語選択
            st.markdown("**🌐 表示言語**")
            lang_label=st.selectbox("言語",list(LANG_OPTIONS.keys()),index=0,label_visibility="collapsed")
            selected_lang=LANG_OPTIONS[lang_label]
            if selected_lang!="ja" and not get_secret("DEEPL_API_KEY"):
                st.markdown('<div style="font-size:11px;color:#aa6030;background:rgba(255,200,150,0.3);border-radius:6px;padding:4px 8px;">⚠️ DeepL APIキー未設定。日本語で表示します。</div>', unsafe_allow_html=True)
                selected_lang="ja"

            st.markdown("---")

            # 地図タイル
            st.markdown("**🗺️ 地図タイル**")
            tile_opts=["標準地図","写真（空中写真）","淡色地図","陰影起伏図","OpenStreetMap"]
            tile_name=st.selectbox("タイル",tile_opts,index=0,label_visibility="collapsed")
            st.markdown(f'<div style="font-size:11px;color:#4a6a9a;">{"© OpenStreetMap contributors" if tile_name=="OpenStreetMap" else "出典：国土地理院"}</div>', unsafe_allow_html=True)
            map_zoom=st.slider("🔍 ズーム",10,17,st.session_state.map_zoom,1)
            st.session_state.map_zoom=map_zoom

            st.markdown("---")

            # 表示モード
            st.markdown('<div style="color:#3a5a8a;font-size:13px;margin-bottom:4px;">📡 表示モード</div>', unsafe_allow_html=True)
            mode_label=st.radio("モード",list(MODES.keys()),index=0,label_visibility="collapsed")
            mode_cfg=MODES[mode_label]

            st.markdown("---")
            show_detail=st.toggle("🔍 詳細情報を表示", value=False)

            # ★ 周辺スポット自動取得（選択座標基準）
            st.markdown("---")
            use_osm=st.toggle("🌐 周辺スポット自動取得（OSM）", value=False,
                              help="選択した観光地周辺のスポットをOpenStreetMapから取得します")

        # ★ スポット表示ロジック
        all_spots = list(SPOT_DATA_BUILTIN)

        # OSM取得（プリセット選択時の座標を基準に）
        if use_osm:
            osm_lat = st.session_state.osm_center_lat or sim_lat
            osm_lon = st.session_state.osm_center_lon or sim_lon
            if not st.session_state.osm_loaded:
                with st.spinner("OpenStreetMapから周辺スポットを取得中..."):
                    osm = fetch_overpass_spots(osm_lat, osm_lon, 3000)
                    st.session_state.osm_spots = osm
                    st.session_state.osm_loaded = True
            if st.session_state.osm_spots:
                all_spots = all_spots + st.session_state.osm_spots
                st.markdown(
                    f'<div style="font-size:11px;color:#2a7a3a;">🌐 OSM: {len(st.session_state.osm_spots)}件追加<br>© OpenStreetMap contributors</div>',
                    unsafe_allow_html=True)
        else:
            st.session_state.osm_loaded = False

        # ★ プリセット選択中はそのスポット1件のみ表示
        selected_id = st.session_state.get("selected_spot_id")
        if selected_id and not gps_active:
            # 選択スポットを1件だけ抽出
            selected_spot = next(
                (sp for sp in all_spots if sp.get("id") == selected_id), None)
            if selected_spot:
                dist = haversine_km(sim_lat, sim_lon, selected_spot["lat"], selected_spot["lon"])
                brg  = bearing_deg(sim_lat, sim_lon, selected_spot["lat"], selected_spot["lon"])
                visible_spots = [(selected_spot, dist, brg)]
                # OSMスポットも追加（周辺スポット取得時）
                if use_osm and st.session_state.osm_spots:
                    osm_visible = filter_spots(st.session_state.osm_spots, sim_lat, sim_lon)
                    visible_spots = visible_spots + osm_visible
            else:
                visible_spots = filter_spots(all_spots, sim_lat, sim_lon)
        else:
            # GPS使用時 or プリセット未選択時は通常フィルタ
            visible_spots = filter_spots(all_spots, sim_lat, sim_lon)

        nearest = visible_spots[0] if visible_spots else None

        sensor_badge='<span class="sensor-active-badge">🟢 GPS</span>' if gps_active else '<span class="sensor-manual-badge">🎛 手動</span>'
        if nearest:
            sp0,d0,_=nearest
            st.markdown(f'<div class="info-panel">{sensor_badge}<br>📍 {sim_lat:.4f}, {sim_lon:.4f}<br>🧭 {sim_heading:.0f}°（{deg_to_dir(sim_heading)}）<br>📡 {len(visible_spots)}件<br>📏 最寄り：{sp0["name"]} {dist_label(d0)}</div>', unsafe_allow_html=True)
        else:
            st.markdown(f'<div class="info-panel">{sensor_badge}<br>📍 {sim_lat:.4f}, {sim_lon:.4f}<br>📡 スポットなし</div>', unsafe_allow_html=True)

    with col_main:
        st.markdown(f'<div class="mode-title-bar" style="background:{mode_cfg["bg"]};">{mode_cfg["icon"]} {mode_label}</div>', unsafe_allow_html=True)

        # 地図（再描画抑制・完全固定key版）
        # プリセットボタン切替では地図を再描画しない
        # タイル変更・ズーム変更時のみ再描画
        map_key = f"kanko_map_{tile_name[:2]}_{map_zoom}"
        map_data={}; map_ok=False
        try:
            fmap=build_map(sim_lat,sim_lon,sim_heading,tile_name,map_zoom,mode_cfg,visible_spots)
            map_data=st_folium(fmap,width="100%",height=380,returned_objects=["last_clicked"],key=map_key)
            map_ok=True
        except Exception: pass
        if not map_ok:
            st.markdown('<div class="map-placeholder">🗺️ 地図の読み込みに失敗しました。F5で再読み込みしてください。</div>', unsafe_allow_html=True)
        if map_data and map_data.get("last_clicked"):
            c=map_data["last_clicked"]
            st.markdown(f'<div style="font-size:12px;color:#4a6a9a;text-align:right;">🖱 クリック地点：{c.get("lat",0):.5f}, {c.get("lng",0):.5f}</div>', unsafe_allow_html=True)

        sensor_lbl="🟢 GPS・コンパス取得中" if gps_active else "🎛 手動シミュレータ"
        st.markdown(
            f'<div class="ar-compass">{sensor_lbl}　🧭 {sim_heading:.0f}°（{deg_to_dir(sim_heading)}）　／　{len(visible_spots)}件<br>'
            f'<span style="font-size:12px;color:#4a6a9a;">フェーズ7：写真付きカード・撮影スポット・アコーディオンUI</span>'
            f'</div>', unsafe_allow_html=True)

        render_ar_view(visible_spots,sim_heading,sim_lat,sim_lon)
        render_lookaround_nav(visible_spots,sim_heading)

        # ★ おみくじモード
        if mode_cfg["key"] == "omikuji":
            spot_name = visible_spots[0][0]["name"] if visible_spots else "播磨エリア"
            render_omikuji(spot_name)
            st.markdown("---")

        # ARカード（写真付き）
        if not visible_spots:
            st.markdown('<div class="ar-card font-noto-sans" style="background:rgba(100,150,220,0.55);text-align:center;">📭 この範囲にスポットがありません</div>', unsafe_allow_html=True)
        else:
            for sp,dist_km,brg in visible_spots:
                render_spot_card(sp,mode_cfg,dist_km,brg,show_detail,selected_lang)
                if sp.get("location_limited") and dist_km<0.3:
                    st.markdown(f'<div class="location-limited-card">🌟 <strong style="color:#2a4a8a;">現地限定コンテンツ解放！</strong><br><span style="font-size:16px;color:#2a4060;">{sp["location_limited_content"]}</span></div>', unsafe_allow_html=True)

        # 雲判定
        if mode_cfg["key"]=="cloud":
            st.markdown("---")
            today_count=cloud_usage_today(); remaining=3-today_count
            st.markdown(f'<div class="cloud-result">☁️ <b>雲判定モード</b>　本日残り：{remaining}/3回<br><span style="font-size:14px;">空の写真をアップロードすると雲の種類を判定します。</span><br><span style="font-size:12px;opacity:0.8;">⚠️ 雲の分析はAIによるものです。正確な天気予報は気象庁等でご確認ください。</span></div>', unsafe_allow_html=True)
            uploaded=st.file_uploader("☁️ 空の写真をアップロード",type=["jpg","jpeg","png"],label_visibility="collapsed")
            if uploaded and remaining>0:
                with st.spinner("雲を分析中..."):
                    result=analyze_cloud_gemini(uploaded.read())
                if result.get("is_dummy"):
                    st.markdown(f'<div class="cloud-result">📡 {result["description"]}</div>', unsafe_allow_html=True)
                else:
                    st.markdown(f'<div class="cloud-result">☁️ <b>{result.get("cloud_type","不明")}</b><br>{result.get("description","")}<br>🌤 {result.get("weather_hint","")}<br><span style="font-size:12px;opacity:0.8;">⚠️ 雲の分析はAIによるものです。正確な天気予報は気象庁等でご確認ください。</span></div>', unsafe_allow_html=True)
            elif uploaded and remaining<=0:
                st.warning("本日の雲判定上限（3回）に達しました。明日またお試しください。")

        # SNSシェア
        if visible_spots:
            st.markdown("---")
            sp_share,d_share,_=visible_spots[0]
            share_text=make_share_text(sp_share,mode_cfg,d_share)
            st.markdown('<div class="share-card"><b>📤 SNSシェア</b><br><span style="font-size:12px;color:#4a6a9a;">以下のテキストをコピーしてSNSに投稿できます。</span></div>', unsafe_allow_html=True)
            st.text_area("シェアテキスト",value=share_text,height=120,label_visibility="collapsed")

        # 問題報告
        st.markdown("---")
        with st.expander("⚠️ 問題を報告する"):
            st.markdown('<div class="report-form"><b>📝 問題報告フォーム</b><br><span style="font-size:13px;">情報の誤り・表示の不具合などをご報告ください。</span></div>', unsafe_allow_html=True)
            report_spot=st.text_input("スポット名（任意）",placeholder="例：高御位神社")
            report_type=st.selectbox("問題の種類",["情報が間違っている","地図の位置がずれている","表示が崩れている","その他"])
            report_detail=st.text_area("詳細を教えてください",height=80)
            if st.button("📤 報告を送信",type="primary"):
                if report_detail:
                    st.success("✅ ご報告ありがとうございます！内容を確認して改善に努めます。"); st.balloons()
                else:
                    st.warning("詳細を入力してください。")

    # 夜モード適用
    if st.session_state.get("night_mode",False):
        st.markdown("""<style>
.stApp{background:linear-gradient(160deg,#050510 0%,#0a0a28 40%,#080818 100%)!important;}
.stApp::before{background:radial-gradient(ellipse at 20% 20%,rgba(30,30,100,0.4) 0%,transparent 50%),radial-gradient(ellipse at 80% 80%,rgba(20,20,80,0.3) 0%,transparent 50%);}
.app-header h1{color:#8888FF!important;}.app-header p{color:#6666AA!important;}
.info-panel,.ar-compass,.lookaround-card,.map-placeholder,.share-card,.report-form{background:rgba(20,20,60,0.65)!important;color:#CCCCFF!important;border-color:rgba(80,80,160,0.40)!important;}
.ar-card{color:#EEEEFF!important;}
.ar-card-title{color:#FFFFFF!important;}
.ar-card-kana{color:#CCCCFF!important;}
.ar-card-summary{color:#EEEEFF!important;}
.ar-card-detail{color:#DDDDFF!important;}
.ar-badge{color:#FFFFFF!important;border-color:rgba(150,150,255,0.50)!important;}
.ar-disclaimer{color:#BBBBEE!important;}
.mode-title-bar{color:#FFFFFF!important;}
</style>""", unsafe_allow_html=True)

    # ★ フェーズ8収益化時：Claude API 自動生成をここに追加
    # claude_api_key = get_secret("ANTHROPIC_API_KEY")
    # if claude_api_key:
    #     from anthropic import Anthropic
    #     client = Anthropic(api_key=claude_api_key)

    now=datetime.now().strftime("%Y年%m月%d日 %H:%M")
    st.markdown(
        f'<div class="app-footer">'
        f'観光AR案内アプリ フェーズ7 ／ 播磨・関西エリア<br>'
        f'Wikipedia API：CC BY-SA ／ AI生成コンテンツには免責表記を付与しています<br>'
        f'最終更新：{now} ／ v18 Phase7'
        f'</div>', unsafe_allow_html=True)

if __name__=="__main__":
    main()
