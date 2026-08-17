import logging
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db

# --- Seed values used only the very first time the bot runs (no DB row yet).
# After that, city/coordinates/timezone all live in the database and can be
# changed with /setcity. Change these two lines if your first-ever run
# should start somewhere other than Yerevan. ---
DEFAULT_CITY_NAME = "Yerevan"
DEFAULT_LATITUDE = 40.1792
DEFAULT_LONGITUDE = 44.4991
DEFAULT_TIMEZONE = "Asia/Yerevan"
# ---

load_dotenv()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("weather_bot")

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
CACHE_TTL_SECONDS = 15 * 60
_cache = {}  # (lat, lon) -> {"data": ..., "fetched_at": ...}

TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

JOB_ID = "daily_briefing"
scheduler = AsyncIOScheduler()
telegram_app = None  # set in main(), used by the scheduled job to send messages

_pending_city_choices = {}  # chat_id -> list of up-to-3 geocoding candidates


# ---------------------------------------------------------------------------
# Open-Meteo
# ---------------------------------------------------------------------------

async def fetch_weather(lat, lon, timezone):
    """Fetch current conditions + a 2-day forecast (today, tomorrow), plus
    yesterday via past_days, from Open-Meteo. Cached per-location for 15 minutes."""
    cache_key = (lat, lon)
    now_aware = datetime.now(ZoneInfo(timezone))
    cached = _cache.get(cache_key)
    if cached and (now_aware - cached["fetched_at"]).total_seconds() < CACHE_TTL_SECONDS:
        return cached["data"]

    params = {
        "latitude": lat,
        "longitude": lon,
        "timezone": timezone,
        "temperature_unit": "celsius",
        "wind_speed_unit": "kmh",
        "precipitation_unit": "mm",
        "past_days": 1,
        "forecast_days": 2,
        "current": "temperature_2m,apparent_temperature",
        "hourly": "temperature_2m,apparent_temperature,precipitation_probability,precipitation",
        "daily": (
            "temperature_2m_max,temperature_2m_min,apparent_temperature_max,"
            "apparent_temperature_min,uv_index_max,wind_gusts_10m_max,sunrise,sunset"
        ),
    }

    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(OPEN_METEO_URL, params=params)
        response.raise_for_status()
        data = response.json()

    _cache[cache_key] = {"data": data, "fetched_at": now_aware}
    return data


async def geocode_city(name):
    """Returns a list of matching places (possibly empty)."""
    params = {"name": name, "count": 5, "language": "en", "format": "json"}
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(OPEN_METEO_GEOCODING_URL, params=params)
        response.raise_for_status()
        data = response.json()
    return data.get("results", [])


def _hourly_indices_for_day(data, day_str):
    return [i for i, t in enumerate(data["hourly"]["time"]) if t.startswith(day_str)]


# ---------------------------------------------------------------------------
# Tip rules (SPEC section 4)
# ---------------------------------------------------------------------------

def build_tip_rules(data, now, day_offset):
    """Evaluate the section 4 tip rules for the given day.

    day_offset=0 is today (uses the live 'current' reading and the last-12h
    half of the ice rule). day_offset=1 is tomorrow (no live reading exists
    yet, so the forecasted midday hour stands in for 'current'; the ice
    rule only looks forward, since there's no past for a future day).
    `now` is a naive datetime already in local wall-clock time, matching
    the naive local timestamps Open-Meteo returns when `timezone` is set
    explicitly.
    """
    daily = data["daily"]
    hourly = data["hourly"]

    target_date = now + timedelta(days=day_offset)
    target_str = target_date.strftime("%Y-%m-%d")
    prev_str = (target_date - timedelta(days=1)).strftime("%Y-%m-%d")

    di = daily["time"].index(target_str)
    previ = daily["time"].index(prev_str)
    day_hour_idxs = _hourly_indices_for_day(data, target_str)

    high = daily["temperature_2m_max"][di]
    low = daily["temperature_2m_min"][di]
    feels_high = daily["apparent_temperature_max"][di]
    feels_low = daily["apparent_temperature_min"][di]
    uv_max = daily["uv_index_max"][di]
    gust_max = daily["wind_gusts_10m_max"][di]
    sunrise = datetime.fromisoformat(daily["sunrise"][di])
    sunset = datetime.fromisoformat(daily["sunset"][di])
    prev_high = daily["temperature_2m_max"][previ]

    is_today = day_offset == 0
    day_label = "today" if is_today else "tomorrow"
    prev_label = "yesterday" if is_today else "today"

    if is_today:
        current = data["current"]
        temp_ref = current["temperature_2m"]
        feels_ref = current["apparent_temperature"]
    else:
        noon_idx = min(
            day_hour_idxs,
            key=lambda i: abs(datetime.fromisoformat(hourly["time"][i]).hour - 12),
        )
        temp_ref = hourly["temperature_2m"][noon_idx]
        feels_ref = hourly["apparent_temperature"][noon_idx]

    tips = {}

    # --- Clothing (feels-like band) ---
    if feels_ref < -5:
        clothing = "Serious winter gear — insulated coat, hat, gloves, scarf"
    elif feels_ref < 5:
        clothing = "Winter coat and a hat"
    elif feels_ref < 13:
        clothing = "Warm jacket; a layer underneath you can remove"
    elif feels_ref < 19:
        clothing = "Light jacket or a jumper"
    elif feels_ref <= 25:
        clothing = "Comfortable — no jacket needed"
    else:
        clothing = "Light, breathable clothing"
    tips["clothing"] = clothing

    # Layering override: the day's feels-like high/low swing by 10C or more
    tips["layering_override"] = (feels_high - feels_low) >= 10

    # --- Carry ---
    carry = []

    daylight_idxs = [
        i
        for i in day_hour_idxs
        if sunrise <= datetime.fromisoformat(hourly["time"][i]) <= sunset
    ]
    max_daylight_rain_chance = max(
        (hourly["precipitation_probability"][i] for i in daylight_idxs), default=0
    )
    umbrella_recommended = max_daylight_rain_chance >= 40
    if max_daylight_rain_chance >= 70:
        carry.append("Bring an umbrella — rain is very likely")
    elif umbrella_recommended:
        carry.append("Worth bringing an umbrella")

    if uv_max >= 6:
        carry.append("Sunglasses and sunscreen — UV will be high")

    if feels_high > 30:
        carry.append("Water — it'll feel hot out there")

    tips["carry"] = carry

    # --- Heads-ups ---
    headsups = []

    if gust_max > 40:
        wind_msg = f"Windy {day_label}, gusts up to {round(gust_max)} km/h"
        if umbrella_recommended:
            wind_msg += " — an umbrella won't do much good in that"
        headsups.append(wind_msg)

    if is_today:
        last_12h_start = now - timedelta(hours=12)
        recent_precip = sum(
            hourly["precipitation"][i] or 0
            for i, t in enumerate(hourly["time"])
            if last_12h_start <= datetime.fromisoformat(t) <= now
        )
        upcoming_precip = sum(
            hourly["precipitation"][i] or 0
            for i in day_hour_idxs
            if datetime.fromisoformat(hourly["time"][i]) >= now
        )
        icy = temp_ref <= 0 and (recent_precip > 0 or upcoming_precip > 0)
    else:
        day_precip = sum(hourly["precipitation"][i] or 0 for i in day_hour_idxs)
        icy = low <= 0 and day_precip > 0

    if icy:
        headsups.append("Ground could be icy — watch your footing")

    diff = high - prev_high
    if abs(diff) >= 8:
        direction = "up" if diff > 0 else "down"
        headsups.append(
            f"Big change from {prev_label} — {day_label}'s high ({round(high)}°) is "
            f"{direction} {round(abs(diff))}°C from {prev_label}'s ({round(prev_high)}°)"
        )

    if umbrella_recommended:
        buckets = {"morning": [], "afternoon": [], "evening": []}
        for i in daylight_idxs:
            hour = datetime.fromisoformat(hourly["time"][i]).hour
            chance = hourly["precipitation_probability"][i]
            if hour < 12:
                buckets["morning"].append(chance)
            elif hour < 18:
                buckets["afternoon"].append(chance)
            else:
                buckets["evening"].append(chance)
        best_bucket = max(buckets, key=lambda b: max(buckets[b], default=0))
        if max(buckets[best_bucket], default=0) >= 40:
            headsups.append(f"Rain looks most likely in the {best_bucket}")

    tips["headsups"] = headsups
    tips["is_today"] = is_today
    tips["numbers"] = {
        "temp_ref": temp_ref,
        "feels_ref": feels_ref,
        "high": high,
        "low": low,
        "rain_chance": max_daylight_rain_chance,
        "wind": gust_max,
    }

    return tips


def build_headline(tips):
    if tips["headsups"]:
        return tips["headsups"][0]
    if tips["layering_override"]:
        return "big swings today — layer up"
    if tips["carry"]:
        return "grab your gear before heading out"
    return "nothing notable today"


def format_briefing(city_name, tips, target_date, day_offset):
    n = tips["numbers"]
    date_str = target_date.strftime("%a %d %b")
    headline = build_headline(tips)
    prefix = "Tomorrow — " if day_offset == 1 else ""
    label = "now" if tips["is_today"] else "midday"

    lines = [f"\U0001F324️ *{prefix}{city_name}, {date_str}* — {headline}", ""]

    lines.append(
        f"\U0001F321️ {round(n['temp_ref'])}°C {label} "
        f"(feels {round(n['feels_ref'])}°C) · "
        f"H:{round(n['high'])}° L:{round(n['low'])}° · "
        f"Rain {round(n['rain_chance'])}% · "
        f"Wind {round(n['wind'])} km/h"
    )
    lines.append("")

    wear_line = tips["clothing"]
    if tips["layering_override"]:
        wear_line += ". Big swing between the high and low — dress in layers."
    lines.append(f"\U0001F455 {wear_line}")

    if tips["carry"]:
        lines.append(f"\U0001F392 {'; '.join(tips['carry'])}")

    for h in tips["headsups"]:
        lines.append(f"⚠️ {h}")

    return "\n".join(lines)


async def get_briefing_text(day_offset=0):
    settings = db.get_settings()
    data = await fetch_weather(settings["latitude"], settings["longitude"], settings["timezone"])
    now = datetime.now(ZoneInfo(settings["timezone"])).replace(tzinfo=None)
    tips = build_tip_rules(data, now, day_offset)
    target_date = now + timedelta(days=day_offset)
    return format_briefing(settings["city_name"], tips, target_date, day_offset)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

async def send_scheduled_briefing():
    try:
        settings = db.get_settings()
        if not settings["chat_id"] or not settings["scheduled_time"]:
            logger.warning("Scheduled send fired but no chat_id/schedule is set; skipping")
            return
        text = await get_briefing_text(day_offset=0)
        await telegram_app.bot.send_message(settings["chat_id"], text, parse_mode="Markdown")
        logger.info("Scheduled briefing sent to chat %s", settings["chat_id"])
    except Exception:
        logger.exception("Scheduled briefing failed to send")


def register_daily_job(time_str, timezone):
    hour, minute = (int(p) for p in time_str.split(":"))
    scheduler.add_job(
        send_scheduled_briefing,
        trigger=CronTrigger(hour=hour, minute=minute, timezone=ZoneInfo(timezone)),
        id=JOB_ID,
        replace_existing=True,
    )
    logger.info("Daily briefing scheduled for %s (%s)", time_str, timezone)


def remove_daily_job():
    if scheduler.get_job(JOB_ID):
        scheduler.remove_job(JOB_ID)
        logger.info("Daily briefing schedule removed")


async def post_init(application):
    scheduler.start()
    settings = db.get_settings()
    if settings["scheduled_time"]:
        register_daily_job(settings["scheduled_time"], settings["timezone"])
        logger.info(
            "Resumed existing schedule from database: %s (%s)",
            settings["scheduled_time"],
            settings["timezone"],
        )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def _apply_city(result):
    """Persist a chosen geocoding result as the new city, and if a schedule
    is active, re-register it under the new city's timezone."""
    settings = db.get_settings()
    timezone = result.get("timezone") or settings["timezone"]
    db.set_city(result["name"], result["latitude"], result["longitude"], timezone)
    if settings["scheduled_time"]:
        register_daily_job(settings["scheduled_time"], timezone)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.set_chat_id(update.effective_chat.id)
    await update.message.reply_text(
        "Hi! I'm your weather bot.\n\n"
        "/weather — today's briefing\n"
        "/tomorrow — tomorrow's briefing\n"
        "/setcity <name> — change your city\n"
        "/subscribe HH:MM — get a daily briefing at that time\n"
        "/unsubscribe — stop the daily briefing\n"
        "/status — show your current city and schedule\n"
        "/help — show this message"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def weather(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.set_chat_id(update.effective_chat.id)
    try:
        text = await get_briefing_text(day_offset=0)
    except Exception:
        logger.exception("Failed to build today's weather briefing")
        await update.message.reply_text(
            "Weather data is temporarily unavailable — please try again shortly."
        )
        return
    await update.message.reply_text(text, parse_mode="Markdown")


async def tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.set_chat_id(update.effective_chat.id)
    try:
        text = await get_briefing_text(day_offset=1)
    except Exception:
        logger.exception("Failed to build tomorrow's weather briefing")
        await update.message.reply_text(
            "Weather data is temporarily unavailable — please try again shortly."
        )
        return
    await update.message.reply_text(text, parse_mode="Markdown")


async def setcity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db.set_chat_id(chat_id)

    query_text = " ".join(context.args).strip()
    if not query_text:
        await update.message.reply_text("Tell me which city — for example: /setcity Tbilisi")
        return

    try:
        results = await geocode_city(query_text)
    except Exception:
        logger.exception("Geocoding request failed")
        await update.message.reply_text(
            "Weather data is temporarily unavailable — please try again shortly."
        )
        return

    if not results:
        await update.message.reply_text(
            f'I couldn\'t find a city called "{query_text}" — try a different spelling?'
        )
        return

    if len(results) == 1:
        result = results[0]
        await _apply_city(result)
        await update.message.reply_text(f"Got it — city set to {result['name']}, {result['country']}.")
        return

    choices = results[:3]
    _pending_city_choices[chat_id] = choices
    keyboard = [
        [InlineKeyboardButton(f"{c['name']}, {c['country']}", callback_data=f"setcity:{i}")]
        for i, c in enumerate(choices)
    ]
    await update.message.reply_text(
        "I found a few matches — which one did you mean?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def setcity_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    db.set_chat_id(chat_id)

    choices = _pending_city_choices.get(chat_id)
    idx_str = query.data.split(":", 1)[1]
    idx = int(idx_str) if idx_str.isdigit() else -1

    if not choices or idx >= len(choices) or idx < 0:
        await query.edit_message_text("That selection expired — run /setcity again.")
        return

    chosen = choices[idx]
    del _pending_city_choices[chat_id]
    await _apply_city(chosen)
    await query.edit_message_text(f"Got it — city set to {chosen['name']}, {chosen['country']}.")


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.set_chat_id(update.effective_chat.id)

    if not context.args:
        await update.message.reply_text(
            "Tell me what time — for example: /subscribe 07:30 (24-hour clock)"
        )
        return

    time_text = context.args[0]
    match = TIME_RE.match(time_text)
    if not match:
        await update.message.reply_text(
            f'"{time_text}" doesn\'t look like a valid time — use HH:MM in 24-hour format, '
            "for example: /subscribe 07:30"
        )
        return

    hour, minute = int(match.group(1)), int(match.group(2))
    time_str = f"{hour:02d}:{minute:02d}"

    settings = db.get_settings()
    db.set_schedule(time_str)
    register_daily_job(time_str, settings["timezone"])

    await update.message.reply_text(
        f"Done — I'll send your daily briefing at {time_str} ({settings['timezone']})."
    )


async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.set_chat_id(update.effective_chat.id)
    settings = db.get_settings()
    if not settings["scheduled_time"]:
        await update.message.reply_text("You don't have a daily briefing scheduled right now.")
        return
    db.set_schedule(None)
    remove_daily_job()
    await update.message.reply_text("Done — daily briefing turned off.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.set_chat_id(update.effective_chat.id)
    settings = db.get_settings()
    lines = [f"City: {settings['city_name']} ({settings['timezone']})"]
    if settings["scheduled_time"]:
        lines.append(f"Daily briefing: {settings['scheduled_time']}")
    else:
        lines.append("Daily briefing: not scheduled")
    await update.message.reply_text("\n".join(lines))


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Not sure what that means — try /help.")


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and add your token."
        )

    db.init_db(DEFAULT_CITY_NAME, DEFAULT_LATITUDE, DEFAULT_LONGITUDE, DEFAULT_TIMEZONE)

    global telegram_app
    telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("help", help_command))
    telegram_app.add_handler(CommandHandler("weather", weather))
    telegram_app.add_handler(CommandHandler("tomorrow", tomorrow))
    telegram_app.add_handler(CommandHandler("setcity", setcity))
    telegram_app.add_handler(CommandHandler("subscribe", subscribe))
    telegram_app.add_handler(CommandHandler("unsubscribe", unsubscribe))
    telegram_app.add_handler(CommandHandler("status", status))
    telegram_app.add_handler(CallbackQueryHandler(setcity_callback, pattern=r"^setcity:\d+$"))
    telegram_app.add_handler(MessageHandler(filters.COMMAND, unknown))

    logger.info("Bot starting (long polling)...")
    telegram_app.run_polling()


if __name__ == "__main__":
    main()
