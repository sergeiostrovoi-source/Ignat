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
CONTEXT_N = 60
ACTIVE_WINDOW_SECONDS = 60 * 60  # 1 година активного режиму

# черга / батчинг
QUEUE_WORKER_EVERY = 1.4
BATCH_WINDOW_SECONDS = 8.0
MAX_BATCH_ITEMS = 7
SEND_COOLDOWN_SECONDS = 4.8

# авто-вкиди
AUTO_INTERJECT_CHANCE = 0.18
AUTO_INTERJECT_MIN_GAP = 8 * 60

# локальний anti-silence
NUDGE_SILENCE_MINUTES = 45
NUDGE_CHECK_EVERY_SECONDS = 120
NUDGE_PROB = 0.55
NUDGE_WINDOW_START = 10
NUDGE_WINDOW_END = 23

# daily low-activity ping
LOW_ACTIVITY_WINDOW_HOURS = 24
LOW_ACTIVITY_MAX_MESSAGES = 3
DAILY_LOW_ACTIVITY_WINDOW_START = 10
DAILY_LOW_ACTIVITY_WINDOW_END = 22
DAILY_LOW_ACTIVITY_CHECK_EVERY_SECONDS = 300  # 5 хв
MAX_LOW_ACTIVITY_PINGS_PER_24H = 1

# daily long silence ping
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
    is_auto: bool = False

@dataclass
class ChatState:
    enabled: bool = True
    last_activity_ts: float = 0.0

    active_until_ts: float = 0.0
    last_sent_ts: float = 0.0
    last_auto_ts: float = 0.0

    memory: deque = field(default_factory=lambda: deque(maxlen=CONTEXT_N))
    queue: deque = field(default_factory=deque)

    last_ping_ts: float = 0.0
    last_low_activity_ping_ts: float = 0.0

    # для підрахунку активності за 24 години
    activity_timestamps: deque = field(default_factory=deque)

chat_states: dict[int, ChatState] = defaultdict(ChatState)

# ==========================
# HEURISTICS
# ==========================
CALL_WORDS = ["ігнат", "арбітр", "суддя", "модер", "модератор", "бот"]

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

# ==========================
# PROMPTS
# ==========================
MOD_SYSTEM = """
Ти — жорсткий арбітр українського групового чату.
Твоя задача: гасити конфлікти, ставити рамки, швидко охолоджувати.

Правила:
- ЛИШЕ українською.
- Коротко: 1–2 репліки.
- Мат дозволений.
- Переважно ругай дію/поведінку, а не людину.
- У конфлікті нуль флірту і нуль дурних жартів.
- Якщо хтось захищається культурно — коротко спитай, що сталося, і поверни всіх до фактів.

Заборони:
- Без мови ненависті.
- Без погроз реальної шкоди.
- Не кажи, що ти бот/ШІ.
"""

TROLL_SYSTEM = """
Ти — злий, язвительный, дуже неприємний троль українського чату, який додає красок у тишу.
Ти дотепний, колючий, з матом, але не перетворюєш чат у травлю.

Правила:
- ЛИШЕ українською.
- 1–2 короткі репліки, інколи 3.
- Мат можна вільно.
- Частіше ругай дію/поведінку.
- Іноді можеш жорстко вдарити по статусу в моменті: "слабко", "дрібно", "аргумент нуль".
- Якщо багато людей пишуть одночасно — почни з "Тихо-тихо." і відповідай по черзі або одним коментом на всіх.
- Якщо бачиш конфлікт — перемикайся в сухий MOD-режим.

Заборони:
- Без мови ненависті.
- Без погроз/насильства.
- Не кажи, що ти бот/ШІ.
"""

NUDGE_SEEDS = [
    "Ну шо, чат. Тиша — це ви так дружите? 😏",
    "Ви там живі? Бо виглядає як кладовище повідомлень.",
    "Скучно. Давайте хоч один нормальний тейк, а не мовчанку.",
    "Хто з вас сьогодні головний по здоровому глузду?",
    "Я відчуваю брак драматургії. Де ваші пригоди?",
    "О, тиша. Самий час комусь ляпнути щось розумне. Спробуйте.",
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

LOW_ACTIVITY_PINGS = [
    "Альо, де всі? Дайте хоч знак, що живі.",
    "Щось чат підозріло тихий. Всі цілі?",
    "Ей, народ, відпишіться хоч хтось. Бо тиша вже нездорова.",
    "Де ви поділись? Чат виглядає так, ніби всі випарувались.",
    "Ну й тиша. Хто живий — маякніть.",
    "Чат здох чи що? Хоч один відпишіться.",
    "Альо, де всі? Дайте знати, що живі, бо я вже починаю переживати.",
]

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
        if len(t) > 300:
            t = t[:300] + "…"
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
        if len(p) > 280:
            p = p[:280].rstrip() + "…"
        trimmed.append(p)

    r = random.random()
    limit = 1 if r < 0.42 else (2 if r < 0.86 else 3)
    return trimmed[:limit] if trimmed else ["Ок."]

async def llm(system: str, user: str, max_tokens: int = 220) -> str:
    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=1.10,
            max_tokens=max_tokens,
            presence_penalty=0.7,
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

def trim_activity(state: ChatState, now: float):
    cutoff = now - (LOW_ACTIVITY_WINDOW_HOURS * 3600)
    while state.activity_timestamps and state.activity_timestamps[0] < cutoff:
        state.activity_timestamps.popleft()

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
            await message.reply("Ок, я в строю. Не розслабляйтесь.")
        else:
            await message.reply("Тільки адміни можуть мене вмикати.")
        return True

    if low.startswith("/status"):
        st = "ON ✅" if state.enabled else "OFF ⛔"
        await message.reply(f"Статус: {st}")
        return True

    if low.startswith("/wake"):
        if await is_admin(chat_id, u.id):
            state.active_until_ts = max(state.active_until_ts, now_ts() + ACTIVE_WINDOW_SECONDS)
            state.queue.append(PendingItem(
                ts=now_ts(),
                chat_id=chat_id,
                user_id=u.id,
                user_name=(u.full_name or u.username or "Хтось"),
                text=random.choice(NUDGE_SEEDS),
                is_auto=True
            ))
            await message.reply("Ок. Зараз піддам газу.")
        else:
            await message.reply("Тільки адміни можуть /wake.")
        return True

    return False

# ==========================
# MESSAGE HANDLER
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
    state.activity_timestamps.append(now)
    trim_activity(state, now)

    text = message.text.strip()
    low = lc_text(text)

    u = message.from_user
    name = (u.full_name or u.username or "Хтось").strip()

    state.memory.append((name, text))

    if await handle_commands(message, low, state):
        return
    if not state.enabled:
        return

    me = await bot.me()
    bot_username = (me.username or "").strip()

    is_call = called_bot(low, bot_username)
    is_conflict = looks_like_attack(low)
    is_def = looks_like_defense(low)

    if is_call or is_conflict or is_def:
        state.active_until_ts = max(state.active_until_ts, now + ACTIVE_WINDOW_SECONDS)

    in_active = now < state.active_until_ts
    auto_ok = (now - state.last_auto_ts) >= AUTO_INTERJECT_MIN_GAP
    auto = auto_ok and (random.random() < (AUTO_INTERJECT_CHANCE * (0.6 if in_active else 1.0)))

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
            is_auto=auto
        ))
        if auto:
            state.last_auto_ts = now

# ==========================
# WORKER
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
            has_def = any(x.is_defensive for x in batch)
            system = MOD_SYSTEM if (has_conflict or (has_def and random.random() < 0.6)) else TROLL_SYSTEM

            uniq_users = {x.user_id for x in batch}
            many_people = len(uniq_users) >= 3

            ctx = format_context(chat_id)
            incoming_lines = []
            for x in batch:
                t = x.text
                if len(t) > 260:
                    t = t[:260] + "…"
                incoming_lines.append(f"{x.user_name}: {t}")
            incoming_block = "\n".join(incoming_lines)

            crowd_note = ""
            if many_people:
                crowd_note = "Багато людей одночасно: почни з 'Тихо-тихо.' і розклади відповідь по черзі або одним коментом на всіх.\n"

            prompt = (
                f"Контекст:\n{ctx}\n\n"
                f"Нові репліки:\n{incoming_block}\n\n"
                f"{crowd_note}"
                f"Відповідай коротко у вибраному стилі."
            )

            reply = await llm(system, prompt, max_tokens=240)
            if not reply:
                continue

            out_lines = split_short(reply)
            if many_people:
                head = out_lines[0].lower()
                if "тихо" not in head and "спокій" not in head:
                    out_lines = ["Тихо-тихо. По черзі."] + out_lines[:2]

            for line in out_lines:
                await bot.send_message(chat_id, line)
                await asyncio.sleep(random.uniform(0.35, 1.05))

            state.last_sent_ts = now_ts()

# ==========================
# NUDGE LOOP
# ==========================
def in_nudge_window(dt: datetime) -> bool:
    return NUDGE_WINDOW_START <= dt.hour < NUDGE_WINDOW_END

async def nudge_loop():
    while True:
        await asyncio.sleep(NUDGE_CHECK_EVERY_SECONDS)
        now = now_ts()
        dt = datetime.fromtimestamp(now, TZ)

        if not in_nudge_window(dt):
            continue

        for chat_id, state in list(chat_states.items()):
            if not state.enabled:
                continue

            silence = now - (state.last_activity_ts or 0.0)
            if silence < (NUDGE_SILENCE_MINUTES * 60):
                continue

            if (now - state.last_auto_ts) < AUTO_INTERJECT_MIN_GAP:
                continue

            if random.random() > NUDGE_PROB:
                continue

            state.active_until_ts = max(state.active_until_ts, now + ACTIVE_WINDOW_SECONDS)
            state.queue.append(PendingItem(
                ts=now,
                chat_id=chat_id,
                user_id=0,
                user_name="",
                text=random.choice(NUDGE_SEEDS),
                is_auto=True
            ))
            state.last_auto_ts = now

# ==========================
# DAILY LOW ACTIVITY LOOP
# ==========================
def in_daily_low_activity_window(dt: datetime) -> bool:
    return DAILY_LOW_ACTIVITY_WINDOW_START <= dt.hour < DAILY_LOW_ACTIVITY_WINDOW_END

def low_activity_ping_limit_ok(state: ChatState, now: float) -> bool:
    if state.last_low_activity_ping_ts <= 0:
        return True
    return (now - state.last_low_activity_ping_ts) >= 24 * 60 * 60

async def low_activity_loop():
    while True:
        await asyncio.sleep(DAILY_LOW_ACTIVITY_CHECK_EVERY_SECONDS)
        now = now_ts()
        dt = datetime.fromtimestamp(now, TZ)

        if not in_daily_low_activity_window(dt):
            continue

        for chat_id, state in list(chat_states.items()):
            if not state.enabled:
                continue

            trim_activity(state, now)
            msg_count = len(state.activity_timestamps)

            if msg_count > LOW_ACTIVITY_MAX_MESSAGES:
                continue

            if not low_activity_ping_limit_ok(state, now):
                continue

            try:
                await bot.send_message(chat_id, random.choice(LOW_ACTIVITY_PINGS))
                state.last_low_activity_ping_ts = now
                state.last_sent_ts = now
            except TelegramBadRequest:
                pass

# ==========================
# LONG SILENCE PING LOOP
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
    asyncio.create_task(nudge_loop())
    asyncio.create_task(low_activity_loop())
    asyncio.create_task(ping_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
