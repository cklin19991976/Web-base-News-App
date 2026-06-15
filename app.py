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

# Railway Database configuration string injection
db_url = os.environ.get("DATABASE_URL")
if db_url:
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql+pg8000://", 1)
    elif db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+pg8000://", 1)
else:
    if os.environ.get("PORT"):
        raise RuntimeError("CRITICAL ERROR: DATABASE_URL environment variable is missing on Railway!")
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
    # 🛠️ FEATURE UPDATE: Changed from fixed keys to storing raw user text input queries
    custom_subject = db.Column(db.String(200), nullable=False)
    # 🛠️ FEATURE UPDATE: Store the preferred hour (0 to 23 UTC)
    delivery_hour = db.Column(db.Integer, nullable=False, default=8) 
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

with app.app_context():
    # db.drop_all()   # <-- ADD THIS LINE TEMPORARILY
    db.create_all()

# ==================== NEWS AND AI LOGIC ENGINE ====================
def fetch_custom_news(user_query):
    """Fetches articles using the exact search phrase the user typed into the web UI."""
    url = "https://newsapi.org/v2/everything"
    date_yesterday = (datetime.utcnow() - timedelta(days=1)).strftime('%Y-%m-%d')
    
    params = {
        "q": user_query,         # Inject user's unique custom text string directly
        "sortBy": "relevancy",  
        "from": date_yesterday,
        "language": "en",
        "pageSize": 25,          # Fetch a wider batch so Gemini can cleanly choose the top 3
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
                
        return "\n\n".join(formatted_news) if formatted_news else "No fresh articles found for your query topic."
    except Exception as e:
        return f"Error gathering data: {e}"

def generate_personalized_html(user_email, user_topic, raw_news_payload):
    """Asks Gemini to force-rank and isolate the 3 most critical news stories from the custom text stream."""
    client = genai.Client(api_key=GEMINI_API_KEY)
    
    prompt = f"""
    You are an elite corporate intelligence compiler. Analyze the raw recent data feed provided below. 
    Your absolute priority task is to critically evaluate these fresh entries and CHOOSE ONLY the top 3 most important, breaking, high-impact news stories from the last 24 hours regarding the user's custom topic: "{user_topic}".

    Generate a clean HTML layout using modern inline CSS:
    <div style="background-color:#f8fafc; padding:30px 15px; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; color:#1e293b; max-width:650px; margin:0 auto; border-radius:12px;">
        <div style="border-bottom:2px solid #e2e8f0; padding-bottom:15px; margin-bottom:25px;">
            <h1 style="margin:0; font-size:22px; color:#0f172a; font-weight:800;">🌟 Your Custom Intelligence Briefing</h1>
            <p style="margin:5px 0 0 0; font-size:13px; color:#64748b;">Targeted monitoring topic: <strong>{user_topic}</strong></p>
        </div>
        <div style="background-color:#eff6ff; border-left:4px solid #3b82f6; padding:15px; margin-bottom:30px; border-radius:0 8px 8px 0;">
            <p style="margin:0; font-size:14px; line-height:1.6; color:#1e3a8a;"><strong>Executive Summary:</strong> [Insert a 2 sentence analytical briefing summarizing how these updates impact the overall landscape of {user_topic} in English]</p>
        </div>

        <h3 style="margin:10px 0 15px 0; font-size:16px; color:#334155; text-transform: uppercase; letter-spacing: 0.05em;">📈 Top 3 Strategic Developments</h3>
        <ul style="margin:0; padding-left:20px; font-size:14px; line-height:1.6; color:#334155;">
            [Format your chosen top 3 stories explicitly as list items. For each story, provide a strong bold headline title, a 2-sentence structural description of what happened, and a clean anchor tag link using the source URL. If the topic involves Taiwan or Chinese markets, write the bullet points in Traditional Chinese (繁體中文). Otherwise, write in English.]
        </ul>
    </div>
    
    Omit all ```html markdown decorators. Output only a raw inner HTML string.

    Raw Data Pool Feed for "{user_topic}":
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

# ==================== HOURLY BATCH COMPILER ====================
def run_hourly_newsletter_batch():
    """Runs automatically every hour. Selects and runs intelligence profiles matching this specific hour."""
    current_utc_hour = datetime.utcnow().hour
    print(f"⏰ Hourly Chron awakened. Checking UTC target queue for hour: {current_utc_hour}:00")
    
    with app.app_context():
        # Query database specifically for users scheduled to receive their emails at this exact hour
        users = UserProfile.query.filter_by(delivery_hour=current_utc_hour).all()
        
        if not users:
            print(f"📭 No user feeds scheduled for delivery inside the {current_utc_hour}:00 UTC block.")
            return
            
        for user in users:
            print(f"🛰️ Processing live pipeline custom feed for: {user.email} (Topic: {user.custom_subject})")
            
            # Fetch news explicitly matching their typed string
            raw_feed_data = fetch_custom_news(user.custom_subject)
            
            # Run Gemini curation filtering out the top 3 items
            personalized_html = generate_personalized_html(user.email, user.custom_subject, raw_feed_data)
            
            try:
                resend.Emails.send({
                    "from": "NewsEngine <onboarding@resend.dev>", # Replace with custom verified domain when ready
                    "to": [user.email],
                    "subject": f"🌟 Strategic Intel: Top 3 Updates on '{user.custom_subject}'",
                    "html": personalized_html
                })
                print(f"✅ Tailored brief successfully sent to {user.email}")
            except Exception as e:
                print(f"❌ Failed to email user {user.email}: {e}")

# ==================== FRONTEND WEB PAGE INTERFACE ====================
HTML_PAGE_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Custom Adaptive Briefing Engine</title>
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
        <h2>🛰️ Custom Intel Engine</h2>
        <p style="color: #64748b; font-size: 13px; margin-top:-10px;">Type any global news topic and specify your preferred hour to unlock automated tracking briefings.</p>
        
        <form id="configForm">
            <label for="email">Your Email Address:</label>
            <input type="email" id="email" required placeholder="example@gmail.com">
            
            <label for="custom_subject">What subject want to monitor?</label>
            <input type="text" id="custom_subject" required placeholder="e.g., Nvidia AI chips, TSMC, Global bonds">
            
            <label for="deliveryHour">Preferred Delivery Time (UTC):</label>
            <select id="deliveryHour">
                <option value="0">00:00 AM (UTC)</option>
                <option value="2">02:00 AM (UTC)</option>
                <option value="4">04:00 AM (UTC)</option>
                <option value="6">06:00 AM (UTC)</option>
                <option value="8" selected>08:00 AM (UTC) - Default</option>
                <option value="10">10:00 AM (UTC)</option>
                <option value="12">12:00 PM (UTC)</option>
                <option value="14">02:00 PM (UTC)</option>
                <option value="16">04:00 PM (UTC)</option>
                <option value="18">06:00 PM (UTC)</option>
                <option value="20">08:00 PM (UTC)</option>
                <option value="22">10:00 PM (UTC)</option>
            </select>
            
            <button type="submit">Activate Delivery Stream</button>
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
                const result = await response.json();
                
                if(response.ok) {
                    msgDiv.style.color = '#16a34a';
                    msgDiv.innerText = "✅ Track Stream Connected! Your custom brief runs daily.";
                } else {
                    msgDiv.style.color = '#dc2626';
                    msgDiv.innerText = "❌ Error: " + result.message;
                }
            } catch (err) {
                msgDiv.style.color = '#dc2626';
                msgDiv.innerText = "❌ Network configuration transmission failure.";
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
        return jsonify({"status": "error", "message": "Email address and monitoring subject string parameters required."}), 400
        
    user = UserProfile.query.filter_by(email=email).first()
    if user:
        user.custom_subject = subject
        user.delivery_hour = chosen_hour
        print(f"🔄 Updated tracking query parameters to '{subject}' at {chosen_hour}:00 UTC for: {email}")
    else:
        user = UserProfile(email=email, custom_subject=subject, delivery_hour=chosen_hour)
        db.session.add(user)
        print(f"🆕 Initiated new tracking profile node for '{subject}' at {chosen_hour}:00 UTC for: {email}")
        
    db.session.commit()
    return jsonify({"status": "success", "message": "Custom pipeline configurations successfully registered."}), 200

# ==================== AUTOMATED SCHEDULER GATE ====================
scheduler = BackgroundScheduler()
# The cron trigger fires exactly at minute 0 of every hour
scheduler.add_job(func=run_hourly_newsletter_batch, trigger="cron", minute=0)
scheduler.start()

# ==================== DIAGNOSTIC IMMEDIATE TEST TRIGGER ====================
@app.route('/secret-test-trigger')
def secret_test_trigger():
    """Manually kicks off the newsletter compiler instantly for all registered users."""
    try:
        print("⚡ Manual test trigger pulled! Executing custom keyword batch processing...")
        
        with app.app_context():
            users = UserProfile.query.all()
            if not users:
                return jsonify({"status": "error", "message": "No registered users found in the database. Please sign up on the homepage first."}), 400
                
            for user in users:
                print(f"🛰️ Immediate Test Pipeline running for: {user.email} (Topic: {user.custom_subject})")
                
                # 1. Fetch news matching their custom typed query string
                raw_feed_data = fetch_custom_news(user.custom_subject)
                
                # 2. Run Gemini curation filtering out the top 3 items
                personalized_html = generate_personalized_html(user.email, user.custom_subject, raw_feed_data)
                
                # 3. Dispatch payload instantly via Resend
                resend.Emails.send({
                    "from": "NewsEngine <onboarding@resend.dev>", # Replace with custom verified domain when ready
                    "to": [user.email],
                    "subject": f"🔥 IMMEDIATE TEST: Top 3 Updates on '{user.custom_subject}'",
                    "html": personalized_html
                })
                
        return jsonify({"status": "success", "message": "Immediate custom test batch completed! Check your email."}), 200
        
    except Exception as e:
        print(f"❌ Test Trigger Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
