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

# ==================== INITIALIZATION ====================
app = Flask(__name__)

# Fetch the raw database environment string injected by Railway
db_url = os.environ.get("DATABASE_URL")

if db_url:
    # Auto-replace legacy postgres:// syntax with modern SQLAlchemy requirements
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
else:
    # 🛠️ HARDENED PROTECTION FIX: Check if running on Railway production vs local PC
    if os.environ.get("PORT"):
        # If a web port exists but DATABASE_URL is missing, force a visible system log error
        raise RuntimeError("CRITICAL ERROR: DATABASE_URL environment variable is missing on Railway! Please link your Postgres database plug-in to this web service.")
    else:
        # Fall back to SQLite strictly when executing locally on your desktop machine
        db_url = "sqlite:///users.db"

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Fallback to local SQLite database if cloud Postgres environment isn't specified
# app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get("DATABASE_URL", "sqlite:///users.db")
# app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
# db = SQLAlchemy(app)

NEWS_API_KEY = os.environ.get("NEWS_API_KEY", "YOUR_NEWSAPI_ORG_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY")
resend.api_key = os.environ.get("RESEND_API_KEY", "YOUR_RESEND_API_KEY")

# Mapping short database interest keys to robust NewsAPI query terms
INTEREST_MAP = {
    "taiwan_finance": "taiwan AND (market OR finance OR stock OR TSMC)",
    "taiwan_tech": "taiwan AND (AI OR semiconductor OR tech OR hardware)",
    "us_finance": "us AND (fed OR bonds OR dow OR nasdaq OR treasury)",
    "us_tech": "us AND (nvidia OR tech OR \"artificial intelligence\" OR software)",
    "wireless": "(5G OR 6G OR telecom OR wireless OR network)"
}

# ==================== DATABASE MODEL ====================
class UserProfile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    # Storing selected keys as a comma-separated string, e.g., "taiwan_tech,us_finance"
    interests = db.Column(db.String(300), nullable=False) 
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# Ensure database tables exist at start
with app.app_context():
    db.create_all()

# ==================== CORE PROCESSING ENGINE ====================
def fetch_category_news(query_string):
    """Fetches real articles using clean YYYY-MM-DD parameters for stability."""
    url = "https://newsapi.org/v2/everything"
    date_yesterday = (datetime.utcnow() - timedelta(days=1)).strftime('%Y-%m-%d')
    
    params = {
        "q": query_string,
        "sortBy": "relevancy",  
        "from": date_yesterday,
        "language": "en",
        "pageSize": 15,         
        "apiKey": NEWS_API_KEY
    }
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

def generate_personalized_html(user_email, user_interests_list, raw_news_payload):
    """Asks Gemini to construct an executive layout dynamically matching only selected interests."""
    client = genai.Client(api_key=GEMINI_API_KEY)
    
    # Dynamically structure instructions so Gemini knows which sections to render
    sections_requested = ", ".join(user_interests_list).upper()
    
    prompt = f"""
    You are an elite corporate intelligence compiler. Analyze the raw recent data feed provided below. 
    Your primary task is to critically evaluate these fresh entries and select ONLY the absolute top 3 most important news stories of the last 24 hours specifically corresponding to these requested categories: {sections_requested}.
    
    If an interest category is not part of the user's requested list ({sections_requested}), DO NOT include that section block inside the HTML response.

    Generate a clean HTML layout using modern inline CSS:
    <div style="background-color:#f8fafc; padding:30px 15px; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; color:#1e293b; max-width:650px; margin:0 auto; border-radius:12px;">
        <div style="border-bottom:2px solid #e2e8f0; padding-bottom:15px; margin-bottom:25px;">
            <h1 style="margin:0; font-size:22px; color:#0f172a; font-weight:800;">🌟 Your Personalized Intelligence Briefing</h1>
            <p style="margin:5px 0 0 0; font-size:13px; color:#64748b;">Custom tailored monitoring for: {user_email}</p>
        </div>

        <div style="background-color:#eff6ff; border-left:4px solid #3b82f6; padding:15px; margin-bottom:30px; border-radius:0 8px 8px 0;">
            <p style="margin:0; font-size:14px; line-height:1.6; color:#1e3a8a;"><strong>Executive Summary:</strong> [Insert 2 sentence overarching global summary based ONLY on selected topics in English]</p>
        </div>

        [Render individual section cards here dynamically matching user interests]
    </div>
    
    Omit all ```html markdown decorators. Output raw inner HTML string content.

    Raw Data Pool Feed:
    {raw_news_payload}
    """
    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash-lite',
            contents=prompt,
            config=types.GenerateContentConfig(thinking_config=types.ThinkingConfig(thinking_budget=0))
        )
        return response.text
    except Exception as e:
        return f"<h2>Error constructing dashboard layout</h2><p>{e}</p>"

def run_daily_newsletter_batch():
    """Loops through all users in the database, maps their interests, and sends unique tailored reports."""
    print(f"⏰ Initiating daily intelligence compilation cycle at {datetime.now()}")
    with app.app_context():
        users = UserProfile.query.all()
        if not users:
            print("📭 No registered users found in the configuration profile database.")
            return
            
        for user in users:
            print(f"🛰️ Processing customized data pipeline for: {user.email}")
            user_interests = [interest.strip() for interest in user.interests.split(",") if interest.strip()]
            
            # Step 1: Gather only the data streams this user cares about
            user_raw_feed = ""
            for interest in user_interests:
                if interest in INTEREST_MAP:
                    user_raw_feed += f"\n=== SECTION: {interest.upper()} ===\n"
                    user_raw_feed += fetch_category_news(INTEREST_MAP[interest]) + "\n"
            
            # Step 2: Have Gemini map the custom visual dashboard layout
            personalized_html = generate_personalized_html(user.email, user_interests, user_raw_feed)
            
            # Step 3: Dispatch unique payload via Resend API
            try:
                resend.Emails.send({
                    "from": "NewsEngine <onboarding@resend.dev>",
                    "to": [user.email],
                    "subject": "🌟 Your Adaptive Daily Executive Briefing",
                    "html": personalized_html
                })
                print(f"✅ Tailored brief dispatched successfully to {user.email}")
            except Exception as e:
                print(f"❌ Failed to email user {user.email}: {e}")

# ==================== WEB APP USER WEB INTERFACE ====================
HTML_PAGE_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Adaptive News Matrix Configurator</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body { font-family: -apple-system, system-ui, sans-serif; background: #f1f5f9; padding: 40px 20px; margin: 0; display: flex; justify-content: center; }
        .card { background: white; padding: 30px; border-radius: 12px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); max-width: 450px; width: 100%; }
        h2 { margin-top: 0; color: #0f172a; }
        label { display: block; font-weight: 600; margin: 15px 0 5px 0; color: #334155; }
        input[type="email"] { width: 100%; padding: 10px; border: 1px solid #cbd5e1; border-radius: 6px; box-sizing: border-box; font-size: 14px; }
        .checkbox-group { background: #f8fafc; padding: 12px; border-radius: 8px; border: 1px solid #e2e8f0; margin-bottom: 20px; }
        .checkbox-item { display: flex; align-items: center; margin: 10px 0; font-size: 14px; color: #475569; }
        .checkbox-item input { margin-right: 10px; transform: scale(1.1); }
        button { background: #2563eb; color: white; border: none; width: 100%; padding: 12px; border-radius: 6px; font-size: 15px; font-weight: 600; cursor: pointer; transition: background 0.2s; }
        button:hover { background: #1d4ed8; }
        .msg { margin-top: 15px; font-size: 14px; text-align: center; font-weight: bold; }
    </style>
</head>
<body>
    <div class="card">
        <h2>🛰️ Adaptive Matrix Briefing</h2>
        <p style="color: #64748b; font-size: 13px; margin-top:-10px;">Select your strategic subjects to build your personal automated daily newsletter.</p>
        
        <form id="configForm">
            <label for="email">Your Gmail Address:</label>
            <input type="email" id="email" required placeholder="example@gmail.com">
            
            <label>Select Your Subjects:</label>
            <div class="checkbox-group">
                <div class="checkbox-item"><input type="checkbox" name="interest" value="taiwan_finance"> 🇹🇼 Taiwan Market & Finance</div>
                <div class="checkbox-item"><input type="checkbox" name="interest" value="taiwan_tech"> 🇹🇼 Taiwan Tech Ecosystem</div>
                <div class="checkbox-item"><input type="checkbox" name="interest" value="us_finance"> 🇺🇸 United States Market & Finance</div>
                <div class="checkbox-item"><input type="checkbox" name="interest" value="us_tech"> 🇺🇸 United States Tech Innovation</div>
                <div class="checkbox-item"><input type="checkbox" name="interest" value="wireless"> 📡 Wireless Infrastructure (5G/6G)</div>
            </div>
            
            <button type="submit">Activate Delivery Stream</button>
        </form>
        <div id="responseMessage" class="msg"></div>
    </div>

    <script>
        document.getElementById('configForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const email = document.getElementById('email').value;
            const checkedBoxes = document.querySelectorAll('input[name="interest"]:checked');
            
            if(checkedBoxes.length === 0) {
                const msgDiv = document.getElementById('responseMessage');
                msgDiv.style.color = '#dc2626';
                msgDiv.innerText = "❌ Please select at least one interest subject.";
                return;
            }
            
            const interests = Array.from(checkedBoxes).map(cb => cb.value);
            
            try {
                const response = await fetch('/api/configure-profile', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ email, interests })
                });
                const result = await response.json();
                const msgDiv = document.getElementById('responseMessage');
                
                if(response.ok) {
                    msgDiv.style.color = '#16a34a';
                    msgDiv.innerText = "✅ Profile Active! Your custom briefing will run daily.";
                } else {
                    msgDiv.style.color = '#dc2626';
                    msgDiv.innerText = "❌ Error: " + result.message;
                }
            } catch (err) {
                document.getElementById('responseMessage').innerText = "❌ Network processing failure.";
            }
        });
    </script>
</body>
</html>
"""

@app.route('/')
def home():
    """Serves the front-end user control screen dashboard template."""
    return render_template_string(HTML_PAGE_TEMPLATE)

@app.route('/api/configure-profile', methods=['POST'])
def configure_profile():
    """Endpoint capturing JSON requests to create or update multi-user preference nodes."""
    data = request.get_json() or {}
    email = data.get('email', '').strip().lower()
    interests_list = data.get('interests', [])
    
    if not email or not interests_list:
        return jsonify({"status": "error", "message": "Valid email and interest payload required."}), 400
        
    interests_string = ",".join(interests_list)
    
    # Check if user already exists to update parameters, otherwise initialize clean profile row
    user = UserProfile.query.filter_by(email=email).first()
    if user:
        user.interests = interests_string
        print(f"🔄 Updated profile preference matrix for: {email}")
    else:
        user = UserProfile(email=email, interests=interests_string)
        db.session.add(user)
        print(f"🆕 Registered new intelligence pipeline node for: {email}")
        
    db.session.commit()
    return jsonify({"status": "success", "message": "Profile configuration successfully mapped."}), 200

# ==================== AUTOMATED CRON SCHEDULER ====================
scheduler = BackgroundScheduler()
# Schedules the multi-user script orchestration function to trigger cleanly once every 24 hours
scheduler.add_job(func=run_daily_newsletter_batch, trigger="interval", days=1)
scheduler.start()

if __name__ == "__main__":
    # Standard fallback port mapping for clean Railway service binding
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
