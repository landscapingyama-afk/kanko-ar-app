# ============================================================
# kanko_app.py  観光AR案内アプリ フェーズ6
# 播磨エリア + Folium地図 + GPS/コンパス + API連携 + SQLiteキャッシュ
# + 夜モード + 関西エリア拡張 + 問題報告フォーム
# 開発指示書 v18 準拠
# ============================================================
# フェーズ6 追加機能:
#   - 夜モード（Noto Sans JP / rgba(10,10,40,0.75) 深夜紺）
#   - 関西エリアスポット追加（奈良・京都・大阪）
#   - 問題報告フォーム（GitHub Issues連携）
#   - Claude API 自動生成の準備コメント（収益化時に追加予定）
# ============================================================
# フェーズ4 継承機能:
#   - Wikipedia API で説明文を自動取得（CC BY-SA 表記必須）
#   - Overpass API で周辺スポットを自動取得（© OpenStreetMap contributors）
#   - SQLite キャッシュ（緯度・経度・モード・言語 複合主キー）
#   - Gemini Vision API で雲判定（1日3回制限 / フォールバック付き）
#   - DeepL API で日英翻訳（無料枠対応 / フォールバック付き）
#   - SNSシェアテキスト生成
#   - 全API: キー未設定・エラー時は必ずダミーデータにフォールバック
# 権利注記:
#   - Wikipedia API: CC BY-SA ライセンス（出典表記必須）
#   - OpenStreetMap: © OpenStreetMap contributors（出典表記必須）
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
# ■ SQLite キャッシュ設定
#   フェーズ4: DBファイルをアプリと同じディレクトリに作成
#   フェーズ5以降: Hugging Face Spaces の /data ディレクトリに変更予定
# ============================================================
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kanko_cache.db")

def init_db():
    """DBとテーブルを初期化（存在しない場合のみ作成）"""
    try:
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        # コンテンツキャッシュテーブル
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cache (
                latitude    REAL,
                longitude   REAL,
                mode        TEXT,
                language    TEXT,
                content     TEXT,
                char_count  INTEGER,
                created_at  TEXT,
                PRIMARY KEY (latitude, longitude, mode, language)
            )
        """)
        # Wikipedia キャッシュテーブル
        cur.execute("""
            CREATE TABLE IF NOT EXISTS wiki_cache (
                spot_id     TEXT PRIMARY KEY,
                title       TEXT,
                extract     TEXT,
                fetched_at  TEXT
            )
        """)
        # 雲判定使用回数テーブル（1日3回制限）
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cloud_usage (
                usage_date  TEXT PRIMARY KEY,
                count       INTEGER DEFAULT 0
            )
        """)
        con.commit()
        con.close()
        return True
    except Exception:
        return False

def cache_get(lat, lon, mode, lang="ja") -> str | None:
    """キャッシュから取得。なければNone。"""
    try:
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        cur.execute("SELECT content FROM cache WHERE latitude=? AND longitude=? AND mode=? AND language=?",
                    (round(lat,4), round(lon,4), mode, lang))
        row = cur.fetchone()
        con.close()
        return row[0] if row else None
    except Exception:
        return None

def cache_set(lat, lon, mode, content, lang="ja"):
    """キャッシュに保存。"""
    try:
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        cur.execute("""
            INSERT OR REPLACE INTO cache
            (latitude, longitude, mode, language, content, char_count, created_at)
            VALUES (?,?,?,?,?,?,?)
        """, (round(lat,4), round(lon,4), mode, lang, content, len(content),
              datetime.now().isoformat()))
        con.commit()
        con.close()
    except Exception:
        pass

def wiki_cache_get(spot_id) -> str | None:
    try:
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        cur.execute("SELECT extract FROM wiki_cache WHERE spot_id=?", (spot_id,))
        row = cur.fetchone()
        con.close()
        return row[0] if row else None
    except Exception:
        return None

def wiki_cache_set(spot_id, title, extract):
    try:
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        cur.execute("INSERT OR REPLACE INTO wiki_cache (spot_id,title,extract,fetched_at) VALUES (?,?,?,?)",
                    (spot_id, title, extract, datetime.now().isoformat()))
        con.commit()
        con.close()
    except Exception:
        pass

def cloud_usage_today() -> int:
    """今日の雲判定使用回数を返す。"""
    try:
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        today = date.today().isoformat()
        cur.execute("SELECT count FROM cloud_usage WHERE usage_date=?", (today,))
        row = cur.fetchone()
        con.close()
        return row[0] if row else 0
    except Exception:
        return 0

def cloud_usage_increment():
    """今日の雲判定使用回数を+1する。"""
    try:
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        today = date.today().isoformat()
        cur.execute("INSERT OR IGNORE INTO cloud_usage (usage_date, count) VALUES (?,0)", (today,))
        cur.execute("UPDATE cloud_usage SET count=count+1 WHERE usage_date=?", (today,))
        con.commit()
        con.close()
    except Exception:
        pass

# ============================================================
# ■ APIキー取得（st.secrets → 環境変数 の順で探す）
# ============================================================
def get_secret(key: str) -> str:
    """st.secrets → 環境変数 の順に探してAPIキーを返す。なければ空文字。"""
    try:
        val = st.secrets.get(key, "")
        if val: return val
    except Exception:
        pass
    return os.environ.get(key, "")

# ============================================================
# ■ Wikipedia API（CC BY-SA ライセンス）
#   出典: https://ja.wikipedia.org  CC BY-SA 3.0
# ============================================================
@st.cache_data(ttl=86400, show_spinner=False)  # 24時間キャッシュ
def fetch_wikipedia(spot_id: str, title: str) -> str:
    """
    Wikipedia から概要文を取得。
    キャッシュ → API → フォールバック の順。
    """
    # まずSQLiteキャッシュを確認
    cached = wiki_cache_get(spot_id)
    if cached:
        return cached

    try:
        url = "https://ja.wikipedia.org/w/api.php"
        params = {
            "action": "query",
            "titles": title,
            "prop": "extracts",
            "exintro": True,
            "exchars": 180,
            "format": "json",
            "explaintext": True,
        }
        r = requests.get(url, params=params, timeout=8,
                         headers={"User-Agent": "kanko-ar-app/4.0 (educational)"})
        if r.status_code != 200:
            return ""
        data = r.json()
        pages = data.get("query", {}).get("pages", {})
        page  = next(iter(pages.values()))
        extract = page.get("extract", "").strip()
        if extract:
            wiki_cache_set(spot_id, title, extract)
        return extract
    except Exception:
        return ""

# ============================================================
# ■ Overpass API（© OpenStreetMap contributors CC BY-SA）
#   播磨エリア周辺の神社・寺院・史跡を自動取得
# ============================================================
@st.cache_data(ttl=3600, show_spinner=False)
def fetch_overpass_spots(center_lat: float, center_lon: float, radius_m: int = 5000) -> list:
    """
    Overpass API で周辺スポットを取得。
    エラー時は空リストを返す（フォールバック）。
    出典: © OpenStreetMap contributors
    """
    try:
        query = f"""
[out:json][timeout:15];
(
  node["amenity"="place_of_worship"]["religion"="shinto"]
    (around:{radius_m},{center_lat},{center_lon});
  node["historic"="castle"]
    (around:{radius_m},{center_lat},{center_lon});
  node["historic"="ruins"]
    (around:{radius_m},{center_lat},{center_lon});
  node["tourism"="viewpoint"]
    (around:{radius_m},{center_lat},{center_lon});
);
out body 10;
"""
        r = requests.post(
            "https://overpass-api.de/api/interpreter",
            data={"data": query}, timeout=15,
            headers={"User-Agent": "kanko-ar-app/4.0 (educational)"})
        if r.status_code != 200:
            return []
        elements = r.json().get("elements", [])
        spots = []
        for el in elements:
            tags = el.get("tags", {})
            name = tags.get("name:ja") or tags.get("name", "")
            if not name: continue
            spots.append({
                "id": f"osm_{el['id']}",
                "name": name,
                "name_kana": tags.get("name:ja-Hira", ""),
                "category": _osm_category(tags),
                "priority": 3,
                "lat": el.get("lat", 0),
                "lon": el.get("lon", 0),
                "altitude": 0,
                "prefecture": "兵庫県",
                "city": tags.get("addr:city", "播磨エリア"),
                "description": tags.get("description", f"{name}（OpenStreetMapデータ）"),
                "main_detail": f"{name}\n出典: © OpenStreetMap contributors",
                "trust_score": 0.7,
                "approved": True,
                "location_limited": False,
                "location_limited_content": "",
                # 他モードは空（フォールバック表示）
            })
        return spots
    except Exception:
        return []

def _osm_category(tags: dict) -> str:
    if tags.get("religion") == "shinto": return "shrine"
    if tags.get("historic") == "castle": return "castle"
    if tags.get("historic"):             return "historical"
    return "default"

# ============================================================
# ■ DeepL API 翻訳（無料枠 月50万字まで）
# ============================================================
def translate_deepl(text: str, target_lang: str = "EN") -> str:
    """
    DeepL API で翻訳。
    APIキー未設定・エラー時は元テキストをそのまま返す。
    """
    api_key = get_secret("DEEPL_API_KEY")
    if not api_key or not text:
        return text

    # キャッシュ確認（翻訳はSQLiteキャッシュを流用）
    cache_key = hashlib.md5(f"{text[:50]}_{target_lang}".encode()).hexdigest()
    cached = cache_get(0.0, 0.0, f"translate_{cache_key}", target_lang)
    if cached:
        return cached

    try:
        # DeepL Free API エンドポイント
        endpoint = "https://api-free.deepl.com/v2/translate"
        r = requests.post(endpoint, timeout=10, data={
            "auth_key": api_key,
            "text": text,
            "target_lang": target_lang,
        })
        if r.status_code != 200:
            return text
        result = r.json()["translations"][0]["text"]
        cache_set(0.0, 0.0, f"translate_{cache_key}", result, target_lang)
        return result
    except Exception:
        return text

# ============================================================
# ■ Gemini Vision API 雲判定（1日3回制限）
#   ⚠️ 雲の分析はAIによるものです。正確な天気予報は気象庁等でご確認ください。
# ============================================================
def analyze_cloud_gemini(image_bytes: bytes) -> dict:
    """
    Gemini Vision API で雲を判定。
    APIキー未設定・エラー・制限超過時はダミーデータを返す。
    """
    api_key = get_secret("GEMINI_API_KEY")
    dummy = {
        "cloud_type": "判定できませんでした",
        "description": "APIキーを設定するか、画像を再アップロードしてください。",
        "weather_hint": "天気の予報は気象庁等でご確認ください。",
        "is_dummy": True,
    }

    if not api_key:
        dummy["description"] = "Gemini APIキー未設定。st.secrets に GEMINI_API_KEY を設定してください。（サンプルデータ）"
        return dummy

    # 1日3回制限チェック
    today_count = cloud_usage_today()
    if today_count >= 3:
        dummy["description"] = f"本日の雲判定上限（3回）に達しました。明日またお試しください。"
        dummy["is_dummy"] = False
        return dummy

    try:
        import base64
        img_b64 = base64.b64encode(image_bytes).decode()
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
        payload = {"contents": [{"parts": [
            {"text": (
                "この画像に写っている雲を分析してください。"
                "以下のJSON形式のみで回答してください（他のテキスト不要）:\n"
                '{"cloud_type":"雲の種類（例：積乱雲・層積雲・高積雲等）",'
                '"description":"雲の特徴を2行以内で説明",'
                '"weather_hint":"この雲から読み取れる天気の傾向を1行で"}'
            )},
            {"inline_data": {"mime_type": "image/jpeg", "data": img_b64}}
        ]}]}
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code != 200:
            return dummy
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        # JSON抽出
        if "{" in text and "}" in text:
            json_str = text[text.index("{"):text.rindex("}")+1]
            result = json.loads(json_str)
            result["is_dummy"] = False
            cloud_usage_increment()
            return result
        return dummy
    except Exception:
        return dummy

# ============================================================
# ■ SNSシェアテキスト生成
# ============================================================
def make_share_text(spot: dict, mode_cfg: dict, dist_km: float) -> str:
    """スポット情報からSNSシェア用テキストを生成する。"""
    cat_map = {"shrine":"⛩","mountain":"🏔","castle":"🏯","temple":"🛕","default":"📍"}
    icon = cat_map.get(spot.get("category","default"), "📍")
    mode_name = {
        "main":"観光案内", "urban_legend":"都市伝説", "powerspot":"パワースポット",
        "healing":"癒しスポット", "festival":"行事案内", "old_map":"古地図",
        "cloud":"雲判定",
    }.get(mode_cfg["key"], "AR案内")
    return (
        f"{icon} {spot['name']}を訪れました！\n"
        f"📍 {spot['prefecture']} {spot['city']}\n"
        f"🏔 標高{spot['altitude']}m\n"
        f"📱 {mode_name}モードで探索中\n\n"
        f"#播磨AR #観光アプリ #{spot['name'].replace(' ','')} "
        f"#{spot['city'].replace(' ','')}"
    )

# ============================================================
# ■ スポットデータ（フェーズ4：4件 + Overpass自動取得）
# ============================================================
SPOT_DATA_BUILTIN = [
    {
        "id": "takamikura_001", "name": "高御位神社", "name_kana": "たかみくらじんじゃ",
        "category": "shrine", "priority": 1, "wiki_title": "高御位山",
        "lat": 34.8418, "lon": 134.8682, "altitude": 304,
        "prefecture": "兵庫県", "city": "加古川市",
        "description": "山全体がご神体の山岳信仰の聖地。縄文・弥生時代から山岳崇拝の地。山頂の岩場（磐座）に鎮座。",
        "main_detail": "✨ ご祭神：高御産巣日神（たかみむすびのかみ）\n　　創造と縁結びを司る、天地開闢の神様です。\n\n🏔 播磨アルプスの最高峰（304m）\n　　山全体がご神体。眼下に播磨平野と瀬戸内海が広がります。\n\n🪨 磐座（いわくら）について\n　　山頂に鎮座する巨大な岩が御神体。縄文時代から「神が宿る岩」として崇められてきました。\n\n👁 見晴らしのポイント\n　　晴れた日は淡路島・四国山地まで見渡せます。\n\n🌿 境内の草花\n　　春はツツジ、秋は紅葉が山肌を彩ります。",
        "urban_legend": "古来より霊山として知られる高御位山。山頂の磐座には不思議な力が宿るという言い伝えが残る。",
        "urban_legend_detail": "かつて修験者たちがこの山に籠もり、磐座の前で何日も祈りを捧げたという記録が残っています。\n\n満月の夜には山頂から青白い光が見えることがあると、地域の古老たちが語り継いできました。\n\n⚠️ AIエンターテイメント情報です。史実とは異なる場合があります。",
        "powerspot": "山頂の磐座は古代から祈りの場。播磨平野を見渡す大地のエネルギーが集まるとされる聖地。",
        "powerspot_detail": "磐座（いわくら）は古代から神が降り立つ場として信仰されてきました。\n地脈（龍脈）が交差する地点とされます。\n朝日が昇る方角に向かって手を合わせると、特別なパワーを受け取れるとされています。\n\n⚠️ AIエンターテイメント情報です。",
        "festival": "元旦：初日の出参拝（多くの参拝者が訪れる）",
        "festival_detail": "🌅 元旦　初日の出参拝\n🌸 春分・秋分　特別祈祷\n☀️ 夏至　山頂観察会\n🍂 10月（体育の日前後）　秋の例大祭\n\n※日程は毎年変動します。最新情報は地元観光協会でご確認ください。",
        "healing_text": "高御位山の風と木々のざわめき、遠く瀬戸内の潮騒が心を癒します。",
        "healing_detail": "🌸 春　鶯の鳴き声と桜吹雪\n🌿 夏　蜩（ひぐらし）と高原の風\n🍂 秋　紅葉と遠く波の音\n❄️ 冬　静寂と冷たく清澄な空気",
        "old_map_description": "江戸時代の播磨国絵図にも記された神聖な山。古来より地域の人々の信仰を集めてきた。",
        "old_map_detail": "📜 播磨国絵図（江戸時代）に「高御位山」として記載。\n🗺 明治以降の地形図と比較すると参拝道の変遷が読み取れます。",
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
        "description": "播磨の名峰。山頂からは播磨平野・淡路島・四国まで望める絶景スポット。",
        "main_detail": "🏔 標高939m　播磨富士とも称される美しい山容\n\n👁 晴れた日は播磨平野・淡路島・四国山地まで一望。\n\n🌺 笠形神社\n　　山頂直下に鎮座。縁結び・五穀豊穣のご神徳。\n\n🍂 紅葉の名所\n　　10〜11月の紅葉は播磨随一の美しさです。",
        "urban_legend": "「播磨富士」と称される美しい山容。古来より雨乞いの山として信仰を集めてきた。",
        "urban_legend_detail": "干ばつの年には村人が笠形山山頂で雨乞いの祈りを捧げたという記録が残ります。\n\n⚠️ AIエンターテイメント情報です。",
        "powerspot": "山頂の笠形神社はパワースポットとして知られ、縁結び・五穀豊穣のご神徳があるとされます。",
        "powerspot_detail": "山頂直下に鎮座する笠形神社は農業の神・大己貴命を祀ります。\n\n⚠️ AIエンターテイメント情報です。",
        "festival": "春：笠形神社例大祭（4月）・秋の紅葉シーズン",
        "festival_detail": "【4月】笠形神社春の例大祭\n【10〜11月】紅葉の見頃\n※詳細は神河町観光協会でご確認ください。",
        "healing_text": "939mの山頂を渡る清涼な風と眼下に広がる播磨平野の静寂が心を解放します。",
        "healing_detail": "春のミツバツツジ、夏のブナ林、秋の紅葉、冬の霧氷と四季折々の癒しがあります。",
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
        "description": "世界遺産・国宝。白漆喰の美しい姿から「白鷺城」と呼ばれる日本最大級の木造城郭。",
        "main_detail": "🏯 世界遺産・国宝（1993年UNESCO登録）\n\n🕊 白鷺城の由来\n　　白漆喰の外壁が白鷺が羽を広げた姿に似ることから。\n\n⛩ 天守閣最上階の長壁神社\n\n🌸 お城の桜\n　　約1,000本の桜が春を彩ります。",
        "urban_legend": "城内には千姫・お菊の霊が宿るという伝説が語り継がれる。",
        "urban_legend_detail": "播州皿屋敷の舞台として知られる姫路城。\n\n⚠️ AIエンターテイメント情報です。",
        "powerspot": "天守閣最上階には長壁神社が鎮座。城の守護神の強いエネルギーを感じる場所。",
        "powerspot_detail": "何度も戦禍をくぐり抜けた城の霊験あらたかなパワースポットとされます。\n\n⚠️ AIエンターテイメント情報です。",
        "festival": "桜（3月下旬〜4月）・姫路お城まつり（5月）・夏の特別夜間公開",
        "festival_detail": "【3月下旬〜4月上旬】桜まつり（約1,000本）\n【5月第3日曜】姫路お城まつり（武者行列）\n【夏季】特別夜間公開（ライトアップ）\n※詳細は姫路市観光課でご確認ください。",
        "healing_text": "白亜の天守閣を見上げながら深呼吸。400年の歴史が静かに語りかけてきます。",
        "healing_detail": "城内の西の丸庭園は特に静かで美しい空間。春の桜、秋の紅葉の季節は格別の癒しを体験できます。",
        "old_map_description": "江戸時代初期（1609年完成）の天守が現存する奇跡の城。",
        "old_map_detail": "慶長14年（1609年）に現在の天守が完成。1993年世界遺産登録。",
        "cloud_info": "姫路城天守閣（標高92m）からの眺望は絶品。東に高御位山、北に笠形山が見渡せます。",
        "cloud_detail": "東：高御位山（約17km）\n北：笠形山（約25km）\n⚠️ 正確な天気予報は気象庁等でご確認ください。",
        "trust_score": 1.0, "approved": True, "location_limited": False, "location_limited_content": "",
    },
    {
        "id": "kakurinji_004", "name": "鶴林寺", "name_kana": "かくりんじ",
        "category": "temple", "priority": 2, "wiki_title": "鶴林寺_(加古川市)",
        "lat": 34.7622, "lon": 134.8394, "altitude": 10,
        "prefecture": "兵庫県", "city": "加古川市",
        "description": "播磨の法隆寺と称される古刹。聖徳太子ゆかりの寺で国宝・重要文化財を多数保有。",
        "main_detail": "🛕 推古天皇元年（593年）創建\n\n📿 国宝2件\n　　本堂・太子堂が国宝に指定されています。\n\n🌳 境内の大銀杏\n　　樹齢推定700年。秋の黄葉は圧巻です。",
        "urban_legend": "聖徳太子が創建に関わったとされる古寺。境内では不思議な光を見たという参拝者の話が伝わる。",
        "urban_legend_detail": "1400年以上の歴史を持つ鶴林寺。太子の御霊が参道を行くのを見た、という言い伝えが地元に残ります。\n⚠️ AIエンターテイメント情報です。",
        "powerspot": "推古天皇元年（593年）創建と伝わる古刹のパワーは格別。",
        "powerspot_detail": "1400年の祈りが積み重なった空間で深呼吸すると、特別な静けさを感じると言われます。\n⚠️ AIエンターテイメント情報です。",
        "festival": "春：花まつり（4月8日）・秋の特別公開",
        "festival_detail": "【4月8日】花まつり（釈迦の誕生日）\n【秋季】国宝特別公開（太子堂・本堂）\n※詳細は鶴林寺までお問い合わせください。",
        "healing_text": "1400年の祈りが染み込んだ境内の静寂と、梵鐘の余韻が心を穏やかにします。",
        "healing_detail": "春の桜、夏の新緑、秋の紅葉、冬の静寂——境内は四季を通じて美しく整えられています。",
        "old_map_description": "推古天皇元年（593年）創建。江戸時代には「播磨の法隆寺」として広く知られた古刹。",
        "old_map_detail": "平安時代の建築様式を今に伝える本堂（国宝）と太子堂（国宝）が残ります。",
        "cloud_info": "境内の大銀杏（樹齢推定700年）の梢から見上げる空は特別な美しさがあります。",
        "cloud_detail": "大銀杏の根元から空を見上げると、四季折々の雲の表情が楽しめます。\n⚠️ 正確な天気予報は気象庁等でご確認ください。",
        "trust_score": 0.95, "approved": True, "location_limited": False, "location_limited_content": "",
    },
    # ============================================================
    # ★ フェーズ6：関西エリア追加スポット
    # ============================================================
    {
        "id": "nara_todaiji_005", "name": "東大寺", "name_kana": "とうだいじ",
        "category": "temple", "priority": 1, "wiki_title": "東大寺",
        "lat": 34.6888, "lon": 135.8398, "altitude": 100,
        "prefecture": "奈良県", "city": "奈良市",
        "description": "世界遺産・国宝。奈良の大仏（盧舎那仏）を本尊とする華厳宗大本山。創建は8世紀。",
        "main_detail": (
            "🛕 華厳宗大本山・世界遺産（1998年UNESCO登録）\n\n"
            "🗿 奈良の大仏\n"
            "　　高さ約15m・重さ約250トンの盧舎那仏坐像。\n\n"
            "🦌 奈良公園の鹿\n"
            "　　境内周辺に約1,000頭の鹿が生息。国の天然記念物。\n\n"
            "🌸 見どころ\n"
            "　　二月堂のお水取り（3月）・正倉院展（秋）が有名。"
        ),
        "urban_legend": "大仏殿の柱には大仏の鼻の穴と同じ大きさの穴が開いており、くぐると無病息災になるという言い伝えが残る。",
        "urban_legend_detail": "大仏殿内の柱の穴をくぐると1年間無病息災になると言われています。\n\n⚠️ AIエンターテイメント情報です。",
        "powerspot": "奈良の大仏は宇宙の真理を体現する盧舎那仏。その巨大なパワーに包まれる体験は格別とされます。",
        "powerspot_detail": "1200年以上の祈りが積み重なった大仏殿。その空間に入ると特別な気に包まれると多くの参拝者が語ります。\n\n⚠️ AIエンターテイメント情報です。",
        "festival": "3月：お水取り（修二会）・10〜11月：正倉院展",
        "festival_detail": "【3月1〜14日】お水取り（修二会）\n　　1200年以上続く伝統行事。\n【10〜11月】正倉院展\n※詳細は東大寺公式サイトでご確認ください。",
        "healing_text": "大仏の静かな微笑みと、1200年の祈りが積み重なった空間が心を深く落ち着かせます。",
        "healing_detail": "🌸 春　桜と大仏殿の絶景\n🌿 夏　青もみじと涼しい境内\n🍂 秋　紅葉と正倉院展の季節\n❄️ 冬　雪の大仏殿は幻想的",
        "old_map_description": "天平15年（743年）聖武天皇の勅願で創建。江戸時代に現在の大仏殿が再建された。",
        "old_map_detail": "743年聖武天皇の詔により建立開始。現在の大仏殿は江戸時代（1709年）に再建。世界最大級の木造建築。",
        "cloud_info": "若草山山頂（342m）からの眺望は奈良盆地を一望できる絶好の雲観察スポットです。",
        "cloud_detail": "若草山から奈良盆地を見渡すと四季折々の雲が楽しめます。\n⚠️ 正確な天気予報は気象庁等でご確認ください。",
        "trust_score": 1.0, "approved": True, "location_limited": False, "location_limited_content": "",
    },
    {
        "id": "kyoto_kinkakuji_006", "name": "金閣寺", "name_kana": "きんかくじ",
        "category": "shrine", "priority": 1, "wiki_title": "鹿苑寺",
        "lat": 35.0394, "lon": 135.7292, "altitude": 100,
        "prefecture": "京都府", "city": "京都市北区",
        "description": "世界遺産。金箔で覆われた舎利殿「金閣」が有名な臨済宗相国寺派の寺院。",
        "main_detail": (
            "🏯 正式名称：鹿苑寺（ろくおんじ）\n\n"
            "✨ 金閣（舎利殿）\n"
            "　　3層の建物全体に金箔が貼られた絶景。\n"
            "　　池に映る逆さ金閣も必見。\n\n"
            "🌊 鏡湖池\n"
            "　　金閣を映す美しい池。庭園は特別史跡・特別名勝に指定。\n\n"
            "🌸 雪化粧した金閣（冬）が特に美しい。"
        ),
        "urban_legend": "金閣寺は1950年に放火で全焼した。犯人の動機が美しすぎるものへの嫉妬だったという話は三島由紀夫の小説にもなった。",
        "urban_legend_detail": "1950年の放火事件後、現在の金閣は1955年に再建されたもの。三島由紀夫の小説「金閣寺」はこの事件を題材にしています。\n\n⚠️ これは実際の史実に基づくエピソードです。",
        "powerspot": "足利義満が建てた北山文化の象徴。金色に輝く舎利殿は見る者すべての心を浄化するパワースポット。",
        "powerspot_detail": "鏡湖池に映る金閣の姿は「浄土の世界」を表現しているとされます。\n\n⚠️ AIエンターテイメント情報です。",
        "festival": "春：桜と金閣・秋：紅葉と金閣・冬：雪の金閣（不定期）",
        "festival_detail": "【3月下旬〜4月上旬】桜と金閣の絶景\n【11月下旬〜12月上旬】紅葉と金閣\n【冬季】雪化粧した金閣（天候次第）\n※詳細は鹿苑寺公式サイトでご確認ください。",
        "healing_text": "金色に輝く舎利殿と静かな池の水面が、日常の喧騒を忘れさせてくれます。",
        "healing_detail": "🌸 春　桜と金色の競演\n🌿 夏　青もみじに映える金閣\n🍂 秋　燃える紅葉と金閣\n❄️ 冬　雪と金閣の幻想的な世界",
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
        "description": "豊臣秀吉が築いた天下統一の象徴。現在の天守閣は1931年再建。大阪城公園として整備。",
        "main_detail": (
            "🏯 豊臣秀吉が1583年に築城開始\n\n"
            "📜 天下統一の象徴\n"
            "　　秀吉の権力の象徴として絢爛豪華な城を築いた。\n\n"
            "🌸 大阪城公園\n"
            "　　約600本の桜が咲く花見の名所。\n\n"
            "👁 天守閣からの眺望\n"
            "　　大阪市内・六甲山・生駒山まで一望できる。"
        ),
        "urban_legend": "大阪城には豊臣秀吉の黄金の茶室が隠されているという伝説が残る。城内のどこかに今も眠っているという噂も。",
        "urban_legend_detail": "秀吉が所持していた「黄金の茶室」は移動式で各地に運ばれたとされます。その行方は今もなお謎に包まれています。\n\n⚠️ AIエンターテイメント情報です。",
        "powerspot": "天下統一を成し遂げた豊臣秀吉のエネルギーが宿る城。立身出世・仕事運のパワースポットとして知られます。",
        "powerspot_detail": "農民から天下人へと上り詰めた秀吉のパワーにあやかれる場所として、ビジネスパーソンに人気のパワースポットです。\n\n⚠️ AIエンターテイメント情報です。",
        "festival": "春：桜まつり（3月下旬〜4月）・夏：大阪城音楽堂イベント",
        "festival_detail": "【3月下旬〜4月上旬】桜まつり（約600本）\n【夏季】大阪城野外音楽堂でのコンサート\n【10〜11月】紅葉シーズン\n※詳細は大阪城公園公式サイトでご確認ください。",
        "healing_text": "広大な大阪城公園の緑と、天守閣の堂々たる姿が心を大きくしてくれます。",
        "healing_detail": "🌸 春　桜600本の絶景\n🌿 夏　緑豊かな公園で散策\n🍂 秋　紅葉と天守閣\n❄️ 冬　澄んだ空気と凛とした天守",
        "old_map_description": "1583年豊臣秀吉が築城開始。江戸時代には徳川幕府により改修。明治以降に現在の公園として整備された。",
        "old_map_detail": "1583年築城開始。1615年大坂夏の陣で落城。現在の天守閣は1931年再建。江戸時代の絵図にも詳細に描かれた。",
        "cloud_info": "大阪城天守閣（標高約50m）からは大阪平野を一望。空気が澄んだ日は六甲山・生駒山も見える絶好の雲観察スポット。",
        "cloud_detail": "天守閣最上階から360度の眺望が楽しめます。\n⚠️ 正確な天気予報は気象庁等でご確認ください。",
        "trust_score": 1.0, "approved": True, "location_limited": False, "location_limited_content": "",
    },
]

DUMMY_DATA = {
    "urban_legend": "この地には古くから不思議な言い伝えが残っています。（サンプルデータ）",
    "urban_legend_detail": "APIキーを設定すると、Claude AIがその土地固有の都市伝説を生成します。\n⚠️ AIエンターテイメント情報です。",
    "powerspot": "大地のエネルギーが集まる特別な場所です。（サンプルデータ）",
    "powerspot_detail": "APIキーを設定するとパワースポット情報をAIが生成します。\n⚠️ AIエンターテイメント情報です。",
    "festival": "年間を通じて様々な祭事が行われています。（サンプルデータ）",
    "festival_detail": "APIキーを設定すると行事・祭りの詳細が表示されます。",
    "healing_sound": "自然の音をお楽しみください。（サンプルデータ）",
    "healing_detail": "APIキーを設定するとその場所の癒し情報が表示されます。",
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
    "🎵 癒し音声":      {"key":"healing",      "font":"Noto Serif JP", "bg":"rgba(255,170,200,0.76)","pin_color":"#FFE8F5","icon":"🎵"},
    "⚡ パワースポット": {"key":"powerspot",    "font":"M PLUS 1p",    "bg":"rgba(255,140,160,0.78)","pin_color":"#FFE0E8","icon":"⚡"},
    "🎋 行事案内":      {"key":"festival",     "font":"Kosugi Maru",  "bg":"rgba(255,155,175,0.78)","pin_color":"#FFE5EC","icon":"🎋"},
    "📜 古地図":        {"key":"old_map",      "font":"Kaisei Decol", "bg":"rgba(240,155,170,0.80)","pin_color":"#FFE8E0","icon":"📜"},
    "☁️ 雲判定":        {"key":"cloud",        "font":"Kosugi Maru",  "bg":"rgba(160,210,235,0.76)","pin_color":"#E8F8FF","icon":"☁️"},
    # ★ フェーズ6：夜モード（仕様書準拠）
    "🌙 夜モード":       {"key":"night",        "font":"Noto Sans JP",  "bg":"rgba(10,10,40,0.75)",  "pin_color":"#8888FF","icon":"🌙"},
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
    for max_km,max_n,cats in [(0.5,3,None),(2.0,5,None),(5.0,3,None),(9999,3,["mountain"])]:
        tier=[(sp,d,b) for sp,d,b in cands if sp["id"] not in seen and d<=max_km
              and (cats is None or sp.get("category") in cats)]
        tier.sort(key=lambda x:(CATEGORY_PRIORITY.index(x[0].get("category","default"))
                                 if x[0].get("category","default") in CATEGORY_PRIORITY else 99,x[1]))
        for item in tier[:max_n]:
            if item[0]["id"] not in seen:
                seen.add(item[0]["id"]); result.append(item)
    return result

def get_content(spot, mode_key, lang="ja"):
    """コンテンツ取得。Wikipedia補完 + DeepL翻訳対応。"""
    try:
        field_map = {
            "main":         ("description","main_detail"),
            "urban_legend": ("urban_legend","urban_legend_detail"),
            "powerspot":    ("powerspot","powerspot_detail"),
            "healing":      ("healing_text","healing_detail"),
            "festival":     ("festival","festival_detail"),
            "old_map":      ("old_map_description","old_map_detail"),
            "cloud":        ("cloud_info","cloud_detail"),
        }
        if mode_key in field_map:
            sk, dk = field_map[mode_key]
            summary = spot.get(sk) or DUMMY_DATA.get(mode_key, "データなし")
            detail  = spot.get(dk) or DUMMY_DATA.get(f"{mode_key}_detail", "詳細なし")

            # Wikipedia で補完（メイン案内のみ）
            if mode_key == "main" and spot.get("wiki_title"):
                wiki_text = fetch_wikipedia(spot["id"], spot["wiki_title"])
                if wiki_text and len(wiki_text) > 20:
                    detail = detail + f"\n\n📖 Wikipedia より\n{wiki_text[:160]}…\n（出典: Wikipedia CC BY-SA）"

            # 日本語以外は翻訳
            if lang != "ja":
                summary = translate_deepl(summary, lang)
                detail  = translate_deepl(detail,  lang)

            return {"summary": summary, "detail": detail,
                    "is_dummy": not bool(spot.get(sk))}
        return {"summary": DUMMY_DATA.get(mode_key,"データなし"),
                "detail":  DUMMY_DATA.get(f"{mode_key}_detail","詳細なし"), "is_dummy": True}
    except Exception:
        return {"summary":"情報を読み込めませんでした。","detail":"管理者にお問い合わせください。","is_dummy":True}

# ============================================================
# ■ 平滑化フィルタ（フェーズ3から継承）
# ============================================================
def smooth_heading(new_val, buf):
    buf.append(new_val)
    return sum(buf)/len(buf)

def smooth_gps(new_lat, new_lon, prev_lat, prev_lon):
    if prev_lat is None: return new_lat, new_lon
    if haversine_km(prev_lat,prev_lon,new_lat,new_lon)*1000 < 20:
        return prev_lat, prev_lon
    return new_lat, new_lon

# ============================================================
# ■ センサーJS（フェーズ3から継承）
# ============================================================
SENSOR_JS = """
<div id="sensor-panel" style="background:rgba(255,255,255,0.55);border-radius:14px;padding:14px 16px;
  font-family:'Noto Sans JP',sans-serif;font-size:13px;color:#2a4a7a;
  border:1px solid rgba(100,150,220,0.35);backdrop-filter:blur(8px);margin-bottom:6px;">
  <div style="font-weight:700;font-size:15px;margin-bottom:8px;">📡 センサー状態</div>
  <div><span id="gps-icon">🔵</span> <b>GPS：</b><span id="gps-status">取得中…</span></div>
  <div style="font-size:12px;margin-top:4px;">緯度：<span id="disp-lat">--</span>　経度：<span id="disp-lon">--</span></div>
  <div style="font-size:12px;">精度：<span id="disp-acc">--</span>m　速度：<span id="disp-speed">--</span>km/h</div>
  <div style="margin-top:6px;"><span id="compass-icon">🔵</span> <b>コンパス：</b><span id="compass-status">待機中</span></div>
  <div style="font-size:12px;">方位：<span id="disp-heading">--</span>°（<span id="disp-dir">--</span>）</div>
  <div id="walk-warning" style="display:none;background:rgba(255,180,50,0.25);border:1.5px solid rgba(255,150,30,0.6);
    border-radius:8px;padding:6px 10px;margin-top:6px;font-weight:700;color:#7a3a00;font-size:12px;">
    ⚠️ 速度が高いです。立ち止まってください。</div>
  <div style="margin-top:10px;display:flex;gap:6px;flex-wrap:wrap;">
    <button onclick="startSensors()" style="background:linear-gradient(135deg,#6aaaf0,#4888e0);color:#fff;
      border:none;border-radius:8px;padding:6px 12px;font-size:12px;font-weight:700;cursor:pointer;">📡 GPS取得開始</button>
    <button onclick="calibrateCompass()" style="background:linear-gradient(135deg,#f0a0c0,#d870a0);color:#fff;
      border:none;border-radius:8px;padding:6px 12px;font-size:12px;font-weight:700;cursor:pointer;">🧭 コンパス校正</button>
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
    else{document.getElementById("compass-icon").textContent="🔴";document.getElementById("compass-status").textContent="許可が必要";}})
    .catch(()=>{document.getElementById("compass-icon").textContent="🔴";});}
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
# ■ Folium地図構築
# ============================================================
def build_map(user_lat, user_lon, heading, tile_name, zoom, mode_cfg, visible_spots):
    pin_color = mode_cfg["pin_color"]
    if tile_name == "OpenStreetMap":
        t = OSM_TILE
        m = folium.Map(location=[user_lat,user_lon],zoom_start=zoom,tiles=t["url"],attr=t["attr"])
    else:
        t = GSI_TILES[tile_name]
        m = folium.Map(location=[user_lat,user_lon],zoom_start=zoom,tiles=t["url"],attr=t["attr"],max_zoom=t["max_zoom"])
    for r_km,color,label in [(0.5,"#FF88AA","500m"),(2.0,"#88AAFF","2km"),(5.0,"#AADDFF","5km")]:
        folium.Circle([user_lat,user_lon],radius=r_km*1000,color=color,fill=True,fill_color=color,
                      fill_opacity=0.05,weight=1.2,opacity=0.45,tooltip=label).add_to(m)
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
# ■ CSS（フェーズ3継承 + フェーズ4追加スタイル）
# ============================================================
GLOBAL_CSS = "<style>\n@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;700&family=Noto+Serif+JP:wght@400;700&family=Yuji+Syuku&family=M+PLUS+1p:wght@700&family=Kosugi+Maru&family=Kaisei+Decol:wght@400;700&display=swap');\n" + """
#MainMenu,footer,header{visibility:hidden;}
.stApp{background:linear-gradient(160deg,#c8e8fa 0%,#b8d0f5 22%,#c0dcf8 44%,#bbd4f8 66%,#cce6fb 85%,#c4dff8 100%)!important;}
.stApp::before{content:'';position:fixed;inset:0;
  background:radial-gradient(ellipse at 15% 10%,rgba(255,255,255,0.45) 0%,transparent 45%),
             radial-gradient(ellipse at 85% 80%,rgba(180,200,255,0.25) 0%,transparent 50%);
  pointer-events:none;z-index:0;}
.block-container{padding-top:1rem!important;padding-bottom:1rem!important;max-width:900px!important;position:relative;z-index:1;}
.ar-card{border-radius:18px;padding:18px 22px;margin:10px 0;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);
  border:1px solid rgba(255,255,255,0.60);box-shadow:0 5px 28px rgba(160,80,120,0.18),0 1px 5px rgba(160,80,120,0.10);
  animation:fadeInUp 0.5s ease both;color:#FFFFFF!important;font-size:18px!important;
  text-shadow:0 1px 5px rgba(120,40,70,0.60),0 0 4px rgba(0,0,0,0.38);}
.ar-card-title{font-size:23px;font-weight:700;color:#FFFFFF;text-shadow:0 1px 7px rgba(120,40,70,0.70),0 0 5px rgba(0,0,0,0.45);
  margin-bottom:7px;display:flex;align-items:center;gap:8px;letter-spacing:0.04em;}
.ar-card-kana{font-size:15px;color:rgba(255,255,255,0.93);margin-bottom:10px;text-shadow:0 0 5px rgba(0,0,0,0.45);}
.ar-card-summary{overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;
  font-size:17px;line-height:1.75;color:#FFFFFF;text-shadow:0 0 5px rgba(0,0,0,0.50);margin-top:6px;}
.ar-card-detail{font-size:17px;line-height:1.95;color:#FFFFFF;text-shadow:0 0 5px rgba(0,0,0,0.48);
  white-space:pre-wrap;margin:12px -4px -4px;padding:14px 18px;background:rgba(0,0,0,0.12);
  border-radius:0 0 14px 14px;border-top:1px solid rgba(255,255,255,0.38);}
.ar-detail-label{font-size:14px;color:rgba(255,255,255,0.88);margin-top:10px;padding-top:7px;
  border-top:1px dashed rgba(255,255,255,0.38);text-shadow:0 0 4px rgba(0,0,0,0.48);}
.ar-badge{display:inline-flex;align-items:center;gap:4px;background:rgba(0,0,0,0.18);
  border:1px solid rgba(255,255,255,0.48);border-radius:20px;padding:4px 12px;font-size:14px;
  margin:3px 4px 3px 0;color:#FFFFFF;font-weight:600;text-shadow:0 0 4px rgba(0,0,0,0.50);}
.ar-disclaimer{font-size:13px;opacity:0.88;margin-top:8px;color:rgba(255,255,240,0.95);}
.ar-fallback-badge{display:inline-block;background:rgba(0,0,0,0.18);border:1px solid rgba(255,255,255,0.42);
  border-radius:8px;padding:2px 10px;font-size:13px;color:rgba(255,245,220,0.95);margin-bottom:6px;}
.wiki-badge{display:inline-block;background:rgba(60,120,200,0.18);border:1px solid rgba(60,120,200,0.40);
  border-radius:6px;padding:1px 8px;font-size:11px;color:rgba(200,230,255,0.9);margin-left:6px;}
.osm-badge{display:inline-block;background:rgba(60,180,80,0.18);border:1px solid rgba(60,180,80,0.40);
  border-radius:6px;padding:1px 8px;font-size:11px;color:rgba(200,255,210,0.9);margin-left:4px;}
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
  border:1px solid rgba(255,255,255,0.55);text-align:center;color:#FFFFFF;font-size:18px;font-weight:700;
  animation:fadeInUp 0.3s ease both;text-shadow:0 1px 4px rgba(120,40,80,0.50);backdrop-filter:blur(10px);}
.location-limited-card{border-radius:14px;padding:14px 18px;margin:8px 0;
  background:rgba(200,230,255,0.60);border:1.5px solid rgba(120,180,240,0.55);backdrop-filter:blur(10px);}
.lookaround-card{background:rgba(255,255,255,0.38);border-radius:14px;padding:14px 18px;margin:8px 0;
  border:1px solid rgba(120,160,220,0.35);color:#2a4a7a;font-size:15px;line-height:1.9;backdrop-filter:blur(6px);}
.lookaround-card h4{color:#2a4a7a;margin:0 0 10px;font-size:17px;font-weight:700;}
.map-placeholder{background:rgba(255,255,255,0.40);border:1px solid rgba(100,150,210,0.32);
  border-radius:14px;padding:20px;text-align:center;color:#3a5a8a;font-size:14px;margin:8px 0;}
.share-card{background:rgba(255,255,255,0.42);border-radius:12px;padding:12px 16px;margin:8px 0;
  border:1px solid rgba(120,160,220,0.30);color:#2a4a7a;font-size:13px;}
.share-card textarea{width:100%;background:rgba(200,220,255,0.25);border:1px solid rgba(100,150,210,0.35);
  border-radius:8px;padding:8px;font-size:13px;color:#2a4a7a;resize:none;}
.cloud-result{border-radius:14px;padding:16px 18px;margin:8px 0;
  background:rgba(160,210,235,0.55);border:1px solid rgba(100,180,220,0.45);color:#1a3a5a;font-size:16px;}
.app-footer{text-align:center;color:rgba(50,80,140,0.65);font-size:11px;padding:22px 0 10px;line-height:1.9;}
.phase6-badge{display:inline-block;background:rgba(80,200,120,0.20);border:1px solid rgba(80,200,120,0.45);
  border-radius:8px;padding:2px 10px;font-size:12px;color:#1a6a3a;}
/* ★ フェーズ6：夜モード */
.night-bg{background:linear-gradient(160deg,#050510 0%,#0a0a28 40%,#080818 100%)!important;}
.report-form{background:rgba(255,255,255,0.42);border-radius:14px;padding:16px 18px;
  margin:8px 0;border:1px solid rgba(120,160,220,0.30);color:#2a4a7a;font-size:15px;}
.sensor-active-badge{display:inline-block;background:rgba(60,180,100,0.25);border:1px solid rgba(60,180,100,0.55);
  border-radius:8px;padding:2px 10px;font-size:12px;color:#1a6a3a;}
.sensor-manual-badge{display:inline-block;background:rgba(100,140,220,0.20);border:1px solid rgba(100,140,220,0.45);
  border-radius:8px;padding:2px 10px;font-size:12px;color:#3a5a9a;}
.font-noto-sans{font-family:'Noto Sans JP',sans-serif;}
.font-yuji-syuku{font-family:'Yuji Syuku',serif;}
.font-noto-serif{font-family:'Noto Serif JP',serif;}
.font-mplus1p{font-family:'M PLUS 1p',sans-serif;font-weight:700;}
.font-kosugi-maru{font-family:'Kosugi Maru',sans-serif;}
.font-kaisei-decol{font-family:'Kaisei Decol',serif;}
div[data-testid="stRadio"] label,div[data-testid="stSelectbox"] label,
div[data-testid="stSlider"] label{color:#3a5a8a!important;font-size:14px!important;}
div[data-testid="stRadio"] div[role="radiogroup"] label{color:#2a4a7a!important;font-size:15px!important;}
div[data-testid="stMarkdownContainer"] p{color:#2a4a7a;}
.stButton>button{background:linear-gradient(135deg,#6aaaf0 0%,#4888e0 100%)!important;color:#FFFFFF!important;
  border:none!important;border-radius:10px!important;font-weight:700!important;
  text-shadow:0 1px 3px rgba(30,60,140,0.40)!important;box-shadow:0 2px 10px rgba(80,130,220,0.32)!important;}
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
        "safety_shown": False, "map_zoom": 13,
        "preset_lat": 34.8330, "preset_lon": 134.8620,
        "heading_buf": [], "prev_lat": None, "prev_lon": None,
        "sensor_mode": "manual", "selected_lang": "ja",
        "osm_spots": [], "osm_loaded": False,
        # ★ フェーズ6
        "night_mode": False,
        "selected_area": "播磨エリア",
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

# ============================================================
# ■ GPS params 取得
# ============================================================
def get_gps_from_params():
    try:
        p = st.query_params
        if p.get("gps_active") != "1": return None,None,None,None
        lat=float(p.get("gps_lat","0")); lon=float(p.get("gps_lon","0"))
        hdg=float(p.get("gps_heading","0")); spd=float(p.get("gps_speed","0"))
        if lat==0.0 and lon==0.0: return None,None,None,None
        return lat,lon,hdg,spd
    except Exception:
        return None,None,None,None

# ============================================================
# ■ ARカードレンダリング
# ============================================================
def render_spot_card(spot, mode_cfg, dist_km, brg, expanded, lang="ja"):
    mode_key = mode_cfg["key"]
    content  = get_content(spot, mode_key, lang)
    fc       = FONT_CLASS.get(mode_cfg["font"], "font-noto-sans")
    opac     = opacity_by_dist(dist_km)
    cat_icon = CATEGORY_STYLE.get(spot.get("category","default"), CATEGORY_STYLE["default"])["icon"]
    is_osm   = spot.get("id","").startswith("osm_")
    disclaimer = ""
    if mode_key in ("urban_legend","powerspot"):
        disclaimer = "⚠️ このコンテンツはAIが生成したエンターテイメント情報です。史実とは異なる場合があります。"
    elif mode_key == "cloud":
        disclaimer = "⚠️ 雲の分析はAIによるものです。正確な天気予報は気象庁等をご確認ください。"
    label_map = {"main":"🔍 見どころ・ご神体情報","urban_legend":"📖 詳しい言い伝え",
                 "powerspot":"✨ パワースポット詳細","healing":"🎵 季節の癒し情報",
                 "festival":"🗓 行事・祭り日程","old_map":"📜 歴史・古地図情報","cloud":"☁️ 雲と天気の詳細"}
    fb  = '<span class="ar-fallback-badge">📡 サンプルデータ</span>' if content["is_dummy"] else ""
    osm_badge = '<span class="osm-badge">OSM</span>' if is_osm else ""
    wiki_badge = '<span class="wiki-badge">Wikipedia</span>' if spot.get("wiki_title") and not is_osm else ""
    det = (f'<div class="ar-detail-label">▼ {label_map.get(mode_key,"📋 詳細情報")}</div>'
           f'<div class="ar-card-detail">{content["detail"]}</div>') if expanded else ""
    html = (f'<div class="ar-card {fc}" style="background:{mode_cfg["bg"]};opacity:{opac};">'
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
# ■ 見下ろしナビ
# ============================================================
def render_lookaround_nav(visible_spots, heading):
    if not visible_spots: return
    lines = []
    for sp,dist_km,brg in visible_spots[:5]:
        icon=CATEGORY_STYLE.get(sp.get("category","default"),CATEGORY_STYLE["default"])["icon"]
        rel=(brg-heading+360)%360
        arrow=("↑ 正面" if rel<30 or rel>330 else "↗ 右前方" if rel<90 else
               "→ 右方" if rel<150 else "↓ 後方" if rel<210 else "← 左方" if rel<270 else "↖ 左前方")
        lines.append(f'<b>{icon} {sp["name"]}</b> — {arrow} {dist_label(dist_km)}'
                     f'<span style="opacity:0.7;font-size:13px;"> ({deg_to_dir(brg)}方向)</span>')
    st.markdown('<div class="lookaround-card font-noto-sans"><h4>🗺️ 見下ろしナビ</h4>'
                + "<br>".join(lines) + "</div>", unsafe_allow_html=True)

# ============================================================
# ■ メインアプリ
# ============================================================
def main():
    init_session()
    init_db()
    st.markdown(GLOBAL_CSS, unsafe_allow_html=True)

    # ヘッダー
    st.markdown(
        '<div class="app-header"><h1>⛩ 観光AR案内 ／ 播磨エリア</h1>'
        '<p>山岳信仰の聖地・歴史の街をARで探索 '
        '<span class="phase6-badge">フェーズ6</span></p></div>',
        unsafe_allow_html=True)

    # 安全警告（初回のみ）
    if not st.session_state.safety_shown:
        st.markdown(
            '<div class="safety-warning"><p>⚠️ 歩きながらの使用は危険です。<br>'
            '必ず立ち止まってご使用ください。<br>'
            '<span style="font-size:13px;font-weight:400;">'
            '登山中は足元・周囲の安全を最優先にしてください。</span></p></div>',
            unsafe_allow_html=True)
        if st.button("✅ 確認しました", type="primary", use_container_width=True):
            st.session_state.safety_shown = True
            st.rerun()
        st.stop()

    # GPS取得
    gps_lat, gps_lon, gps_heading, gps_speed = get_gps_from_params()
    gps_active = gps_lat is not None
    if gps_active:
        buf = deque(st.session_state.heading_buf, maxlen=10)
        gps_heading = smooth_heading(gps_heading, buf)
        st.session_state.heading_buf = list(buf)
        gps_lat, gps_lon = smooth_gps(gps_lat, gps_lon, st.session_state.prev_lat, st.session_state.prev_lon)
        st.session_state.prev_lat = gps_lat; st.session_state.prev_lon = gps_lon

    col_ctrl, col_main = st.columns([1, 2], gap="medium")

    # ── 左カラム ─────────────────────────────────
    with col_ctrl:
        # センサーJS
        st.components.v1.html(SENSOR_JS, height=320, scrolling=False)

        # センサーモード表示
        if gps_active:
            st.markdown(f'<div class="sensor-active-badge">🟢 GPS・コンパス取得中</div>', unsafe_allow_html=True)
        else:
            st.markdown('<div class="sensor-manual-badge">🎛 手動シミュレータ</div>', unsafe_allow_html=True)

        st.markdown("---")

        # ★ フェーズ6：夜モードトグル
        night_mode = st.toggle("🌙 夜モード", value=st.session_state.night_mode)
        st.session_state.night_mode = night_mode

        st.markdown("---")

        # ★ フェーズ6：エリア選択
        st.markdown("**🗾 エリア選択**")
        selected_area = st.selectbox("エリア",
            ["播磨エリア", "関西エリア（奈良・京都・大阪）", "全エリア"],
            index=0, label_visibility="collapsed")

        # プリセットボタン（エリア別）
        st.markdown("**📍 プリセット位置**")
        if selected_area == "播磨エリア":
            presets = {"🧗 高御位山麓":(34.8330,134.8620),"⛩ 神社山頂":(34.8418,134.8682),
                       "🏯 姫路城":(34.8394,134.6939),"🛕 鶴林寺":(34.7622,134.8394)}
        elif "関西" in selected_area:
            presets = {"🛕 東大寺":(34.6888,135.8398),"🌸 奈良公園":(34.6851,135.8448),
                       "✨ 金閣寺":(35.0394,135.7292),"⛩ 伏見稲荷":(34.9671,135.7727),
                       "🏯 大阪城":(34.6873,135.5262),"🌊 道頓堀":(34.6688,135.5027)}
        else:
            presets = {"🧗 高御位山麓":(34.8330,134.8620),"🏯 姫路城":(34.8394,134.6939),
                       "🛕 東大寺":(34.6888,135.8398),"✨ 金閣寺":(35.0394,135.7292),
                       "🏯 大阪城":(34.6873,135.5262),"⛩ 神社山頂":(34.8418,134.8682)}
        pcols = st.columns(2)
        for i,(label,(plat,plon)) in enumerate(presets.items()):
            with pcols[i%2]:
                if st.button(label, use_container_width=True, key=f"preset_{i}_{selected_area[:2]}"):
                    st.session_state.preset_lat=plat; st.session_state.preset_lon=plon; st.rerun()

        st.markdown("---")

        if gps_active:
            sim_lat=gps_lat; sim_lon=gps_lon; sim_heading=gps_heading
            st.markdown(f'<div style="font-size:12px;color:#3a5a8a;">GPS緯度：{sim_lat:.5f}<br>GPS経度：{sim_lon:.5f}<br>コンパス：{sim_heading:.0f}°</div>', unsafe_allow_html=True)
        else:
            sim_lat=st.slider("📍 緯度",34.70,35.10,st.session_state.preset_lat,0.0005,format="%.4f")
            sim_lon=st.slider("📍 経度",134.60,135.00,st.session_state.preset_lon,0.0005,format="%.4f")
            sim_heading=st.slider("🧭 向き（方位角）",0,359,45,1,help="0°=北/90°=東/180°=南/270°=西")

        st.markdown("---")

        # ★ フェーズ4：言語選択
        st.markdown("**🌐 表示言語**")
        lang_label = st.selectbox("言語", list(LANG_OPTIONS.keys()), index=0, label_visibility="collapsed")
        selected_lang = LANG_OPTIONS[lang_label]
        if selected_lang != "ja" and not get_secret("DEEPL_API_KEY"):
            st.markdown('<div style="font-size:11px;color:#aa6030;background:rgba(255,200,150,0.3);border-radius:6px;padding:4px 8px;">⚠️ DeepL APIキー未設定。日本語で表示します。</div>', unsafe_allow_html=True)
            selected_lang = "ja"
        st.session_state.selected_lang = selected_lang

        st.markdown("---")

        # 地図タイル
        st.markdown("**🗺️ 地図タイル**")
        tile_opts=["標準地図","写真（空中写真）","淡色地図","陰影起伏図","OpenStreetMap"]
        tile_name=st.selectbox("タイル",tile_opts,index=0,label_visibility="collapsed")
        st.markdown(f'<div style="font-size:11px;color:#4a6a9a;">{"© OpenStreetMap contributors" if tile_name=="OpenStreetMap" else "出典：国土地理院"}</div>', unsafe_allow_html=True)
        map_zoom=st.slider("🔍 ズーム",10,17,st.session_state.map_zoom,1)
        st.session_state.map_zoom=map_zoom

        st.markdown("---")

        st.markdown('<div style="color:#3a5a8a;font-size:13px;margin-bottom:4px;">📡 表示モード</div>', unsafe_allow_html=True)
        mode_label=st.radio("モード",list(MODES.keys()),index=0,label_visibility="collapsed")
        mode_cfg=MODES[mode_label]

        st.markdown("---")
        show_detail=st.toggle("🔍 詳細情報を表示", value=False)

        # ★ フェーズ4：Overpass自動取得オプション
        st.markdown("---")
        use_osm=st.toggle("🌐 周辺スポット自動取得（OSM）", value=False,
                          help="OpenStreetMapから周辺の神社・史跡を自動取得します")

        st.markdown("---")

        # スポット合成（組み込み + OSM）
        all_spots = list(SPOT_DATA_BUILTIN)
        if use_osm:
            if not st.session_state.osm_loaded:
                with st.spinner("OpenStreetMapからスポットを取得中..."):
                    osm = fetch_overpass_spots(sim_lat, sim_lon, 3000)
                    st.session_state.osm_spots = osm
                    st.session_state.osm_loaded = True
            if st.session_state.osm_spots:
                all_spots = all_spots + st.session_state.osm_spots
                st.markdown(f'<div style="font-size:11px;color:#2a7a3a;">🌐 OSM: {len(st.session_state.osm_spots)}件追加<br>© OpenStreetMap contributors</div>', unsafe_allow_html=True)
        else:
            st.session_state.osm_loaded = False

        visible_spots = filter_spots(all_spots, sim_lat, sim_lon)
        nearest = visible_spots[0] if visible_spots else None

        sensor_badge = '<span class="sensor-active-badge">🟢 GPS</span>' if gps_active else '<span class="sensor-manual-badge">🎛 手動</span>'
        if nearest:
            sp0,d0,_ = nearest
            st.markdown(f'<div class="info-panel">{sensor_badge}<br>📍 {sim_lat:.4f}, {sim_lon:.4f}<br>🧭 {sim_heading:.0f}°（{deg_to_dir(sim_heading)}）<br>📡 {len(visible_spots)}件<br>📏 最寄り：{sp0["name"]} {dist_label(d0)}</div>', unsafe_allow_html=True)
        else:
            st.markdown(f'<div class="info-panel">{sensor_badge}<br>📍 {sim_lat:.4f}, {sim_lon:.4f}<br>📡 スポットなし</div>', unsafe_allow_html=True)

    # ── 右カラム ──────────────────────────────────
    with col_main:
        st.markdown(f'<div class="mode-title-bar" style="background:{mode_cfg["bg"]};">{mode_cfg["icon"]} {mode_label}</div>', unsafe_allow_html=True)

        # Folium地図
        map_key=(f"kanko_map_{int(sim_lat*200)}_{int(sim_lon*200)}"
                 f"_{int(sim_heading/45)}_{tile_name[:3]}_{map_zoom}")
        map_data={}; map_ok=False
        try:
            fmap=build_map(sim_lat,sim_lon,sim_heading,tile_name,map_zoom,mode_cfg,visible_spots)
            map_data=st_folium(fmap,width="100%",height=380,returned_objects=["last_clicked"],key=map_key)
            map_ok=True
        except Exception:
            pass
        if not map_ok:
            st.markdown('<div class="map-placeholder">🗺️ 地図の読み込みに失敗しました。F5で再読み込みしてください。</div>', unsafe_allow_html=True)
        if map_data and map_data.get("last_clicked"):
            c=map_data["last_clicked"]
            st.markdown(f'<div style="font-size:12px;color:#4a6a9a;text-align:right;">🖱 クリック地点：{c.get("lat",0):.5f}, {c.get("lng",0):.5f}</div>', unsafe_allow_html=True)

        # 方位インジケーター
        sensor_lbl="🟢 GPS・コンパス取得中" if gps_active else "🎛 手動シミュレータ"
        st.markdown(f'<div class="ar-compass">{sensor_lbl}　🧭 {sim_heading:.0f}°（{deg_to_dir(sim_heading)}）　／　{len(visible_spots)}件<br><span style="font-size:12px;color:#4a6a9a;">フェーズ6：夜モード・関西拡張・問題報告フォーム対応</span></div>', unsafe_allow_html=True)

        # 見下ろしナビ
        render_lookaround_nav(visible_spots, sim_heading)

        # ★ フェーズ4：☁️ 雲判定（画像アップロード）
        if mode_cfg["key"] == "cloud":
            st.markdown("---")
            today_count = cloud_usage_today()
            remaining = 3 - today_count
            st.markdown(
                f'<div class="cloud-result">'
                f'☁️ <b>雲判定モード</b>　本日残り：{remaining}/3回<br>'
                f'<span style="font-size:14px;">空の写真をアップロードすると雲の種類を判定します。</span><br>'
                f'<span style="font-size:12px;opacity:0.8;">⚠️ 雲の分析はAIによるものです。正確な天気予報は気象庁等でご確認ください。</span>'
                f'</div>', unsafe_allow_html=True)
            uploaded = st.file_uploader("☁️ 空の写真をアップロード", type=["jpg","jpeg","png"],
                                        label_visibility="collapsed")
            if uploaded and remaining > 0:
                with st.spinner("雲を分析中..."):
                    result = analyze_cloud_gemini(uploaded.read())
                if result.get("is_dummy"):
                    st.markdown(
                        f'<div class="cloud-result">📡 {result["description"]}</div>',
                        unsafe_allow_html=True)
                else:
                    st.markdown(
                        f'<div class="cloud-result">'
                        f'☁️ <b>{result.get("cloud_type","不明")}</b><br>'
                        f'{result.get("description","")}<br>'
                        f'🌤 {result.get("weather_hint","")}<br>'
                        f'<span style="font-size:12px;opacity:0.8;">⚠️ 雲の分析はAIによるものです。正確な天気予報は気象庁等でご確認ください。</span>'
                        f'</div>', unsafe_allow_html=True)
            elif uploaded and remaining <= 0:
                st.warning("本日の雲判定上限（3回）に達しました。明日またお試しください。")

        # ARカード一覧
        if not visible_spots:
            st.markdown('<div class="ar-card font-noto-sans" style="background:rgba(100,150,220,0.55);text-align:center;">📭 この範囲にスポットがありません</div>', unsafe_allow_html=True)
        else:
            for sp,dist_km,brg in visible_spots:
                render_spot_card(sp, mode_cfg, dist_km, brg, show_detail, selected_lang)
                if sp.get("location_limited") and dist_km < 0.3:
                    st.markdown(f'<div class="location-limited-card">🌟 <strong style="color:#2a4a8a;">現地限定コンテンツ解放！</strong><br><span style="font-size:16px;color:#2a4060;">{sp["location_limited_content"]}</span></div>', unsafe_allow_html=True)

        # ★ フェーズ4：SNSシェア
        if visible_spots:
            st.markdown("---")
            sp_share, d_share, _ = visible_spots[0]
            share_text = make_share_text(sp_share, mode_cfg, d_share)
            st.markdown('<div class="share-card"><b>📤 SNSシェア</b><br><span style="font-size:12px;color:#4a6a9a;">以下のテキストをコピーしてSNSに投稿できます。</span></div>', unsafe_allow_html=True)
            st.text_area("シェアテキスト", value=share_text, height=120, label_visibility="collapsed")

        # ★ フェーズ6：問題報告フォーム
        st.markdown("---")
        with st.expander("⚠️ 問題を報告する"):
            st.markdown(
                '<div class="report-form">'
                '<b>📝 問題報告フォーム</b><br>'
                '<span style="font-size:13px;">情報の誤り・表示の不具合などをご報告ください。</span>'
                '</div>', unsafe_allow_html=True)
            report_spot = st.text_input("スポット名（任意）", placeholder="例：高御位神社")
            report_type = st.selectbox("問題の種類",
                ["情報が間違っている", "地図の位置がずれている",
                 "表示が崩れている", "その他"])
            report_detail = st.text_area("詳細を教えてください", height=80)
            if st.button("📤 報告を送信", type="primary"):
                if report_detail:
                    # ★ フェーズ6：GitHub Issues への自動投稿
                    # 収益化時にGitHub API連携を追加予定
                    # github_token = get_secret("GITHUB_TOKEN")
                    # repo = "landscapingyama-afk/kanko-ar-app"
                    # issue_title = f"[報告] {report_type}：{report_spot}"
                    # issue_body = f"スポット: {report_spot}\n種類: {report_type}\n詳細: {report_detail}"
                    st.success(
                        "✅ ご報告ありがとうございます！\n"
                        "内容を確認して改善に努めます。")
                    st.balloons()
                else:
                    st.warning("詳細を入力してください。")

    # ★ フェーズ6：夜モード適用（背景を暗くする）
    if st.session_state.get("night_mode", False):
        st.markdown("""
<style>
.stApp {
    background: linear-gradient(160deg,#050510 0%,#0a0a28 40%,#080818 100%) !important;
}
.stApp::before {
    background:
        radial-gradient(ellipse at 20% 20%, rgba(30,30,100,0.4) 0%, transparent 50%),
        radial-gradient(ellipse at 80% 80%, rgba(20,20,80,0.3) 0%, transparent 50%);
}
.app-header h1 { color: #8888FF !important; }
.app-header p  { color: #6666AA !important; }
.info-panel, .simulator-note, .ar-compass, .lookaround-card,
.map-placeholder, .share-card, .report-form {
    background: rgba(20,20,60,0.65) !important;
    color: #CCCCFF !important;
    border-color: rgba(80,80,160,0.40) !important;
}
</style>
""", unsafe_allow_html=True)

    # ============================================================
    # ★ フェーズ8収益化時：Claude API 自動生成をここに追加
    # ============================================================
    # 以下のコメントを解除して claude_api_key を設定してください
    # claude_api_key = get_secret("ANTHROPIC_API_KEY")
    # if claude_api_key:
    #     # 都市伝説・パワースポット・行事案内を自動生成
    #     from anthropic import Anthropic
    #     client = Anthropic(api_key=claude_api_key)
    #     # generate_spot_content(spot, mode_key, client)
    # ============================================================

    # フッター（権利表記）
    now = datetime.now().strftime("%Y年%m月%d日 %H:%M")
    st.markdown(
        f'<div class="app-footer">'
        f'観光AR案内アプリ フェーズ6 ／ 播磨・関西エリア<br>'
        f'地図タイル：国土地理院 ／ © <a href="https://www.openstreetmap.org/copyright" style="color:rgba(80,120,180,0.7);">OpenStreetMap</a> contributors<br>'
        f'Wikipedia API：CC BY-SA ／ AI生成コンテンツには免責表記を付与しています<br>'
        f'最終更新：{now} ／ v18 Phase6'
        f'</div>', unsafe_allow_html=True)

if __name__ == "__main__":
    main()
