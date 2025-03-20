import os
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import spacy
from langdetect import detect
import schedule
import time
import threading
import redis
import json
import requests
from textblob import TextBlob
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SPORTS_API_KEY = os.getenv("SPORTS_API_KEY", "3")  # Default to TheSportsDB free test key
HUGGINGFACE_API_TOKEN = os.getenv("HUGGINGFACE_API_TOKEN")  # Hugging Face API token

# Initialize Redis for leaderboard and caching
redis_client = redis.Redis(host='localhost', port=6379, db=0)

# Load Spanish NLP model
nlp = spacy.load("es_core_news_sm")

# Simple in-memory context store (can be replaced with Redis for persistence)
USER_CONTEXT = {}

# Default language responses with variations
RESPONSES = {
    "es": {
        "welcome": [
            "¡Bienvenido a TipsterX! Usa /tips para predicciones o /leaderboard para ver los rankings.",
            "¡Qué bueno verte, crack! Prueba /tips para unas predicciones o /leaderboard para el top.",
        ],
        "tips_positive": [
            "¡Apostando fuerte! Aquí tienes: {}.",
            "¡Se ve clarito! Mi predicción: {}.",
        ],
        "tips_neutral": [
            "Hmm, está complicado, pero diría que {}.",
            "Analicé todo y creo que {}.",
        ],
        "leaderboard": [
            "Top 3 usuarios: {}",
            "Los mejores están aquí: {}",
        ],
        "error": [
            "Algo salió mal, intenta de nuevo.",
            "Ups, fallé. Dame otra chance.",
        ]
    },
    "en": {
        "welcome": [
            "Welcome to TipsterX! Use /tips for predictions or /leaderboard for rankings.",
            "Hey, good to see you! Try /tips for predictions or /leaderboard for the top ranks.",
        ],
        "tips_positive": [
            "Big bet incoming! Here’s my take: {}.",
            "Looks clear to me! Prediction: {}.",
        ],
        "tips_neutral": [
            "Tough call, but I’d say {}.",
            "I crunched the numbers—here’s my guess: {}.",
        ],
        "leaderboard": [
            "Top 3 users: {}",
            "Here’s the best of the best: {}",
        ],
        "error": [
            "Something went wrong, try again.",
            "Oops, messed up. Let’s try that again.",
        ]
    }
}

# Detect language from text
def detect_language(text):
    try:
        return detect(text) if detect(text) in ["es", "en"] else "es"
    except:
        return "es"

# Analyze sentiment to adjust tone
def analyze_sentiment(text):
    blob = TextBlob(text)
    sentiment = blob.sentiment.polarity
    return "positive" if sentiment > 0 else "neutral"

# Cache sports data to avoid API rate limits
def fetch_sports_data():
    cache_key = "sports_data"
    cached = redis_client.get(cache_key)
    if cached:
        return json.loads(cached)

    try:
        # Fetch upcoming events for a league (e.g., English Premier League, id=4328)
        url = f"https://www.thesportsdb.com/api/v1/json/{SPORTS_API_KEY}/eventsnextleague.php?id=4328"
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        redis_client.setex(cache_key, 3600, json.dumps(data))  # Cache for 1 hour
        return data
    except requests.RequestException as e:
        print(f"Error fetching sports data: {e}")
        return {"error": "Failed to fetch sports data"}

# Generate AI-powered betting tip using Hugging Face Inference API
def generate_betting_tip(user_message, user_id):
    try:
        # Use context if available
        context = USER_CONTEXT.get(user_id, {"messages": [], "last_topic": None})
        recent_messages = context["messages"][-3:]  # Last 3 messages for context
        prompt = f"Recent chat: {recent_messages}\nUser says: {user_message}\nPredict a betting tip:"

        headers = {
            "Authorization": f"Bearer {HUGGINGFACE_API_TOKEN}",
            "Content-Type": "application/json",
        }
        data = {
            "inputs": prompt,
            "parameters": {"max_length": 50, "num_return_sequences": 1}
        }
        response = requests.post("https://api-inference.huggingface.co/v1/models/gpt2", headers=headers, json=data)
        response.raise_for_status()
        result = response.json()
        return result[0]["generated_text"].strip() if result else "Couldn’t predict, try again!"
    except Exception as e:
        print(f"Hugging Face API error: {e}")
        teams = ["Barcelona", "Real Madrid", "Manchester United", "PSG"]
        scores = ["2-1", "1-0", "3-2", "0-0"]
        import random
        return f"{random.choice(teams)} might win {random.choice(scores)}."

# Update leaderboard based on user activity
def update_leaderboard(user_id, username):
    score = redis_client.zincrby("leaderboard", 1, user_id)
    return score

# Get top 3 users from leaderboard
def get_leaderboard():
    top_users = redis_client.zrevrange("leaderboard", 0, 2, withscores=True)
    return [(user.decode(), score) for user, score in top_users]

# Command: Start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = detect_language(update.message.text)
    sentiment = analyze_sentiment(update.message.text)
    user_id = str(user.id)

    # Initialize user context
    USER_CONTEXT[user_id] = {"last_topic": None, "last_sentiment": sentiment, "messages": []}
    
    response_list = RESPONSES[lang]["welcome"]
    await update.message.reply_text(response_list[0 if sentiment == "positive" else 1])
    update_leaderboard(user.id, user.username)

# Command: Tips
async def tips(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = detect_language(update.message.text)
    sentiment = analyze_sentiment(update.message.text)
    user_id = str(user.id)

    # Update context
    USER_CONTEXT[user_id]["messages"].append(update.message.text)
    USER_CONTEXT[user_id]["last_sentiment"] = sentiment
    USER_CONTEXT[user_id]["last_topic"] = "betting"

    tip = generate_betting_tip(update.message.text, user_id)
    response_list = RESPONSES[lang][f"tips_{sentiment}"]
    await update.message.reply_text(response_list[0].format(tip))
    update_leaderboard(user.id, user.username)

# Command: Leaderboard
async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = detect_language(update.message.text)
    user_id = str(user.id)

    # Update context
    USER_CONTEXT[user_id]["messages"].append(update.message.text)
    USER_CONTEXT[user_id]["last_topic"] = "leaderboard"

    top_users = get_leaderboard()
    leaderboard_text = "\n".join([f"{i+1}. {user[0]}: {int(user[1])}" for i, user in enumerate(top_users)])
    response_list = RESPONSES[lang]["leaderboard"]
    await update.message.reply_text(response_list[0].format(leaderboard_text))
    update_leaderboard(user.id, user.username)

# Handle free-text messages for dynamic responses with context
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.lower()
    lang = detect_language(text)
    sentiment = analyze_sentiment(text)
    user_id = str(user.id)

    # Initialize or update user context
    if user_id not in USER_CONTEXT:
        USER_CONTEXT[user_id] = {"last_topic": None, "last_sentiment": sentiment, "messages": []}
    
    USER_CONTEXT[user_id]["messages"].append(text)
    USER_CONTEXT[user_id]["last_sentiment"] = sentiment
    if len(USER_CONTEXT[user_id]["messages"]) > 5:  # Keep only last 5 messages
        USER_CONTEXT[user_id]["messages"] = USER_CONTEXT[user_id]["messages"][-5:]

    # Simple intent detection with context
    if "predic" in text or "tip" in text or "bet" in text:
        USER_CONTEXT[user_id]["last_topic"] = "betting"
        tip = generate_betting_tip(text, user_id)
        response_list = RESPONSES[lang][f"tips_{sentiment}"]
        reply = response_list[0].format(tip)
    elif "who" in text and "win" in text:
        USER_CONTEXT[user_id]["last_topic"] = "betting"
        tip = generate_betting_tip(f"Predict a winner for: {text}", user_id)
        response_list = RESPONSES[lang][f"tips_{sentiment}"]
        reply = response_list[0].format(tip)
    elif USER_CONTEXT[user_id]["last_topic"] == "betting":
        # Continue betting convo if last topic was betting
        ai_response = generate_betting_tip(f"Continue the conversation naturally about betting: {text}", user_id)
        reply = ai_response
    else:
        # Fallback to general conversation
        USER_CONTEXT[user_id]["last_topic"] = "general"
        ai_response = generate_betting_tip(f"Respond naturally like a sports fan buddy: {text}", user_id)
        reply = ai_response

    await update.message.reply_text(reply)
    update_leaderboard(user.id, user.username)

# Scheduled updates every 4 hours
def scheduled_updates(app: Application):
    def send_updates():
        sports_data = fetch_sports_data()
        message = "Tendencias deportivas: Aquí tienes las últimas actualizaciones..." if not sports_data.get("error") else "No hay datos deportivos ahora."
        print(f"Broadcasting update: {message}")

    schedule.every(4).hours.do(send_updates)
    while True:
        schedule.run_pending()
        time.sleep(60)

# Error handler
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = detect_language(update.message.text) if update.message else "es"
    response_list = RESPONSES[lang]["error"]
    await update.message.reply_text(response_list[0])
    print(f"Error: {context.error}")

# Main function to run the bot
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("tips", tips))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    # Start scheduled updates in a separate thread
    threading.Thread(target=scheduled_updates, args=(app,), daemon=True).start()

    print("Bot started...")
    app.run_polling()

if __name__ == "__main__":
    main()