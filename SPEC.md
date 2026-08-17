# Weather Telegram Bot — Build Spec

**Status:** Draft — ready to build
**Purpose of this file:** This is the source of truth for what the bot does. Read this file before writing any code. If anything here is ambiguous or contradictory, ask before building rather than guessing.

---

## 1. Summary

A personal Telegram bot that sends me a daily weather briefing at a time I choose, and also answers on demand when I ask. The briefing is not just numbers — it tells me what to actually wear and carry today.

Single user (me). No multi-user support, no accounts, no web interface.

> **Assumption — change these two lines to your own values:**
> Default city: **Yerevan, Armenia** (lat 40.1792, lon 44.4991)
> Units: **Celsius**, wind in km/h, 24-hour clock
> Timezone: **Asia/Yerevan**

---

## 2. Commands

| Command | What it does |
|---|---|
| `/start` | Welcome message, short explanation, lists the other commands |
| `/weather` | Sends today's briefing immediately |
| `/tomorrow` | Sends tomorrow's briefing |
| `/subscribe HH:MM` | Schedules the daily briefing at that local time. Replaces any existing schedule. Example: `/subscribe 07:30` |
| `/unsubscribe` | Stops the daily briefing |
| `/setcity <name>` | Changes the location. Example: `/setcity Tbilisi` |
| `/status` | Shows current city and scheduled time, or says none is set |
| `/help` | Same as `/start` |

**Behaviour on bad input:** Never crash and never stay silent. If `/subscribe 25:99` or `/subscribe` with no time is sent, reply with a plain-language correction and an example. If `/setcity` finds no match, say the city wasn't found and ask for a different spelling. If it finds several matches, list up to three with their country and ask which one.

**Anything unrecognised:** Reply with a short nudge pointing to `/help`. Do not ignore it.

---

## 3. The briefing message

Structure, in this order:

1. **One-line headline** — city, date, and the single most important thing about today (e.g. rain coming, unusually cold, nothing notable)
2. **The numbers** — current temperature, feels-like, today's high and low, chance of rain, wind
3. **What to wear** — a short sentence, not a list
4. **What to carry** — only if there's something to carry
5. **One heads-up** — only if a rule below fires

Keep it short enough to read on a lock screen. Use a small number of emoji as visual anchors (one per section at most), not decoration. Never send an empty section — if there's nothing to carry, omit that line entirely rather than writing "nothing needed".

---

## 4. Tip rules

These are the heart of the bot. Evaluate all of them; include the ones that fire.

### Clothing (based on the day's feels-like temperature range)

| Feels-like | Advice |
|---|---|
| Below -5°C | Serious winter gear — insulated coat, hat, gloves, scarf |
| -5°C to 4°C | Winter coat and a hat |
| 5°C to 12°C | Warm jacket; a layer underneath you can remove |
| 13°C to 18°C | Light jacket or a jumper |
| 19°C to 25°C | Comfortable — no jacket needed |
| Above 25°C | Light, breathable clothing |

**Layering override:** if the difference between today's high and low feels-like is 10°C or more, add a note that the day swings a lot and to dress in layers, whatever the band above says.

### Carry

- **Umbrella** — chance of precipitation is 40% or higher at any point during daylight hours. If it's above 70%, phrase it more firmly.
- **Sunglasses / sunscreen** — UV index reaches 6 or higher.
- **Water** — high feels-like above 30°C.

### Heads-ups

- **Wind** — gusts above 40 km/h: mention it (umbrellas are useless above this; say so if an umbrella was also recommended).
- **Ice** — temperature at or below 0°C *and* any precipitation in the last 12 hours or forecast: warn about slippery ground.
- **Big change from yesterday** — if today's high differs from yesterday's by 8°C or more, say so. This is the tip I'll find most useful, because it's the thing I get wrong.
- **Rain timing** — if rain is expected, say roughly when (morning / afternoon / evening) rather than just that it will rain.

---

## 5. Data source

Use **Open-Meteo** (`api.open-meteo.com`). No API key, no signup, free for non-commercial use.

- Use its geocoding endpoint to turn a city name into coordinates for `/setcity`
- Request daily and hourly values in one call, with `timezone` set explicitly — never rely on the default
- Cache the response for 15 minutes so repeated `/weather` calls don't re-fetch

If the API is unreachable or returns an error, send a plain message saying weather data is temporarily unavailable and to try again shortly. Do not send a stack trace. Do not fail silently on the scheduled send.

---

## 6. Storage

SQLite, one small file, one row of settings:

- `chat_id`
- `city_name`, `latitude`, `longitude`
- `scheduled_time` (nullable — null means unsubscribed)
- `timezone`

Settings must survive a restart. If the bot restarts, any existing schedule resumes automatically without me re-subscribing.

---

## 7. Secrets

The Telegram bot token goes in a `.env` file, loaded at startup. `.env` must be listed in `.gitignore` before the first commit. Include a `.env.example` showing the variable names with placeholder values, so the real file is never needed in the repo.

The token must never appear in code, in commit history, in log output, or in an error message.

---

## 8. Non-goals

Deliberately not building these. Do not add them unprompted.

- Multiple users or user accounts
- More than one scheduled time per day
- Severe weather alerts or push warnings
- Historical weather or charts
- Multiple cities at once
- A web dashboard or admin panel
- Any AI/LLM call — the tips are rule-based, and that's intentional

---

## 9. Technical notes

- **Python 3.12+**, `python-telegram-bot` for the Telegram side, `APScheduler` for the scheduling
- **Long polling**, not webhooks — simpler, and fine for one user
- Runs as a single always-on process; must survive being restarted by the host at any moment
- Log to standard output so the hosting platform captures it; log every scheduled send and every failure

**Build order — do not skip ahead:**

1. `/start` and `/weather` working locally, replying with real data
2. The tip rules, with the message formatted properly
3. `/setcity` and storage
4. `/subscribe`, `/unsubscribe`, `/status` and the scheduler
5. Error handling pass across everything
6. Deployment config

Confirm each step works in Telegram before starting the next.

---

## 10. Done means

- I message `/weather` and get a readable briefing in under 5 seconds
- I set `/subscribe 07:30` and receive the briefing at 07:30 the next morning without touching anything
- I close my laptop and it still arrives
- Every command has been sent bad input at least once and none of them crashed the bot

---

## 11. Open questions — resolved

- **Skip logic:** Always send the daily briefing, even on days with nothing notable (e.g. "nothing notable today").
- **`/tomorrow`:** Manual command only. No automatic evening send.
- **Time format:** 24-hour (`07:30`) everywhere in messages, consistent with section 1's default.
- **Hosting:** Not decided yet. Build for local run first (venv + `.env`); deployment config (build step 6) will be scoped once a host is chosen.