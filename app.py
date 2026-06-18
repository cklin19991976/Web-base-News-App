import os
import time
from datetime import datetime, timedelta
import requests
import resend
from flask import Flask, request, jsonify, render_template_string
from flask_sqlalchemy import SQLAlchemy
from apscheduler.schedulers.background import BackgroundScheduler
from google import genai
from google.genai import types
from google.genai.errors import APIError  # 🛠️ IMPORT THE API ERROR EXCEPTION LAYER

# ==================== INITIALIZATION ====================
app = Flask(__name__)

db_url = os.environ.get("DATABASE_URL")
if db_url:
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql+pg8000://", 1)
    elif db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+pg8000://", 1)
else:
    db_url = "sqlite:///users.db"

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

NEWS_API_KEY = os.environ.get("NEWS_API_KEY", "YOUR_NEWSAPI_ORG_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY")
resend.api_key = os.environ.get("RESEND_API_KEY", "YOUR_RESEND_API_KEY")

# ==================== DATABASE MODEL ====================
class UserProfile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    custom_subject = db.Column(db.String(300), nullable=False) 
    delivery_hour = db.Column(db.Integer, nullable=False, default=8) 
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

with app.app_context():
    db.create_all()

# ==================== NEWS AND AI LOGIC ENGINE ====================
def fetch_custom_news(user_query):
    """Fetches articles for a single topic string, adapting language rules automatically."""
    url = "https://newsapi.org/v2/everything"
    date_yesterday = (datetime.utcnow() - timedelta(days=1)).strftime('%Y-%m-%d')
    
    # 🛠️ THE FIX: Detect if the user typed Chinese characters
    # If the string contains Chinese, we drop the "en" restriction so it finds local Taiwanese tech media.
    contains_chinese = any('\u4e00' <= char <= '\u9fff' for char in user_query)
    
    params = {
        "q": user_query,         
        "sortBy": "relevancy",  
        "from": date_yesterday,
        "pageSize": 8,          # Slightly increased to give Gemini a better pool
        "apiKey": NEWS_API_KEY
    }
    
    # Only enforce English if the query string is pure English text
    if not contains_chinese:
        params["language"] = "en"
        
    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        data = response.json()
        articles = data.get("articles", [])
        
        formatted_news = []
        for art in articles:
            title = art.get("title", "")
            source = art.get("source", {}).get("name", "Unknown")
            description = art.get("description", "")
            article_url = art.get("url", "#")
            
            if title and title != "[Removed]":
                formatted_news.append(f"[{source}] {title}\nSummary: {description}\nLink: {article_url}")
                
        return "\n\n".join(formatted_news) if formatted_news else "No fresh articles found."
    except Exception as e:
        return f"Error gathering data: {e}"

def generate_single_subject_section_html(user_topic, raw_news_payload):
    """Asks Gemini to compile news stories, utilizing an exponential backoff wrapper for 429 safety."""
    client = genai.Client(api_key=GEMINI_API_KEY)
    
    prompt = f"""
    You are an elite corporate intelligence analyst. Analyze the raw recent data feed provided below.
    Your absolute mandate is to isolate EXACTLY the top 3 most important, high-impact news stories from the last 24 hours regarding this specific topic: "{user_topic}". 

    Generate a clean HTML fragment with NO outer body or html tags:
    <div style="margin-bottom: 25px; background: white; padding: 20px; border-radius: 8px; border: 1px solid #e2e8f0;">
        <h3 style="margin:0 0 12px 0; font-size:15px; color:#1e40af; text-transform: uppercase; letter-spacing: 0.05em; border-bottom: 2px solid #eff6ff; padding-bottom: 5px;">
            📊 Monitoring Target: {user_topic}
        </h3>
        <ul style="margin:0; padding-left:20px; font-size:14px; line-height:1.6; color:#334155;">
            [Format your chosen top 3 stories explicitly as <li> elements. For each story, provide a bold headline, a 2-sentence operational description, and a clean hyperlinked anchor tag link using the source URL. If the topic involves Taiwan or East Asian tech/finance markets, write the text for these bullet points in Traditional Chinese (繁體中文). Otherwise, write in English.]
        </ul>
    </div>

    Omit all markdown fence rules (like ```html). Output only raw inner HTML block code string.
    Raw Data Pool Feed for "{user_topic}":
    {raw_news_payload}
    """
    
    # 🛠️ THE PRODUCTION RESILIENCE GATE: Intelligent Retry Loop
    max_retries = 4
    base_delay = 3.0 # Start with a 3 second sleep if throttled
    
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt,
                config=types.GenerateContentConfig(thinking_config=types.ThinkingConfig(thinking_budget=0))
            )
            return response.text
        except Exception as e:
            error_str = str(e)
            # 🛠️ FIXED: Catch 429 (Rate Limits) AND 503 (Server Overloaded / High Demand)
            if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str or "503" in error_str or "UNAVAILABLE" in error_str:
                calculated_delay = base_delay * (2 ** attempt) # Wait 3s, then 6s, then 12s...
                print(f"⚠️ [Attempt {attempt + 1}/{max_retries}] Gemini traffic/demand wall detected ('{user_topic}'). Cooling down for {calculated_delay}s...")
                time.sleep(calculated_delay)
            else:
                # If it's a completely different error (like an invalid API key), fail immediately
                print(f"❌ Non-rate limit error hit: {e}")
                return f"<p>Error compiling news layout data: {e}</p>"
                
    # If all retries failed
    return f"<p>System skipped execution segment for '{user_topic}' due to high API demand congestion. Please retry shortly.</p>"

def generate_market_sidebar_html():
    """
    使用 yfinance 抓取即時行情與過去 6 個月的歷史收盤趨勢數據，
    並使用 QuickChart.io API 自動產出可嵌入郵件的動態折線圖，完全不消耗 Gemini 額度。
    """
    import yfinance as yf
    import json
    
    tickers = {
        "原油價格": "CL=F",
        "黃豆價格": "ZS=F",
        "黃金價格": "GC=F",
        "美元指數": "DX-Y.NYB"
    }
    
    sidebar_html = ""
    chart_datasets = []
    months_labels = []
    
    # 🎨 為每條折線配置雅致的企業圖表色彩
    colors = {
        "原油價格": "#f59e0b",  # 琥珀金
        "黃豆價格": "#84cc16",  # 草原綠
        "黃金價格": "#eab308",  # 亮金黃
        "美元指數": "#3b82f6"   # 科技藍
    }
    
    try:
        # 1. 循環抓取即時報價與歷史走勢數據
        for name, ticker in tickers.items():
            asset = yf.Ticker(ticker)
            
            # --- 抓取即時當日報價 ---
            todays_data = asset.history(period="1d")
            if not todays_data.empty:
                current_price = todays_data['Close'].iloc[-1]
                if name == "原油價格":
                    price_str = f"${current_price:.2f} / 桶"
                elif name == "黃豆價格":
                    price_str = f"${(current_price/100):.2f} / 英斗"
                elif name == "黃金價格":
                    price_str = f"${current_price:.2f} / 盎司"
                else:
                    price_str = f"{current_price:.2f} pts"
            else:
                price_str = "暫無即時報價"
                
            # 拼接即時行情文字面板
            sidebar_html += f"""
            <div style="background:#ffffff; padding:12px; margin-bottom:12px; border-radius:6px; border:1px solid #e2e8f0;">
                <div style="font-size:12px; color:#64748b; font-weight:600;">{name}</div>
                <div style="font-size:18px; color:#0f172a; font-weight:700; margin-top:2px;">{price_str}</div>
                <div style="font-size:11px; color:#94a3b8; margin-top:2px;">Yahoo Finance 即時數據</div>
            </div>
            """
            
            # --- 抓取 6 個月歷史趨勢數據 (按月採樣 '1mo') ---
            history_6m = asset.history(period="6mo", interval="1mo")
            if not history_6m.empty:
                # 僅提取收盤價數據，並進行基底歸一化或標準尺度百分比轉換，以防四大數據（2000點 vs 70點）放同張圖會變形扁平
                # 這裡使用 6 個月前的初始價格作為 100% 基準線計算「相對漲跌百分比趨勢」
                initial_price = history_6m['Close'].iloc[0]
                pct_trend = [round(((p - initial_price) / initial_price) * 100, 1) for p in history_6m['Close']]
                
                # 採集時間軸標籤 (格式化為 MM月)
                if not months_labels:
                    months_labels = [d.strftime('%m月') for d in history_6m.index]
                
                # 組裝 QuickChart dataset 線條屬性
                chart_datasets.append({
                    "label": name,
                    "data": pct_trend,
                    "borderColor": colors[name],
                    "backgroundColor": "transparent",
                    "fill": False,
                    "borderWidth": 2,
                    "pointRadius": 3
                })

        # 2. 當資料成功取得，使用 Chart.js 配置語法構造 QuickChart 圖像端點網址
        if chart_datasets:
            chart_config = {
                "type": "line",
                "data": {
                    "labels": months_labels,
                    "datasets": chart_datasets
                },
                "options": {
                    "title": {
                        "display": True,
                        "text": "近6個月宏觀資產相對漲跌幅趨勢 (%)",
                        "fontSize": 12,
                        "fontColor": "#334155"
                    },
                    "legend": {
                        "position": "bottom",
                        "labels": {"fontSize": 10, "boxWidth": 12}
                    },
                    "scales": {
                        "yAxes": [{
                            "ticks": {
                                "fontSize": 9,
                                "callback": "function(value){return value + '%';}"
                            },
                            "gridLines": {"color": "#f1f5f9"}
                        }],
                        "xAxes": [{"ticks": {"fontSize": 9}, "gridLines": {"display": False}}]
                    }
                }
            }
            
            # 將 Python 字典轉換成 JSON 字符串並進行 URL 編碼
            encoded_config = requests.utils.quote(json.dumps(chart_config))
            chart_url = f"https://quickchart.io/chart?c={encoded_config}&width=240&height=180"
            
            # 將生成的圖表嵌入至即時行情面板的最下方
            sidebar_html += f"""
            <div style="background:#ffffff; padding:10px; margin-top:15px; border-radius:6px; border:1px solid #e2e8f0; text-align:center;">
                <img src="{chart_url}" width="100%" style="max-width:240px; display:block; margin:0 auto; height:auto;" alt="六個月宏觀趨勢圖表" />
            </div>
            """
            
        return sidebar_html
        
    except Exception as e:
        print(f"❌ 圖表引擎生成失敗: {e}")
        return sidebar_html  # 降級保護：若發生異常，依然返回純即時數據面板，不讓整封信件因報錯而中斷。

def compile_master_email_body(user_email, topics_list):
    """Loops through every topic independently to guarantee a 3-news breakdown per subject with a market dashboard sidebar."""
    sections_html = ""
    
    for topic in topics_list:
        print(f"🔄 Processing independent micro-pipeline for subject element: {topic}")
        raw_news = fetch_custom_news(topic)
        sections_html += generate_single_subject_section_html(topic, raw_news)
        
        # Keep an underlying rhythm safety spacing
        time.sleep(1.0)
        
    print("📈 Fetching global macro commodities tracking telemetry matrix...")
    market_sidebar_html = generate_market_sidebar_html()
        
    master_wrapper = f"""
    <div style="background-color:#f1f5f9; padding:25px 10px; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; color:#1e293b;">
        <table align="center" border="0" cellpadding="0" cellspacing="0" width="100%" style="max-width:750px; background-color:#ffffff; border-radius:12px; overflow:hidden; border:1px solid #e2e8f0; border-collapse:collapse;">
            <tr>
                <td colspan="2" style="padding:25px 20px; border-bottom:2px solid #f1f5f9; background:#ffffff;">
                    <h1 style="margin:0; font-size:22px; color:#0f172a; font-weight:800;">🌟 Your Multi-Subject Matrix Briefing</h1>
                    <p style="margin:5px 0 0 0; font-size:13px; color:#64748b;">Custom tailored streams for: {user_email}</p>
                </td>
            </tr>
            <tr valign="top">
                <!-- LEFT COLUMN: NEWS TRACKING PANELS (65% WIDTH) -->
                <td width="65%" style="padding:20px 15px 20px 20px;">
                    {sections_html}
                </td>
                
                <!-- RIGHT COLUMN: COMMODITIES SIDEBAR WIDGET (35% WIDTH) -->
                <td width="35%" style="padding:20px 20px 20px 15px; background-color:#f8fafc; border-left:1px solid #e2e8f0;">
                    <h4 style="margin:0 0 15px 0; font-size:13px; color:#475569; text-transform:uppercase; letter-spacing:0.05em; border-bottom:2px solid #cbd5e1; padding-bottom:5px;">
                        📈 全球商品與金融數據
                    </h4>
                    {market_sidebar_html}
                </td>
            </tr>
            <tr>
                <td colspan="2" style="text-align: center; padding: 20px; font-size: 11px; color: #94a3b8; background-color:#f8fafc; border-top:1px solid #e2e8f0;">
                    Automated intelligence network node engine. To modify your subjects, re-submit the core web configuration portal form.
                </td>
            </tr>
        </table>
    </div>
    """
    return master_wrapper

# ==================== HOURLY BATCH COMPILER ====================
def run_hourly_newsletter_batch():
    current_utc_hour = datetime.utcnow().hour
    print(f"⏰ Hourly Cron awakened. Evaluating target queue for hour: {current_utc_hour}:00")
    
    with app.app_context():
        users = UserProfile.query.filter_by(delivery_hour=current_utc_hour).all()
        
        for user in users:
            topics = [t.strip() for t in user.custom_subject.split(",") if t.strip()]
            final_email_html = compile_master_email_body(user.email, topics)
            
            try:
                resend.Emails.send({
                    "from": "IntelBrief <briefing@newshighlights.online>", # Make sure this matches your verified domain!
                    "to": [user.email],
                    "subject": f"🌟 Strategic Briefing Matrix: {len(topics)} Tracked Subjects",
                    "html": final_email_html
                })
                print(f"✅ Multi-section report safely sent to {user.email}")
            except Exception as e:
                print(f"❌ Failed to email user {user.email}: {e}")

# ==================== WEB INTERFACE ====================
HTML_PAGE_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>智慧型多主題新聞搜尋</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body { font-family: -apple-system, system-ui, sans-serif; background: #f1f5f9; padding: 40px 20px; margin: 0; display: flex; justify-content: center; }
        .card { background: white; padding: 30px; border-radius: 12px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); max-width: 450px; width: 100%; }
        h2 { margin-top: 0; color: #0f172a; }
        label { display: block; font-weight: 600; margin: 15px 0 5px 0; color: #334155; }
        input[type="email"], input[type="text"], select { width: 100%; padding: 10px; border: 1px solid #cbd5e1; border-radius: 6px; box-sizing: border-box; font-size: 14px; background: white; }
        input:focus, select:focus { outline: none; border-color: #2563eb; box-shadow: 0 0 0 2px rgba(37,99,235,0.1); }
        button { background: #2563eb; color: white; border: none; width: 100%; padding: 12px; border-radius: 6px; font-size: 15px; font-weight: 600; cursor: pointer; margin-top: 20px; transition: background 0.2s; }
        button:hover { background: #1d4ed8; }
        .msg { margin-top: 15px; font-size: 14px; text-align: center; font-weight: bold; }
    </style>
</head>
<body>
    <div class="card">
        <h2>🛰️ 每日新聞搜尋引擎</h2>
        <p style="color: #64748b; font-size: 13px; margin-top:-10px;">請輸入多個您感興趣的新聞主題（以逗號隔開），系統每日將針對每個主題利用人工智慧選出3條最重要的新聞，並附帶即時大宗商品與金融指數發送至您的信箱。</p>
        
        <form id="configForm">
            <label for="email">您的電子郵件地址：</label>
            <input type="email" id="email" required placeholder="example@gmail.com">
            
            <label for="custom_subject">您想追蹤的新聞主題（多主題請用逗號隔開,中文和英文新聞主題都可以）：</label>
            <input type="text" id="custom_subject" required placeholder="例如：台積電, World cup, BLACKPINK">
            
            <label for="deliveryHour">每日派送時間 (UTC/台灣時區)：</label>
            <select id="deliveryHour">
                <option value="0">上午 00:00 (UTC) / 台灣 08:00 AM</option>
                <option value="2">上午 02:00 (UTC) / 台灣 10:00 AM</option>
                <option value="4">上午 04:00 (UTC) / 台灣 12:00 PM</option>
                <option value="6">上午 06:00 (UTC) / 台灣 02:00 PM</option>
                <option value="8" selected>上午 08:00 (UTC) / 台灣 04:00 PM - 預設</option>
                <option value="10">上午 10:00 (UTC) / 台灣 06:00 PM</option>
                <option value="12">下午 12:00 (UTC) / 台灣 08:00 PM</option>
                <option value="14">下午 02:00 (UTC) / 台灣 10:00 PM</option>
                <option value="16">下午 04:00 (UTC) / 台灣 12:00 AM</option>
                <option value="18">下午 06:00 (UTC) / 台灣 02:00 AM</option>
                <option value="20">下午 08:00 (UTC) / 台灣 04:00 AM</option>
                <option value="22">下午 10:00 (UTC) / 台灣 06:00 AM</option>
            </select>
            
            <button type="submit">啟用情報派送</button>
        </form>
        <div id="responseMessage" class="msg"></div>
    </div>

    <script>
        document.getElementById('configForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const email = document.getElementById('email').value.trim();
            const subject = document.getElementById('custom_subject').value.trim();
            const hour = document.getElementById('deliveryHour').value;
            const msgDiv = document.getElementById('responseMessage');
            
            try {
                const response = await fetch('/api/configure-profile', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ email, subject, hour })
                });
                
                if(response.ok) {
                    msgDiv.style.color = '#16a34a';
                    msgDiv.innerText = "✅ 訂閱設定成功！專屬的新聞簡報將每日按時發送。";
                } else {
                    const result = await response.json();
                    msgDiv.style.color = '#dc2626';
                    msgDiv.innerText = "❌ 設定失敗: " + result.message;
                }
            } catch (err) {
                msgDiv.style.color = '#dc2626';
                msgDiv.innerText = "❌ 網路傳輸處理異常，請稍後再試。";
            }
        });
    </script>
</body>
</html>
"""

@app.route('/')
def home():
    return render_template_string(HTML_PAGE_TEMPLATE)

@app.route('/api/configure-profile', methods=['POST'])
def configure_profile():
    data = request.get_json() or {}
    email = data.get('email', '').strip().lower()
    subject = data.get('subject', '').strip()
    chosen_hour = int(data.get('hour', 8))
    
    if not email or not subject:
        return jsonify({"status": "error", "message": "Parameters missing."}), 400
        
    user = UserProfile.query.filter_by(email=email).first()
    if user:
        user.custom_subject = subject
        user.delivery_hour = chosen_hour
    else:
        user = UserProfile(email=email, custom_subject=subject, delivery_hour=chosen_hour)
        db.session.add(user)
        
    db.session.commit()
    return jsonify({"status": "success"}), 200

# ==================== TEST ROUTE TRIGGER ====================
@app.route('/secret-test-trigger')
def secret_test_trigger():
    """Triggers execution instantly for rapid multi-subject validation."""
    try:
        with app.app_context():
            users = UserProfile.query.all()
            if not users:
                return jsonify({"status": "error", "message": "Sign up on homepage first."}), 400
                
            for user in users:
                topics = [t.strip() for t in user.custom_subject.split(",") if t.strip()]
                final_email_html = compile_master_email_body(user.email, topics)
                
                resend.Emails.send({
                    "from": "IntelBrief <briefing@newshighlights.online>", # Adjust to your custom domain!
                    "to": [user.email],
                    "subject": f"🔥 MULTI-SECTION TEST: {len(topics)} Subjects + Real-Time Sidebar",
                    "html": final_email_html
                })
        return jsonify({"status": "success", "message": "Multi-subject instant delivery engine complete!"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# ==================== AUTOMATED CRON SCHEDULER ====================
scheduler = BackgroundScheduler()
scheduler.add_job(func=run_hourly_newsletter_batch, trigger="cron", minute=0)
scheduler.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
