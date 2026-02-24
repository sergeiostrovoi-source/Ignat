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

# ==========================
# ENV
# ==========================
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

if not TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY")

bot = Bot(token=TOKEN)
dp = Dispatcher()
client = OpenAI(api_key=OPENAI_API_KEY)

TZ = ZoneInfo("Europe/Kiev")
MODEL = "gpt-4.1-mini"

# ==========================
# CONFIG
# ==========================
CONTEXT_N = 45  # больше контекста

# 1 час активного режима после вызова/вмешательства
ACTIVE_WINDOW_SECONDS = 60 * 60

# Очередь и батчинг
QUEUE_WORKER_EVERY = 1.5
BATCH_WINDOW_SECONDS = 7.0
MAX_BATCH_ITEMS = 6
SEND_COOLDOWN_SECONDS = 5.5  # быстрее реагирует, но не флудит

# Самовключение (редко)
AUTO_INTERJECT_CHANCE = 0.10

# Daily ping
SILENCE_HOURS_FOR_PING = 18
PING_WINDOW_START = 10
PING_WINDOW_END = 22
MORNING_PING_HOUR = 7
MORNING_PING_PROB = 0.18
PING_CHECK_EVERY_SECONDS = 60

# ==========================
# STATE
# ==========================
@dataclass
class PendingItem:
    ts: float
    chat_id: int
    user_id: int
    user_name: str
    text: str
    is_call: bool = False
    is_conflict: bool = False
    is_defensive: bool = False

@dataclass
class ChatState:
    enabled: bool = True
    last_activity_ts: float = 0.0

    active_until_ts: float = 0.0
    last_sent_ts: float = 0.0

    memory: deque = field(default_factory=lambda: deque(maxlen=CONTEXT_N))
    queue: deque = field(default_factory=deque)

    last_ping_ts: float = 0.0

chat_states: dict[int, ChatState] = defaultdict(ChatState)

# ==========================
# HEURISTICS
# ==========================
CALL_WORDS = ["ігнат", "арбітр", "суддя", "модер", "модератор", "бот"]

# Триггеры эскалации/агрессии (без слуров)
ATTACK_MARKERS = [
    "заткнись", "завались", "відвали", "йди нах", "пішов нах",
    "соси", "нахуй", "нах*й",
    "хуй", "залуп", "пизд", "пізд", "пизда", "пізда",
    "сука", "єбан", "йоб", "бля", "бляха",
    "придур", "ідіот", "дебіл", "клоун", "мудак", "лох"
]

DEFENSE_MARKERS = [
    "я не", "ти не так", "шо ти", "чого ти", "та не", "серйозно?", "я взагалі",
    "поясню", "не треба", "давай без", "спокійно", "ти про шо", "я просто",
    "не наїжджай", "перестань", "досить", "чого ти до мене"
]

PING_TEXTS = [
    "Куди всі пропали, друзяки? 😄",
    "Ей, чат, ви живі там?",
    "Тиша така, що аж підозріло. Хто на зв’язку?",
    "Я щось скучив. Розкажіть, що нового?",
]
MORNING_TEXTS = [
    "Доброго ранку, друзяки ☕️",
    "Ранок. Прокидаємось, легенди 😄",
]

# ==========================
# PROMPTS (максимально жёстко, но без слуров/ненависти)
# ==========================
MOD_SYSTEM = """
Ти — жорсткий дорослий арбітр українського групового чату. Ти різкий і неприємний, але тримаєш рамки.

Ціль: зупиняти ескалацію і наїзди, ставити правила.

Правила:
- ЛИШЕ українською.
- Коротко: 1–2 репліки (інколи 3, якщо треба розвести натовп).
- Мат дозволений.
- Принцип: частіше ругай ДІЮ/ПОВЕДІНКУ, але інколи можеш жорстко бити по статусу в моменті
  ("виглядаєш жалюгідно", "це слабко", "логіка кульгає", "спроба домінувати — нуль"),
  НЕ використовуючи слури/мову ненависті.
- У конфлікті — нуль жартів/флірту. Сухо і владно.

Заборони:
- Без слурів/мови ненависті.
- Без погроз реальної шкоди.
- Не кажи, що ти бот/ШІ.
"""

TROLL_SYSTEM = """
Ти — максимально жорсткий арбітр-троль українського чату.
Ти підтримуєш діалог, відповідаєш швидко, грубо, з матом, але не скочуєшся в мову ненависті.

Правила:
- ЛИШЕ українською.
- 1–2 короткі репліки (інколи 3).
- Мат можна вільно (типу: хуй, залупа, пізда, хуй моржовий і т.п.), але без слурів/мови ненависті.
- Частіше ругай ДІЮ/ПОВЕДІНКУ. Іноді — по статусу в моменті ("це слабко", "виглядаєш жалюгідно", "аргумент нуль").
- Якщо багато людей пишуть одночасно — спочатку "Тихо-тихо.", потім відповідай по черзі або одним коментом на всіх.
- Якщо бачиш, що це конфлікт — перемикайся в сухий MOD-режим.

Заборони:
- Погрози/насильство — ні.
- Слури/мова ненависті — ні.
- Не кажи, що ти бот/ШІ.
"""

# ==========================
# HELPERS
# ==========================
def now_ts() -> float:
    return time.time()

def in_group(chat_type: str) -> bool:
    return chat_type in ("group", "supergroup")

def lc_text(t: str) -> str:
    return (t or "").strip().lower()

def called_bot(low: str, bot_username: str) -> bool:
    if bot_username and f"@{bot_username.lower()}" in low:
        return True
    return any(w in low for w in CALL_WORDS)

def looks_like_attack(low: str) -> bool:
    return any(w in low for w in ATTACK_MARKERS)

def looks_like_defense(low: str) -> bool:
    return any(w in low for w in DEFENSE_MARKERS)

def format_context(chat_id: int) -> str:
    mem = list(chat_states[chat_id].memory)
    lines = []
    for name, txt in mem[-CONTEXT_N:]:
        if not txt:
            continue
        t = txt.strip()
        if len(t) > 280:
            t = t[:280] + "…"
        lines.append(f"{name}: {t}")
    return "\n".join(lines)

def split_short(text: str) -> list[str]:
    raw = (text or "").replace("\r", "\n").strip()
    if not raw:
        return ["Ок."]

    parts = [p.strip() for p in raw.split("\n") if p.strip()]
    if len(parts) == 1:
        tmp = raw
        for sep in ["! ", "? ", ". ", "… "]:
            tmp = tmp.replace(sep, sep.strip() + "\n")
        parts = [p.strip() for p in tmp.split("\n") if p.strip()]

    trimmed = []
    for p in parts:
        if len(p) > 260:
            p = p[:260].rstrip() + "…"
        trimmed.append(p)

    r = random.random()
    limit = 1 if r < 0.45 else (2 if r < 0.88 else 3)
    return trimmed[:limit] if trimmed else ["Ок."]

async def llm(system: str, user: str, max_tokens: int = 200) -> str:
    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=1.07,
            max_tokens=max_tokens,
            presence_penalty=0.65,
            frequency_penalty=0.45,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        return ""

async def is_admin(chat_id: int, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        return m.status in ("administrator", "creator")
    except TelegramBadRequest:
        return False

# ==========================
# COMMANDS
# ==========================
async def handle_commands(message: Message, low: str, state: ChatState) -> bool:
    chat_id = message.chat.id
    u = message.from_user

    if low.startswith("/off"):
        if await is_admin(chat_id, u.id):
            state.enabled = False
            await message.reply("Ок. Я вимкнений у цьому чаті. Вмикати: /on")
        else:
            await message.reply("Тільки адміни можуть мене вимикати.")
        return True

    if low.startswith("/on"):
        if await is_admin(chat_id, u.id):
            state.enabled = True
            await message.reply("Ок, я в строю.")
        else:
            await message.reply("Тільки адміни можуть мене вмикати.")
        return True

    if low.startswith("/status"):
        st = "ON ✅" if state.enabled else "OFF ⛔"
        await message.reply(f"Статус: {st}")
        return True

    return False

# ==========================
# MESSAGE HANDLER (enqueue only)
# ==========================
@dp.message()
async def on_message(message: Message):
    if not in_group(message.chat.type):
        return
    if not message.text:
        return

    chat_id = message.chat.id
    state = chat_states[chat_id]
    now = now_ts()
    state.last_activity_ts = now

    text = message.text.strip()
    low = lc_text(text)

    u = message.from_user
    name = (u.full_name or u.username or "Хтось").strip()

    # memory
    state.memory.append((name, text))

    # commands
    if await handle_commands(message, low, state):
        return
    if not state.enabled:
        return

    me = await bot.me()
    bot_username = (me.username or "").strip()

    is_call = called_bot(low, bot_username)
    is_conflict = looks_like_attack(low)
    is_def = looks_like_defense(low)

    # activate window
    if is_call or is_conflict or is_def:
        state.active_until_ts = max(state.active_until_ts, now + ACTIVE_WINDOW_SECONDS)

    in_active = now < state.active_until_ts
    auto = (not in_active) and (random.random() < AUTO_INTERJECT_CHANCE)

    # enqueue if relevant
    if is_call or is_conflict or is_def or in_active or auto:
        state.queue.append(PendingItem(
            ts=now,
            chat_id=chat_id,
            user_id=u.id,
            user_name=name,
            text=text,
            is_call=is_call,
            is_conflict=is_conflict,
            is_defensive=is_def,
        ))

# ==========================
# WORKER: batching + crowd control
# ==========================
async def chat_worker_loop():
    while True:
        await asyncio.sleep(QUEUE_WORKER_EVERY)
        now = now_ts()

        for chat_id, state in list(chat_states.items()):
            if not state.enabled or not state.queue:
                continue

            if state.last_sent_ts and (now - state.last_sent_ts) < SEND_COOLDOWN_SECONDS:
                continue

            # batch
            batch = []
            first_ts = state.queue[0].ts
            while state.queue and len(batch) < MAX_BATCH_ITEMS:
                item = state.queue[0]
                if (item.ts - first_ts) <= BATCH_WINDOW_SECONDS:
                    batch.append(state.queue.popleft())
                else:
                    break

            if not batch:
                continue

            has_conflict = any(x.is_conflict for x in batch)
            # Если защитный вайб без явного мата/наезда — модераторский вопрос "шо сталося?"
            has_def = any(x.is_defensive for x in batch)

            # crowd?
            uniq_users = {x.user_id for x in batch}
            many_people = len(uniq_users) >= 3

            ctx = format_context(chat_id)
            incoming_lines = []
            for x in batch:
                t = x.text
                if len(t) > 240:
                    t = t[:240] + "…"
                incoming_lines.append(f"{x.user_name}: {t}")
            incoming_block = "\n".join(incoming_lines)

            # choose mode
            system = MOD_SYSTEM if (has_conflict or (has_def and random.random() < 0.55)) else TROLL_SYSTEM

            crowd_note = ""
            if many_people:
                crowd_note = "Багато людей одночасно. Почни з 'Тихо-тихо.' і відповідай по черзі або одним коментом на всіх.\n"

            prompt = (
                f"Контекст (останні репліки):\n{ctx}\n\n"
                f"Нові репліки:\n{incoming_block}\n\n"
                f"{crowd_note}"
                f"Відповідай у вибраному стилі, коротко."
            )

            reply = await llm(system, prompt, max_tokens=210)
            if not reply:
                continue

            out_lines = split_short(reply)

            if many_people:
                head = out_lines[0].lower()
                if "тихо" not in head and "спокій" not in head:
                    out_lines = ["Тихо-тихо. По черзі."] + out_lines[:2]

            for line in out_lines:
                await bot.send_message(chat_id, line)
                await asyncio.sleep(random.uniform(0.35, 1.1))

            state.last_sent_ts = now_ts()

# ==========================
# PING LOOP
# ==========================
def can_ping_now(dt: datetime) -> bool:
    if PING_WINDOW_START <= dt.hour < PING_WINDOW_END:
        return True
    if dt.hour == MORNING_PING_HOUR and random.random() < MORNING_PING_PROB:
        return True
    return False

def ping_limit_ok(state: ChatState, now: float) -> bool:
    if state.last_ping_ts <= 0:
        return True
    return (now - state.last_ping_ts) >= 24 * 60 * 60

async def ping_loop():
    while True:
        await asyncio.sleep(PING_CHECK_EVERY_SECONDS)
        now = now_ts()
        dt = datetime.fromtimestamp(now, TZ)

        if not can_ping_now(dt):
            continue

        for chat_id, state in list(chat_states.items()):
            if not state.enabled:
                continue
            if not ping_limit_ok(state, now):
                continue

            silence = now - (state.last_activity_ts or 0.0)
            if silence < SILENCE_HOURS_FOR_PING * 3600:
                continue

            txt = random.choice(MORNING_TEXTS) if dt.hour == MORNING_PING_HOUR else random.choice(PING_TEXTS)
            try:
                await bot.send_message(chat_id, txt)
                state.last_ping_ts = now
                state.last_sent_ts = now
            except TelegramBadRequest:
                pass

# ==========================
# START
# ==========================
async def main():
    asyncio.create_task(chat_worker_loop())
    asyncio.create_task(ping_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
