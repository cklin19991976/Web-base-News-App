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
    """Fetches articles for a single explicit topic string."""
    url = "https://newsapi.org/v2/everything"
    date_yesterday = (datetime.utcnow() - timedelta(days=1)).strftime('%Y-%m-%d')
    
    params = {
        "q": user_query,         
        "sortBy": "relevancy",  
        "from": date_yesterday,
        "language": "en",
        "pageSize": 5,          # 🛠️ TOKEN OPTIMIZATION: Reduced from 15 to 5. 
                                # This strips out thousands of redundant tokens while retaining top stories.
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

def generate_single_subject_section_html(user_topic, raw_news_payload):
    """Asks Gemini to compile news stories, utilizing an exponential backoff wrapper for 429 safety."""
    client = genai.Client(api_key=GEMINI_API_KEY)
    
    prompt = f"""
    You are an elite corporate intelligence analyst. Analyze the raw recent data feed provided below.
    Your absolute mandate is to isolate EXACTLY the top 3 most important, high-impact news stories from the last 24 hours regarding this specific topic: "{user_topic}". 

    Generate a clean HTML fragment with NO outer body or html tags:
    <div style="margin-bottom: 35px; background: white; padding: 20px; border-radius: 8px; border: 1px solid #e2e8f0;">
        <h3 style="margin:0 0 15px 0; font-size:16px; color:#1e40af; text-transform: uppercase; letter-spacing: 0.05em; border-bottom: 2px solid #eff6ff; padding-bottom: 5px;">
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
                model='gemini-2.5-flash-lite',
                contents=prompt,
                config=types.GenerateContentConfig(thinking_config=types.ThinkingConfig(thinking_budget=0))
            )
            return response.text
        except Exception as e:
            # Check if this exception is a 429 rate limit
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                calculated_delay = base_delay * (2 ** attempt) # Wait 3s, then 6s, then 12s...
                print(f"⚠️ [Attempt {attempt + 1}/{max_retries}] Gemini rate wall detected for '{user_topic}'. Cooling down for {calculated_delay}s...")
                time.sleep(calculated_delay)
            else:
                # If it's a completely different error, fail immediately so we can read it
                print(f"❌ Non-rate limit error hit: {e}")
                return f"<p>Error compiling news layout data: {e}</p>"
                
    # If all retries failed
    return f"<p>System skipped execution segment for '{user_topic}' due to high API demand congestion. Please retry shortly.</p>"

def compile_master_email_body(user_email, topics_list):
    """Loops through every topic independently to guarantee a 3-news breakdown per subject."""
    sections_html = ""
    
    for topic in topics_list:
        print(f"🔄 Processing independent micro-pipeline for subject element: {topic}")
        raw_news = fetch_custom_news(topic)
        sections_html += generate_single_subject_section_html(topic, raw_news)
        
        # Keep an underlying rhythm safety spacing
        time.sleep(1.0)
        
    master_wrapper = f"""
    <div style="background-color:#f8fafc; padding:30px 15px; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; color:#1e293b; max-width:650px; margin:0 auto; border-radius:12px;">
        <div style="border-bottom:2px solid #e2e8f0; padding-bottom:15px; margin-bottom:25px;">
            <h1 style="margin:0; font-size:22px; color:#0f172a; font-weight:800;">🌟 Your Multi-Subject Matrix Briefing</h1>
            <p style="margin:5px 0 0 0; font-size:13px; color:#64748b;">Custom tailored streams for: {user_email}</p>
        </div>
        
        {sections_html}
        
        <div style="text-align: center; margin-top: 20px; font-size: 11px; color: #94a3b8;">
            Automated intelligence network node engine. To modify your subjects, re-submit the core web configuration portal form.
        </div>
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
                    "from": "Matrix Briefing <briefing@yourdomain.com>", # Make sure this matches your verified domain!
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
    <title>Multi-Subject Briefing Engine</title>
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
        <h2>🛰️ Multi-Stream Configurator</h2>
        <p style="color: #64748b; font-size: 13px; margin-top:-10px;">Type your tracking subjects separated by commas to receive exactly 3 top news stories for each individual field.</p>
        
        <form id="configForm">
            <label for="email">Your Email Address:</label>
            <input type="email" id="email" required placeholder="example@gmail.com">
            
            <label for="custom_subject">Monitored Subjects (Separate with Commas):</label>
            <input type="text" id="custom_subject" required placeholder="e.g., Bitcoin, TSMC, Nvidia AI">
            
            <label for="deliveryHour">Preferred Delivery Time (UTC):</label>
            <select id="deliveryHour">
                <option value="0">00:00 AM (UTC)</option>
                <option value="2">02:00 AM (UTC)</option>
                <option value="4">04:00 AM (UTC)</option>
                <option value="6">06:00 AM (UTC)</option>
                <option value="8" selected>08:00 AM (UTC)</option>
                <option value="10">10:00 AM (UTC)</option>
                <option value="12">12:00 PM (UTC)</option>
                <option value="14">02:00 PM (UTC)</option>
                <option value="16">04:00 PM (UTC)</option>
                <option value="18">06:00 PM (UTC)</option>
                <option value="20">08:00 PM (UTC)</option>
                <option value="22">10:00 PM (UTC)</option>
            </select>
            
            <button type="submit">Deploy Tracking Matrix</button>
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
                    msgDiv.innerText = "✅ Multi-stream profile armed and ready!";
                } else {
                    const result = await response.json();
                    msgDiv.style.color = '#dc2626';
                    msgDiv.innerText = "❌ Error: " + result.message;
                }
            } catch (err) {
                msgDiv.style.color = '#dc2626';
                msgDiv.innerText = "❌ Network transmission processing failure.";
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
                    "from": "Matrix Briefing <briefing@yourdomain.com>", # Adjust to your custom domain!
                    "to": [user.email],
                    "subject": f"🔥 MULTI-SECTION TEST: {len(topics)} Subjects Isolated",
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