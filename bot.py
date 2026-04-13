import os
import logging
import random
import threading
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from zoneinfo import ZoneInfo

import httpx
from google import genai
from openai import OpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from data.jokes import JOKES
from data.quotes import QUOTES
from data.weather import CITIES, WEATHER_CODES

# ===== ENV =====
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_KEY = os.environ.get("GEMINI_KEY", "")
GROQ_KEY = os.environ.get("GROQ_KEY", "")
OPENROUTER_KEY = os.environ.get("OPENROUTER_KEY", "")

# ===== CLIENTS =====
gemini_client = genai.Client(api_key=GEMINI_KEY) if GEMINI_KEY else None
groq_client = OpenAI(api_key=GROQ_KEY, base_url="https://api.groq.com/openai/v1") if GROQ_KEY else None
openrouter_client = OpenAI(
    api_key=OPENROUTER_KEY,
    base_url="https://openrouter.ai/api/v1",
) if OPENROUTER_KEY else None

# ===== LOGGING =====
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("google_genai").setLevel(logging.WARNING)

# ===== USER STATE =====
# Example:
# {
#   123456: {
#       "mode": "menu" | "ai_chat",
#       "last_city": "sofia",
#       "selected_ai": "gemini"
#   }
# }
user_state: dict[int, dict[str, str]] = {}


# ===== HEALTHCHECK SERVER =====
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format: str, *args) -> None:
        return


def run_health_server() -> None:
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()


# ===== STATE =====
def get_user_state(user_id: int) -> dict[str, str]:
    if user_id not in user_state:
        user_state[user_id] = {
            "mode": "menu",
            "last_city": "",
            "selected_ai": "",
        }
    return user_state[user_id]


# ===== MENUS =====
def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🤖 Ask AI", callback_data="ai_menu")],
            [InlineKeyboardButton("🌦 Weather", callback_data="weather")],
            [InlineKeyboardButton("🕒 Time", callback_data="time")],
            [InlineKeyboardButton("😂 Joke", callback_data="joke")],
            [InlineKeyboardButton("💬 Quote", callback_data="quote")],
            [InlineKeyboardButton("ℹ️ Help", callback_data="help")],
        ]
    )


def ai_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✨ Gemini", callback_data="ai_gemini")],
            [InlineKeyboardButton("⚡ Groq", callback_data="ai_groq")],
            [InlineKeyboardButton("🦙 OpenRouter", callback_data="ai_openrouter")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_main")],
        ]
    )


def weather_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Sofia", callback_data="city_sofia"),
                InlineKeyboardButton("Pernik", callback_data="city_pernik"),
            ],
            [
                InlineKeyboardButton("Amsterdam", callback_data="city_amsterdam"),
                InlineKeyboardButton("Larnaca", callback_data="city_larnaca"),
            ],
            [InlineKeyboardButton("🔙 Back", callback_data="back_main")],
        ]
    )


def weather_actions_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔄 Refresh", callback_data="refresh_weather")],
            [InlineKeyboardButton("🌦 Weather Menu", callback_data="weather")],
            [InlineKeyboardButton("🔙 Main Menu", callback_data="back_main")],
        ]
    )


def back_to_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 Main Menu", callback_data="back_main")]]
    )


# ===== HELPERS =====
def get_time_text() -> str:
    sofia_time = datetime.now(ZoneInfo("Europe/Sofia")).strftime("%H:%M")
    pernik_time = datetime.now(ZoneInfo("Europe/Sofia")).strftime("%H:%M")
    amsterdam_time = datetime.now(ZoneInfo("Europe/Amsterdam")).strftime("%H:%M")
    larnaca_time = datetime.now(ZoneInfo("Asia/Nicosia")).strftime("%H:%M")
    utc_time = datetime.now(timezone.utc).strftime("%H:%M")

    return (
        "🕒 Current time\n\n"
        f"Sofia: {sofia_time}\n"
        f"Pernik: {pernik_time}\n"
        f"Amsterdam: {amsterdam_time}\n"
        f"Larnaca: {larnaca_time}\n"
        f"UTC: {utc_time}"
    )


def weather_code_to_text(code: int | None) -> str:
    if code is None:
        return "Unknown"
    return WEATHER_CODES.get(code, "Unknown")


async def get_weather(city_key: str) -> str:
    city = CITIES[city_key]

    params = {
        "latitude": city["lat"],
        "longitude": city["lon"],
        "current": "temperature_2m,weather_code,precipitation,rain",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,rain_sum,showers_sum",
        "timezone": city["timezone"],
        "forecast_days": 1,
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params=params,
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as e:
        logger.exception("Weather error")
        if e.response.status_code == 429:
            return "⚠️ Weather service is busy right now. Please try again in a moment."
        return "⚠️ Weather service returned an error."
    except Exception:
        logger.exception("Unexpected weather error")
        return "⚠️ Could not fetch weather right now."

    current = data.get("current", {})
    daily = data.get("daily", {})

    temperature = current.get("temperature_2m", "N/A")
    condition = weather_code_to_text(current.get("weather_code"))
    rain_chance = daily.get("precipitation_probability_max", ["N/A"])[0]
    temp_max = daily.get("temperature_2m_max", ["N/A"])[0]
    temp_min = daily.get("temperature_2m_min", ["N/A"])[0]

    rain_sum = daily.get("rain_sum", [0])[0]
    precipitation_sum = daily.get("precipitation_sum", [0])[0]
    showers_sum = daily.get("showers_sum", [0])[0]

    will_rain = (
        (isinstance(rain_sum, (int, float)) and rain_sum > 0)
        or (isinstance(precipitation_sum, (int, float)) and precipitation_sum > 0)
        or (isinstance(showers_sum, (int, float)) and showers_sum > 0)
    )

    rain_text = "Yes ☔" if will_rain else "No 😎"

    return (
        f"🌦 Weather in {city['name']}\n\n"
        f"🌡 Current: {temperature}°C\n"
        f"🌤 Condition: {condition}\n"
        f"🌧 Rain chance: {rain_chance}%\n"
        f"☔ Will it rain today? {rain_text}\n"
        f"📈 Today: {temp_min}°C - {temp_max}°C"
    )


async def ask_gemini(question: str) -> str:
    if not gemini_client:
        return "Gemini is not configured."
    response = gemini_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=question,
    )
    return response.text or "No response."


async def ask_groq(question: str) -> str:
    if not groq_client:
        return "Groq is not configured."
    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": question}],
    )
    return response.choices[0].message.content or "No response."


async def ask_openrouter(question: str) -> str:
    if not openrouter_client:
        return "OpenRouter is not configured."
    response = openrouter_client.chat.completions.create(
        model="meta-llama/llama-3.3-70b-instruct:free",
        messages=[{"role": "user", "content": question}],
    )
    return response.choices[0].message.content or "No response."


async def ask_selected_ai(ai_name: str, question: str) -> str:
    if ai_name == "gemini":
        return await ask_gemini(question)
    if ai_name == "groq":
        return await ask_groq(question)
    if ai_name == "openrouter":
        return await ask_openrouter(question)
    return "No AI selected."


def selected_ai_label(ai_name: str) -> str:
    if ai_name == "gemini":
        return "Gemini"
    if ai_name == "groq":
        return "Groq"
    if ai_name == "openrouter":
        return "OpenRouter"
    return "Unknown"


# ===== HANDLERS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return

    user = get_user_state(update.effective_user.id)
    user["mode"] = "menu"

    await update.message.reply_text(
        "Welcome! 👋\n\nI'm your Telegram assistant.\nChoose an option below:",
        reply_markup=main_menu(),
    )


async def safe_edit_message(query, text: str, reply_markup=None) -> None:
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            logger.warning("Ignored harmless Telegram edit error: message not modified")
            return
        raise


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None or update.effective_user is None:
        return

    await query.answer()

    user = get_user_state(update.effective_user.id)
    data = query.data

    if data == "back_main":
        user["mode"] = "menu"
        await safe_edit_message(query, "Main menu:", reply_markup=main_menu())
        return

    if data == "help":
        await safe_edit_message(
            query,
            "How to use me:\n\n"
            "• Tap Ask AI and choose a model.\n"
            "• Then send me a normal message.\n"
            "• Tap Weather to choose a city.\n"
            "• Tap Time for current city times.\n"
            "• Tap Joke or Quote for something fun.",
            reply_markup=back_to_main_menu(),
        )
        return

    if data == "ai_menu":
        user["mode"] = "menu"
        await safe_edit_message(
            query,
            "Choose which AI you want to use:",
            reply_markup=ai_menu(),
        )
        return

    if data == "ai_gemini":
        user["mode"] = "ai_chat"
        user["selected_ai"] = "gemini"
        await safe_edit_message(
            query,
            "✨ Gemini selected.\n\nSend me any message.",
            reply_markup=back_to_main_menu(),
        )
        return

    if data == "ai_groq":
        user["mode"] = "ai_chat"
        user["selected_ai"] = "groq"
        await safe_edit_message(
            query,
            "⚡ Groq selected.\n\nSend me any message.",
            reply_markup=back_to_main_menu(),
        )
        return

    if data == "ai_openrouter":
        user["mode"] = "ai_chat"
        user["selected_ai"] = "openrouter"
        await safe_edit_message(
            query,
            "🦙 OpenRouter selected.\n\nSend me any message.",
            reply_markup=back_to_main_menu(),
        )
        return

    if data == "weather":
        user["mode"] = "menu"
        await safe_edit_message(query, "Choose a city:", reply_markup=weather_menu())
        return

    if data.startswith("city_"):
        city_key = data.replace("city_", "", 1)
        user["last_city"] = city_key
        text = await get_weather(city_key)
        await safe_edit_message(query, text, reply_markup=weather_actions_menu())
        return

    if data == "refresh_weather":
        city_key = user.get("last_city", "")
        if not city_key:
            await safe_edit_message(query, "No city selected yet.", reply_markup=weather_menu())
            return

        text = await get_weather(city_key)
        await safe_edit_message(query, text, reply_markup=weather_actions_menu())
        return

    if data == "time":
        await safe_edit_message(query, get_time_text(), reply_markup=back_to_main_menu())
        return

    if data == "joke":
        await safe_edit_message(
            query,
            f"😂 Joke\n\n{random.choice(JOKES)}",
            reply_markup=back_to_main_menu(),
        )
        return

    if data == "quote":
        await safe_edit_message(
            query,
            f"💬 Quote\n\n{random.choice(QUOTES)}",
            reply_markup=back_to_main_menu(),
        )
        return


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None or update.message.text is None:
        return

    user = get_user_state(update.effective_user.id)

    if user.get("mode") != "ai_chat":
        await update.message.reply_text(
            "Use the menu first.",
            reply_markup=main_menu(),
        )
        return

    selected_ai = user.get("selected_ai", "")
    if not selected_ai:
        await update.message.reply_text(
            "Please choose an AI first.",
            reply_markup=ai_menu(),
        )
        return

    question = update.message.text.strip()

    try:
        await update.message.reply_text(f"Thinking with {selected_ai_label(selected_ai)}... 🤔")
        answer = await ask_selected_ai(selected_ai, question)
        await update.message.reply_text(answer)
    except Exception as e:
        logger.exception("AI error")
        await update.message.reply_text(f"Something went wrong with {selected_ai_label(selected_ai)}: {e}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error

    if isinstance(error, BadRequest) and "Message is not modified" in str(error):
        logger.warning("Ignored harmless Telegram BadRequest: Message is not modified")
        return

    if error and "Conflict: terminated by other getUpdates request" in str(error):
        logger.warning("Telegram polling conflict detected. Another bot instance may still be running.")
        return

    logger.exception("Unhandled exception", exc_info=error)


def main() -> None:
    threading.Thread(target=run_health_server, daemon=True).start()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("Bot is online!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()