import os
import random
import asyncio
import time
from dataclasses import dataclass, field
from collections import defaultdict, deque
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.types import Message
from aiogram.exceptions import TelegramBadRequest
from openai import OpenAI

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

bot = Bot(token=TOKEN)
dp = Dispatcher()

client = OpenAI(api_key=OPENAI_API_KEY)

MODEL = "gpt-4.1-mini"

TZ = ZoneInfo("Europe/Kiev")

CONTEXT_N = 40

BASE_REPLY_CHANCE = 0.03
TEASE_CHANCE = 0.03

BOT_SEND_COOLDOWN = 18
ACTIVE_WINDOW_SECONDS = 10 * 60

NUDGE_SILENCE_MINUTES = 120
NUDGE_MIN_GAP_SECONDS = 8 * 60 * 60

NUDGE_WINDOW_START = 10
NUDGE_WINDOW_END = 22

LOW_ACTIVITY_WINDOW_HOURS = 24
LOW_ACTIVITY_MAX_MESSAGES = 3


@dataclass
class ChatState:
    enabled: bool = True
    memory: deque = field(default_factory=lambda: deque(maxlen=CONTEXT_N))
    activity_timestamps: deque = field(default_factory=deque)

    last_activity_ts: float = 0
    last_sent_ts: float = 0
    active_until_ts: float = 0

    last_nudge_ts: float = 0
    last_low_activity_ping_ts: float = 0

    soften_until_ts: float = 0


chat_states: dict[int, ChatState] = defaultdict(ChatState)


CALL_WORDS = ["ігнат", "бот", "арбітр", "суддя"]


ATTACK_MARKERS = [
    "нахуй",
    "хуй",
    "пішов нах",
    "заткнись",
    "мудак",
    "дебіл",
    "ідіот",
]


DEFENSE_MARKERS = [
    "ти не прав",
    "це не так",
    "перегнув",
]


def now():
    return time.time()


def in_group(chat_type):
    return chat_type in ("group", "supergroup")


def lc(t):
    return (t or "").lower()


def called_bot(text, username):
    if username and f"@{username.lower()}" in text:
        return True
    for w in CALL_WORDS:
        if w in text:
            return True
    return False


def format_context(chat_id):

    mem = list(chat_states[chat_id].memory)

    lines = []

    for name, uid, txt in mem[-CONTEXT_N:]:

        t = txt.strip()

        if len(t) > 260:
            t = t[:260] + "…"

        lines.append(f"{name}: {t}")

    return "\n".join(lines)


def pick_recent_user(chat_id):

    mem = list(chat_states[chat_id].memory)

    seen = set()
    candidates = []

    for name, uid, txt in reversed(mem):

        if uid in seen:
            continue

        seen.add(uid)

        if not name:
            continue

        candidates.append((name, uid))

        if len(candidates) >= 8:
            break

    if not candidates:
        return None

    return random.choice(candidates)


def split_short(text):

    text = text.strip()

    parts = text.split("\n")

    parts = [p.strip() for p in parts if p.strip()]

    if len(parts) == 1:

        tmp = text.replace(". ", ".\n").replace("! ", "!\n").replace("? ", "?\n")

        parts = [p.strip() for p in tmp.split("\n") if p.strip()]

    return parts[:2]


async def llm(system, user, tokens=120):

    try:

        r = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.9,
            max_tokens=tokens,
        )

        return r.choices[0].message.content.strip()

    except:
        return ""


OBSERVER_SYSTEM = """
Ти учасник дружнього українського чату.

Ти поводишся як нормальна людина:
— підтримуєш розмову
— реагуєш на зміст
— не шуміти без причини

1–2 короткі репліки.
"""


PARTICIPANT_SYSTEM = """
Ти учасник українського чату з характером.

Можеш жартувати або підколоти,
але головне — підтримувати адекватну розмову.

1–2 короткі репліки.
"""


ARBITER_SYSTEM = """
Ти арбітр чату.

Коли починається наїзд —
ти коротко ставиш рамки
і повертаєш розмову в нормальний тон.
"""


APOLOGY_SYSTEM = """
Тебе аргументовано поправили.

Коротко визнай,
що ти перегнув.
"""


NUDGE_LINES = [
    "Панове, чат живий?",
    "Щось тихо тут.",
    "Народ, ви де всі?",
]


LOW_ACTIVITY_LINES = [
    "Альо, дайте знак що живі.",
    "Чат заснув?",
]


@dp.message()
async def handle(message: Message):

    if not in_group(message.chat.type):
        return

    if not message.text:
        return

    chat_id = message.chat.id

    state = chat_states[chat_id]

    now_ts = now()

    text = message.text

    low = lc(text)

    u = message.from_user

    name = u.username or u.first_name or "Хтось"

    state.memory.append((name, u.id, text))

    state.last_activity_ts = now_ts

    if not state.enabled:
        return

    if state.last_sent_ts and now_ts - state.last_sent_ts < BOT_SEND_COOLDOWN:
        return

    me = await bot.me()

    bot_username = me.username

    is_call = called_bot(low, bot_username)

    ctx = format_context(chat_id)

    if is_call:

        prompt = f"""
Контекст:

{ctx}

Останнє повідомлення:
{name}: {text}

Відповідай по суті.
"""

        reply = await llm(PARTICIPANT_SYSTEM, prompt)

        if reply:

            for line in split_short(reply):
                await message.reply(line)

            state.last_sent_ts = now_ts

        return

    if random.random() < TEASE_CHANCE:

        target = pick_recent_user(chat_id)

        if target:

            target_name, target_id = target

            prompt = f"""
Контекст:

{ctx}

Ти звертаєшся САМЕ до користувача {target_name}.
Не змінюй ім'я.

Коротко підколи або пожартуй.
"""

            reply = await llm(PARTICIPANT_SYSTEM, prompt)

            if reply:

                await message.reply(split_short(reply)[0])

                state.last_sent_ts = now_ts

        return

    if random.random() < BASE_REPLY_CHANCE:

        prompt = f"""
Контекст:

{ctx}

Останнє повідомлення:
{name}: {text}

Якщо доречно — коротко підтримай розмову.
"""

        reply = await llm(OBSERVER_SYSTEM, prompt)

        if reply:

            await message.reply(split_short(reply)[0])

            state.last_sent_ts = now_ts


async def main():

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
