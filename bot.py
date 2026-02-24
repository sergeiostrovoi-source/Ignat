import os
import asyncio
import random
from collections import defaultdict, deque
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher
from aiogram.types import Message
from aiogram.enums import ChatType

from openai import OpenAI

# =========================
# ENV
# =========================
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

if not TOKEN or not OPENAI_API_KEY:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or OPENAI_API_KEY")

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

bot = Bot(token=TOKEN)
dp = Dispatcher()
client = OpenAI(api_key=OPENAI_API_KEY)

# =========================
# SETTINGS
# =========================
# Hard caps (safety)
MAX_MSGS_PER_DAY = int(os.getenv("IG_MAX_MSGS_PER_DAY", "7"))
MIN_INTERVAL_MINUTES = int(os.getenv("IG_MIN_INTERVAL_MINUTES", "60"))  # at least 1h between Ignat messages

# Silence nudges
SILENCE_HOURS = float(os.getenv("IG_SILENCE_HOURS", "10"))
MAX_SILENCE_NUDGES_PER_WEEK = int(os.getenv("IG_MAX_SILENCE_NUDGES_PER_WEEK", "4"))

# Context
HISTORY_WINDOW = int(os.getenv("IG_HISTORY_WINDOW", "8"))
OPENAI_MAX_TOKENS = int(os.getenv("IG_OPENAI_MAX_TOKENS", "80"))
OPENAI_TEMPERATURE = float(os.getenv("IG_OPENAI_TEMPERATURE", "0.85"))

# Trigger window for simple heuristics
TRIGGER_WINDOW = int(os.getenv("IG_TRIGGER_WINDOW", "10"))

# =========================
# IGNAT PERSONA (UA)
# =========================
IGNAT_SYSTEM_PROMPT = """
Ти — Ігнат. Персонаж групового чату (іронічний контраст з аватаром Сократа).

Ти не філософ-вчитель і не мораліст.
Ти спокійний спостерігач, який говорить рідко й влучно.

Твоя роль:
— іноді легко підколоти
— іноді змінити ракурс розмови
— іноді запустити легкий рух, якщо чат завмер

Тон:
спокійний, трохи загадковий, з легкою іронією.
Без пафосу. Без повчань. Без довгих пояснень.

Жорсткі правила:
1) Відповідь максимум 1–2 рядки.
2) Не обирай сторону. Не вирішуй, хто правий.
3) Не переходь на особистості.
4) Не використовуй складні терміни.
5) Не пояснюй свої висновки.
6) Ніколи не кажи, що ти бот.
7) Якщо контекст слабкий — краще коротко або мовчи.

Вихід: поверни тільки текст репліки Ігната, без лапок і без пояснень.
""".strip()

# =========================
# CANNED LINES (UA) - cheap variety
# =========================
CANNED_SILENCE = [
    "Сьогодні тут підозріло тихо.",
    "Я очікував продовження.",
    "І все? Більше думок немає?",
    "Пауза теж відповідь. Але нудна.",
    "Ви мене дивуєте: пів дня без розгону.",
    "Хтось явно недоговорив.",
    "Продовжуйте. Майже цікаво.",
]
CANNED_SELF_IRONY = [
    "Від мене чекають більшого. Дарма.",
    "Сьогодні без лекцій.",
    "Розчарую: глибини не буде.",
    "Я би сказав розумніше. Але не буду.",
]

# =========================
# STATE
# =========================
# History per chat: deque of (ts, author, text)
chat_history = defaultdict(lambda: deque(maxlen=120))

# For auto-adaptation: keep timestamps of messages in last 24h
chat_activity_ts = defaultdict(lambda: deque(maxlen=5000))

# Stats per chat
chat_stats = defaultdict(lambda: {
    "msg_count_since_ignat": 0,
    "last_activity": datetime.utcnow(),
    "daily_count": 0,
    "last_reset": datetime.utcnow().date(),
    "last_ignat_time": None,
    "weekly_silence_nudges": 0,
    "weekly_reset": datetime.utcnow().date(),  # reset weekly counter each 7 days
})

# =========================
# HELPERS
# =========================
def reset_daily_if_needed(chat_id: int) -> None:
    today = datetime.utcnow().date()
    if chat_stats[chat_id]["last_reset"] != today:
        chat_stats[chat_id]["daily_count"] = 0
        chat_stats[chat_id]["last_reset"] = today

def reset_weekly_if_needed(chat_id: int) -> None:
    today = datetime.utcnow().date()
    last = chat_stats[chat_id]["weekly_reset"]
    if (today - last).days >= 7:
        chat_stats[chat_id]["weekly_silence_nudges"] = 0
        chat_stats[chat_id]["weekly_reset"] = today

def cleanup_activity_24h(chat_id: int) -> None:
    """Keep only last 24h timestamps."""
    now = datetime.utcnow()
    dq = chat_activity_ts[chat_id]
    cutoff = now - timedelta(hours=24)
    while dq and dq[0] < cutoff:
        dq.popleft()

def messages_last_24h(chat_id: int) -> int:
    cleanup_activity_24h(chat_id)
    return len(chat_activity_ts[chat_id])

def adaptive_min_messages(chat_id: int) -> int:
    """
    Simple auto-adaptation based on messages in last 24h.
    - Very active chat: raise threshold (be quieter)
    - Medium: normal
    - Quiet: lower threshold (be more present)
    """
    m24 = messages_last_24h(chat_id)
    if m24 > 80:
        return 25
    elif m24 > 40:
        return 15
    else:
        return 8

def is_direct_mention(text: str) -> bool:
    t = (text or "").lower()
    return "ігнат" in t or "ignat" in t

def detect_silence(chat_id: int) -> bool:
    last = chat_stats[chat_id]["last_activity"]
    return (datetime.utcnow() - last) > timedelta(hours=SILENCE_HOURS)

def detect_activity_pattern(chat_id: int) -> bool:
    """
    Lightweight heuristic: last 6 messages have >=2 distinct authors
    AND at least 6 messages exist recently.
    """
    msgs = list(chat_history[chat_id])[-6:]
    if len(msgs) < 6:
        return False
    authors = {a for _, a, _ in msgs}
    return len(authors) >= 2

def too_soon_since_last_ignat(chat_id: int) -> bool:
    last = chat_stats[chat_id]["last_ignat_time"]
    if not last:
        return False
    return (datetime.utcnow() - last) < timedelta(minutes=MIN_INTERVAL_MINUTES)

def can_ignat_speak(chat_id: int) -> bool:
    reset_daily_if_needed(chat_id)
    reset_weekly_if_needed(chat_id)

    stats = chat_stats[chat_id]
    if stats["daily_count"] >= MAX_MSGS_PER_DAY:
        return False
    if too_soon_since_last_ignat(chat_id):
        return False
    return True

def should_intervene_by_volume(chat_id: int) -> bool:
    """
    Must have enough messages since last Ignat, with adaptive threshold.
    """
    threshold = adaptive_min_messages(chat_id)
    return chat_stats[chat_id]["msg_count_since_ignat"] >= threshold

def mark_ignat_spoke(chat_id: int) -> None:
    chat_stats[chat_id]["daily_count"] += 1
    chat_stats[chat_id]["msg_count_since_ignat"] = 0
    chat_stats[chat_id]["last_ignat_time"] = datetime.utcnow()

async def openai_reply(chat_id: int) -> str | None:
    """
    Generate a short UA reply from Ignat given last HISTORY_WINDOW messages.
    """
    msgs = list(chat_history[chat_id])[-HISTORY_WINDOW:]
    if not msgs:
        return None

    convo = "\n".join([f"{author}: {text}" for _, author, text in msgs])

    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=OPENAI_TEMPERATURE,
            max_tokens=OPENAI_MAX_TOKENS,
            messages=[
                {"role": "system", "content": IGNAT_SYSTEM_PROMPT},
                {"role": "user", "content": convo},
            ],
        )
        out = (resp.choices[0].message.content or "").strip()
        # Hard guard: ensure at most 2 lines
        out_lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        out = "\n".join(out_lines[:2]).strip()
        return out if out else None
    except Exception as e:
        print("OpenAI error:", e)
        return None

async def send_ignat(chat_id: int, text: str, reply_to: Message | None = None) -> None:
    if not text:
        return
    # Occasionally add a tiny self-ironic tail (rare)
    if random.random() < 0.05:
        text = text + "\n" + random.choice(CANNED_SELF_IRONY)

    if reply_to:
        await reply_to.reply(text)
    else:
        await bot.send_message(chat_id, text)

    mark_ignat_spoke(chat_id)

# =========================
# MAIN HANDLER
# =========================
@dp.message()
async def on_message(message: Message):
    # Only group chats
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    if message.from_user is None:
        return

    text = message.text or message.caption or ""
    if not text:
        return

    chat_id = message.chat.id
    author = message.from_user.full_name
    now = datetime.utcnow()

    # Save history
    chat_history[chat_id].append((now, author, text))

    # Activity timestamps for adaptive thresholds
    chat_activity_ts[chat_id].append(now)

    # Update chat stats
    chat_stats[chat_id]["last_activity"] = now
    chat_stats[chat_id]["msg_count_since_ignat"] += 1

    # 1) Direct mention -> reply (but still obey global frequency)
    if is_direct_mention(text) and can_ignat_speak(chat_id):
        reply = await openai_reply(chat_id)
        if reply:
            await send_ignat(chat_id, reply, reply_to=message)
        return

    # 2) Silence nudge (run only if can speak & weekly limit not exceeded)
    # Note: This will only trigger when SOMEONE writes after silence (no cron).
    if detect_silence(chat_id) and can_ignat_speak(chat_id):
        stats = chat_stats[chat_id]
        if stats["weekly_silence_nudges"] < MAX_SILENCE_NUDGES_PER_WEEK:
            # Use canned silence line sometimes (cheap) to save tokens
            if random.random() < 0.6:
                await send_ignat(chat_id, random.choice(CANNED_SILENCE))
            else:
                reply = await openai_reply(chat_id)
                if reply:
                    await send_ignat(chat_id, reply)
            stats["weekly_silence_nudges"] += 1
        return

    # 3) Normal intervention based on adaptive thresholds + simple activity pattern
    if not can_ignat_speak(chat_id):
        return

    if should_intervene_by_volume(chat_id) and detect_activity_pattern(chat_id):
        # Small chance to skip even if triggered, to preserve "rare presence"
        if random.random() < 0.35:
            return

        reply = await openai_reply(chat_id)
        if reply:
            await send_ignat(chat_id, reply)

# =========================
# RUN
# =========================
async def main():
    print("Ігнат запущений (UA, автоадаптація ввімкнена).")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
