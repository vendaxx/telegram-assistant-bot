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
from data.texts import TEXTS
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
#       "selected_ai": "gemini",
#       "language": "en"
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
            "language": "en",
        }
    return user_state[user_id]


# ===== TEXT HELPERS =====
def t(user_id: int, key: str, **kwargs) -> str:
    user = get_user_state(user_id)
    lang = user.get("language", "en")
    lang_map = TEXTS.get(lang, TEXTS["en"])
    template = lang_map.get(key, TEXTS["en"].get(key, key))
    return template.format(**kwargs)


def selected_ai_label(ai_name: str) -> str:
    if ai_name == "gemini":
        return "Gemini"
    if ai_name == "groq":
        return "Groq"
    if ai_name == "openrouter":
        return "OpenRouter"
    return "Unknown"


def selected_ai_icon(ai_name: str) -> str:
    if ai_name == "gemini":
        return "✨"
    if ai_name == "groq":
        return "⚡"
    if ai_name == "openrouter":
        return "🦙"
    return "🤖"


# ===== MENUS =====
def main_menu(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t(user_id, "ask_ai"), callback_data="ai_menu")],
            [InlineKeyboardButton(t(user_id, "weather"), callback_data="weather")],
            [InlineKeyboardButton(t(user_id, "time"), callback_data="time")],
            [InlineKeyboardButton(t(user_id, "joke"), callback_data="joke")],
            [InlineKeyboardButton(t(user_id, "quote"), callback_data="quote")],
            [InlineKeyboardButton(t(user_id, "settings"), callback_data="settings")],
            [InlineKeyboardButton(t(user_id, "help"), callback_data="help")],
        ]
    )


def ai_menu(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✨ Gemini", callback_data="ai_gemini")],
            [InlineKeyboardButton("⚡ Groq", callback_data="ai_groq")],
            [InlineKeyboardButton("🦙 OpenRouter", callback_data="ai_openrouter")],
            [InlineKeyboardButton(t(user_id, "back"), callback_data="back_main")],
        ]
    )


def settings_menu(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t(user_id, "language"), callback_data="settings_language")],
            [InlineKeyboardButton(t(user_id, "back"), callback_data="back_main")],
        ]
    )


def language_menu(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")],
            [InlineKeyboardButton("🇧🇬 Български", callback_data="lang_bg")],
            [InlineKeyboardButton(t(user_id, "back"), callback_data="settings")],
        ]
    )


def weather_menu(user_id: int) -> InlineKeyboardMarkup:
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
            [InlineKeyboardButton(t(user_id, "back"), callback_data="back_main")],
        ]
    )


def weather_actions_menu(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t(user_id, "refresh"), callback_data="refresh_weather")],
            [InlineKeyboardButton(t(user_id, "weather_menu"), callback_data="weather")],
            [InlineKeyboardButton(t(user_id, "back_main"), callback_data="back_main")],
        ]
    )


def back_to_main_menu(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t(user_id, "back_main"), callback_data="back_main")]]
    )


# ===== HELPERS =====
def get_time_text(user_id: int) -> str:
    sofia_time = datetime.now(ZoneInfo("Europe/Sofia")).strftime("%H:%M")
    pernik_time = datetime.now(ZoneInfo("Europe/Sofia")).strftime("%H:%M")
    amsterdam_time = datetime.now(ZoneInfo("Europe/Amsterdam")).strftime("%H:%M")
    larnaca_time = datetime.now(ZoneInfo("Asia/Nicosia")).strftime("%H:%M")
    utc_time = datetime.now(timezone.utc).strftime("%H:%M")

    return (
        f"{t(user_id, 'current_time_title')}\n\n"
        f"Sofia: {sofia_time}\n"
        f"Pernik: {pernik_time}\n"
        f"Amsterdam: {amsterdam_time}\n"
        f"Larnaca: {larnaca_time}\n"
        f"UTC: {utc_time}"
    )


def weather_code_to_text(code: int | None, user_id: int) -> str:
    if code is None:
        return t(user_id, "unknown")
    return WEATHER_CODES.get(code, t(user_id, "unknown"))


async def get_weather(city_key: str, user_id: int) -> str:
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
            return t(user_id, "weather_busy")
        return t(user_id, "weather_error")
    except Exception:
        logger.exception("Unexpected weather error")
        return t(user_id, "weather_unavailable")

    current = data.get("current", {})
    daily = data.get("daily", {})

    temperature = current.get("temperature_2m", "N/A")
    condition = weather_code_to_text(current.get("weather_code"), user_id)
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

    rain_text = t(user_id, "rain_yes") if will_rain else t(user_id, "rain_no")

    return (
        f"{t(user_id, 'weather_in', city=city['name'])}\n\n"
        f"{t(user_id, 'weather_current', temperature=temperature)}\n"
        f"{t(user_id, 'weather_condition', condition=condition)}\n"
        f"{t(user_id, 'weather_rain_chance', rain_chance=rain_chance)}\n"
        f"{t(user_id, 'weather_will_rain', rain_text=rain_text)}\n"
        f"{t(user_id, 'weather_today', temp_min=temp_min, temp_max=temp_max)}"
    )


async def ask_gemini(question: str, user_id: int) -> str:
    if not gemini_client:
        return t(user_id, "gemini_not_configured")
    response = gemini_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=question,
    )
    return response.text or t(user_id, "no_response")


async def ask_groq(question: str, user_id: int) -> str:
    if not groq_client:
        return t(user_id, "groq_not_configured")
    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": question}],
    )
    return response.choices[0].message.content or t(user_id, "no_response")


async def ask_openrouter(question: str, user_id: int) -> str:
    if not openrouter_client:
        return t(user_id, "openrouter_not_configured")
    response = openrouter_client.chat.completions.create(
        model="meta-llama/llama-3.3-70b-instruct:free",
        messages=[{"role": "user", "content": question}],
    )
    return response.choices[0].message.content or t(user_id, "no_response")


async def ask_selected_ai(ai_name: str, question: str, user_id: int) -> str:
    if ai_name == "gemini":
        return await ask_gemini(question, user_id)
    if ai_name == "groq":
        return await ask_groq(question, user_id)
    if ai_name == "openrouter":
        return await ask_openrouter(question, user_id)
    return t(user_id, "no_ai_selected")


# ===== HANDLERS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return

    user = get_user_state(update.effective_user.id)
    user["mode"] = "menu"

    await update.message.reply_text(
        t(update.effective_user.id, "welcome"),
        reply_markup=main_menu(update.effective_user.id),
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

    user_id = update.effective_user.id
    user = get_user_state(user_id)
    data = query.data

    if data == "back_main":
        user["mode"] = "menu"
        await safe_edit_message(query, t(user_id, "main_menu"), reply_markup=main_menu(user_id))
        return

    if data == "help":
        await safe_edit_message(
            query,
            t(user_id, "help_text"),
            reply_markup=back_to_main_menu(user_id),
        )
        return

    if data == "settings":
        user["mode"] = "menu"
        await safe_edit_message(
            query,
            t(user_id, "settings"),
            reply_markup=settings_menu(user_id),
        )
        return

    if data == "settings_language":
        await safe_edit_message(
            query,
            t(user_id, "choose_language"),
            reply_markup=language_menu(user_id),
        )
        return

    if data == "lang_en":
        user["language"] = "en"
        await safe_edit_message(
            query,
            t(user_id, "language_updated"),
            reply_markup=settings_menu(user_id),
        )
        return

    if data == "lang_bg":
        user["language"] = "bg"
        await safe_edit_message(
            query,
            t(user_id, "language_updated"),
            reply_markup=settings_menu(user_id),
        )
        return

    if data == "ai_menu":
        user["mode"] = "menu"
        await safe_edit_message(
            query,
            t(user_id, "choose_ai"),
            reply_markup=ai_menu(user_id),
        )
        return

    if data == "ai_gemini":
        user["mode"] = "ai_chat"
        user["selected_ai"] = "gemini"
        await safe_edit_message(
            query,
            t(
                user_id,
                "ai_selected_send",
                icon=selected_ai_icon("gemini"),
                ai_name=selected_ai_label("gemini"),
            ),
            reply_markup=back_to_main_menu(user_id),
        )
        return

    if data == "ai_groq":
        user["mode"] = "ai_chat"
        user["selected_ai"] = "groq"
        await safe_edit_message(
            query,
            t(
                user_id,
                "ai_selected_send",
                icon=selected_ai_icon("groq"),
                ai_name=selected_ai_label("groq"),
            ),
            reply_markup=back_to_main_menu(user_id),
        )
        return

    if data == "ai_openrouter":
        user["mode"] = "ai_chat"
        user["selected_ai"] = "openrouter"
        await safe_edit_message(
            query,
            t(
                user_id,
                "ai_selected_send",
                icon=selected_ai_icon("openrouter"),
                ai_name=selected_ai_label("openrouter"),
            ),
            reply_markup=back_to_main_menu(user_id),
        )
        return

    if data == "weather":
        user["mode"] = "menu"
        await safe_edit_message(query, t(user_id, "choose_city"), reply_markup=weather_menu(user_id))
        return

    if data.startswith("city_"):
        city_key = data.replace("city_", "", 1)
        user["last_city"] = city_key
        text = await get_weather(city_key, user_id)
        await safe_edit_message(query, text, reply_markup=weather_actions_menu(user_id))
        return

    if data == "refresh_weather":
        city_key = user.get("last_city", "")
        if not city_key:
            await safe_edit_message(query, t(user_id, "no_city_selected"), reply_markup=weather_menu(user_id))
            return

        text = await get_weather(city_key, user_id)
        await safe_edit_message(query, text, reply_markup=weather_actions_menu(user_id))
        return

    if data == "time":
        await safe_edit_message(query, get_time_text(user_id), reply_markup=back_to_main_menu(user_id))
        return

    if data == "joke":
        await safe_edit_message(
            query,
            f"{t(user_id, 'joke_title')}\n\n{random.choice(JOKES)}",
            reply_markup=back_to_main_menu(user_id),
        )
        return

    if data == "quote":
        await safe_edit_message(
            query,
            f"{t(user_id, 'quote_title')}\n\n{random.choice(QUOTES)}",
            reply_markup=back_to_main_menu(user_id),
        )
        return


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None or update.message.text is None:
        return

    user_id = update.effective_user.id
    user = get_user_state(user_id)

    if user.get("mode") != "ai_chat":
        await update.message.reply_text(
            t(user_id, "use_menu_first"),
            reply_markup=main_menu(user_id),
        )
        return

    selected_ai = user.get("selected_ai", "")
    if not selected_ai:
        await update.message.reply_text(
            t(user_id, "please_choose_ai_first"),
            reply_markup=ai_menu(user_id),
        )
        return

    question = update.message.text.strip()

    try:
        await update.message.reply_text(
            t(user_id, "thinking_with", ai_name=selected_ai_label(selected_ai))
        )
        answer = await ask_selected_ai(selected_ai, question, user_id)
        await update.message.reply_text(answer)
    except Exception as e:
        logger.exception("AI error")
        await update.message.reply_text(
            t(
                user_id,
                "something_went_wrong",
                ai_name=selected_ai_label(selected_ai),
                error=str(e),
            )
        )


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