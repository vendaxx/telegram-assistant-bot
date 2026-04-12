import os
import logging
import random
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
from google import genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
GEMINI_KEY = os.environ["GEMINI_KEY"]

# ===== CLIENTS =====
gemini_client = genai.Client(api_key=GEMINI_KEY)

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
logging.getLogger("google_genai").setLevel(logging.WARNING)

# ===== USER STATE =====
user_state: dict[int, dict[str, str]] = {}


def get_user_state(user_id: int) -> dict[str, str]:
    if user_id not in user_state:
        user_state[user_id] = {
            "mode": "menu",
            "last_city": "",
        }
    return user_state[user_id]


# ===== MENUS =====
def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🤖 Ask AI", callback_data="ai")],
            [InlineKeyboardButton("🌦 Weather", callback_data="weather")],
            [InlineKeyboardButton("🧪 Time TEST", callback_data="time")],  # <-- TEST
            [InlineKeyboardButton("😂 Joke", callback_data="joke")],
            [InlineKeyboardButton("💬 Quote", callback_data="quote")],
            [InlineKeyboardButton("ℹ️ Help", callback_data="help")],
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


# ===== HELPERS =====
def get_time_text() -> str:
    sofia_time = datetime.now(ZoneInfo("Europe/Sofia")).strftime("%H:%M")
    amsterdam_time = datetime.now(ZoneInfo("Europe/Amsterdam")).strftime("%H:%M")
    larnaca_time = datetime.now(ZoneInfo("Asia/Nicosia")).strftime("%H:%M")
    utc_time = datetime.now(timezone.utc).strftime("%H:%M")

    return (
        "🕒 Current time\n\n"
        f"Sofia: {sofia_time}\n"
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
                "https://api.open-meteo.com/v1/forecast", params=params
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            return "⚠️ Weather service is busy. Try again in a moment."
        raise

    current = data.get("current", {})
    daily = data.get("daily", {})

    temperature = current.get("temperature_2m", "N/A")
    condition = weather_code_to_text(current.get("weather_code"))

    temp_max = daily.get("temperature_2m_max", ["N/A"])[0]
    temp_min = daily.get("temperature_2m_min", ["N/A"])[0]

    return (
        f"🌦 Weather in {city['name']}\n\n"
        f"🌡 Current: {temperature}°C\n"
        f"🌤 Condition: {condition}\n"
        f"📈 Today: {temp_min}°C - {temp_max}°C"
    )


async def ask_gemini(question: str) -> str:
    response = gemini_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=question,
    )
    return response.text or "No response."


# ===== HANDLERS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return

    user = get_user_state(update.effective_user.id)
    user["mode"] = "menu"

    await update.message.reply_text(
        "🚨 VERSION 1.1 LIVE TEST 🚨\n\nChoose an option below:",
        reply_markup=main_menu(),
    )


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None or update.effective_user is None:
        return

    await query.answer()

    user = get_user_state(update.effective_user.id)
    data = query.data

    if data == "back_main":
        user["mode"] = "menu"
        await query.edit_message_text("Main menu:", reply_markup=main_menu())

    elif data == "ai":
        user["mode"] = "ai_chat"
        await query.edit_message_text("AI mode ON 🤖")

    elif data == "weather":
        await query.edit_message_text("Choose a city:", reply_markup=weather_menu())

    elif data.startswith("city_"):
        city_key = data.replace("city_", "")
        user["last_city"] = city_key
        text = await get_weather(city_key)
        await query.edit_message_text(text, reply_markup=weather_actions_menu())

    elif data == "refresh_weather":
        city_key = user.get("last_city", "")
        text = await get_weather(city_key)
        await query.edit_message_text(text, reply_markup=weather_actions_menu())

    elif data == "time":
        await query.edit_message_text(get_time_text())

    elif data == "joke":
        await query.edit_message_text(f"😂 {random.choice(JOKES)}")

    elif data == "quote":
        await query.edit_message_text(f"💬 {random.choice(QUOTES)}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None or update.message.text is None:
        return

    user = get_user_state(update.effective_user.id)

    if user.get("mode") != "ai_chat":
        await update.message.reply_text("Use the menu first.", reply_markup=main_menu())
        return

    await update.message.reply_text("Thinking... 🤔")
    answer = await ask_gemini(update.message.text)
    await update.message.reply_text(answer)


# ===== MAIN =====
def main() -> None:
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot is online!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()