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
CONTEXT_N = 30

# Active "in-the-chat" window
ACTIVE_WINDOW_SECONDS = 60 * 60   # ✅ 1 hour after being called / engaged

# Queue / pacing
QUEUE_WORKER_EVERY = 2.0
BATCH_WINDOW_SECONDS = 6.0
MAX_BATCH_ITEMS = 4
SEND_COOLDOWN_SECONDS = 6.0       # не чаще 1 ответа раз в ~6 сек на чат

# Gentle auto interject (low)
AUTO_INTERJECT_CHANCE = 0.08
BOT_COOLDOWN_IN_HANDLER = 0.8     # handler almost never replies; worker does

# Daily ping rules
SILENCE_HOURS_FOR_PING = 18
PING_WINDOW_START = 10
PING_WINDOW_END = 22
MORNING_PING_HOUR = 7
MORNING_PING_PROB = 0.15
PING_CHECK_EVERY_SECONDS = 60

# ==========================
# STATE
# ==========================
@dataclass
class PendingItem:
    ts: float
    chat_id: int
    message_id: int
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

    # bot activity
    active_until_ts: float = 0.0
    last_sent_ts: float = 0.0

    # context
    memory: deque = field(default_factory=lambda: deque(maxlen=CONTEXT_N))

    # queue
    queue: deque = field(default_factory=deque)

    # ping
    last_ping_ts: float = 0.0

chat_states: dict[int, ChatState] = defaultdict(ChatState)

# ==========================
# HEURISTICS
# ==========================
CALL_WORDS = ["ігнат", "арбітр", "суддя", "модер", "модератор", "бот"]

# Мы ловим эскалацию/наезд по действиям и общему тону.
# (без слуров и без прицельных “ты …” как цель)
ATTACK_MARKERS = [
    "заткнись", "завались", "відвали", "йди нах", "пішов нах",
    "соси", "пішов ти", "та пішов", "нахуй", "нах*й",
    "хуй", "залуп", "пизд", "пізд", "пизда", "пізда",
    "сука", "єбан", "йоб", "бля", "бляха",
    "придур", "ідіот", "дебіл", "клоун"
]

DEFENSE_MARKERS = [
    "я не", "ти не так", "шо ти", "чого ти", "та не", "серйозно?", "я взагалі",
    "поясню", "не треба", "давай без", "спокійно", "ти про шо", "я просто",
    "ти мене не так", "я не хотів", "не наїжджай"
]

# ==========================
# PROMPTS
# ==========================
MOD_SYSTEM = """
Ти — дорослий модератор українського групового чату.

Ціль: зупиняти наїзди/ескалацію і тримати рамки.

Правила:
- ЛИШЕ українською.
- 1–2 короткі репліки.
- У конфлікті: нуль жартів/флірту/підйобів.
- Принцип: РУГАЄШ ДІЮ/ПОВЕДІНКУ, А НЕ ЛЮДИНУ.
  (тобто: "це хамство/це зайве/не переходь на особисте", а не "ти такий-то".)
- Вистав межу: "без особистого", "по фактах", "переформулюй".
- Якщо тон культурний, але хтось явно захищається — коротко: "що сталося?" і поверни на факти.

Заборони:
- Без слурів/мови ненависті.
- Без погроз реальної шкоди.
- Не кажи, що ти бот/ШІ.
"""

TROLL_SYSTEM = """
Ти — арбітр з характером у дружньому українському чаті.
Ти підтримуєш діалог, інколи грубо і з матом, але тримаєш правило:
РУГАЄШ ДІЮ/ПОВЕДІНКУ, А НЕ ЛЮДИНУ.

Правила:
- ЛИШЕ українською.
- 1–2 короткі репліки (інколи 3, якщо треба).
- Мат дозволений, але без слурів/мови ненависті.
- Не перетворюйся на флуд: якщо багато людей пишуть — почни з "тихо-тихо" і відповідай по черзі або одним коментом на всіх.
- Якщо бачиш конфлікт — перемикайся в режим модератора (сухо, по рамкам).

Заборони:
- Погрози/насильство — ні.
- Прицільне приниження людини (“ти …”) — ні.
- Не кажи, що ти бот/ШІ.
"""

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
        if len(t) > 260:
            t = t[:260] + "…"
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
        if len(p) > 240:
            p = p[:240].rstrip() + "…"
        trimmed.append(p)

    r = random.random()
    limit = 1 if r < 0.55 else (2 if r < 0.9 else 3)
    return trimmed[:limit] if trimmed else ["Ок."]

async def llm(system: str, user: str, max_tokens: int = 160) -> str:
    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=1.05,
            max_tokens=max_tokens,
            presence_penalty=0.6,
            frequency_penalty=0.4,
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

    # context memory
    state.memory.append((name, text))

    # commands
    if await handle_commands(message, low, state):
        return
    if not state.enabled:
        return

    # handler pacing: worker answers
    if state.last_sent_ts and (now - state.last_sent_ts) < BOT_COOLDOWN_IN_HANDLER:
        pass

    me = await bot.me()
    bot_username = (me.username or "").strip()

    is_call = called_bot(low, bot_username)
    is_conflict = looks_like_attack(low)
    is_def = looks_like_defense(low)

    # activate 1 hour when called / conflict / strong defensive vibe
    if is_call or is_conflict:
        state.active_until_ts = max(state.active_until_ts, now + ACTIVE_WINDOW_SECONDS)

    in_active = now < state.active_until_ts
    auto = (not in_active) and (random.random() < AUTO_INTERJECT_CHANCE)

    if is_call or is_conflict or is_def or in_active or auto:
        state.queue.append(PendingItem(
            ts=now,
            chat_id=chat_id,
            message_id=message.message_id,
            user_id=u.id,
            user_name=name,
            text=text,
            is_call=is_call,
            is_conflict=is_conflict,
            is_defensive=is_def,
        ))

# ==========================
# WORKER: reply with batching and "тихо-тихо"
# ==========================
async def chat_worker_loop():
    while True:
        await asyncio.sleep(QUEUE_WORKER_EVERY)
        now = now_ts()

        for chat_id, state in list(chat_states.items()):
            if not state.enabled:
                continue
            if not state.queue:
                continue

            # send cooldown
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
            has_defense = any(x.is_defensive for x in batch)
            called = any(x.is_call for x in batch)

            ctx = format_context(chat_id)

            unique_users = list({x.user_id for x in batch})
            many_people = len(unique_users) >= 3

            incoming_lines = []
            for x in batch:
                t = x.text
                if len(t) > 220:
                    t = t[:220] + "…"
                incoming_lines.append(f"{x.user_name}: {t}")
            incoming_block = "\n".join(incoming_lines)

            # choose system
            if has_conflict:
                system = MOD_SYSTEM
                task = "Зупини ескалацію. Ругай дію/поведінку, а не людину."
            else:
                system = TROLL_SYSTEM
                task = "Підтримай діалог. Ругай дію/поведінку, а не людину."

            # guidance for crowd
            crowd_note = ""
            if many_people:
                crowd_note = "Якщо багато людей одночасно — почни з 'тихо-тихо' і відповідай по черзі або одним коментом на всіх.\n"

            prompt = (
                f"Контекст:\n{ctx}\n\n"
                f"Останні повідомлення:\n{incoming_block}\n\n"
                f"{crowd_note}"
                f"Завдання: {task}"
            )

            reply = await llm(system, prompt, max_tokens=180)
            if not reply:
                continue

            out_lines = split_short(reply)

            # prepend calming line if crowd and not already present
            if many_people:
                head = out_lines[0].lower()
                if "тихо" not in head and "спокій" not in head:
                    out_lines = ["Тихо-тихо. По черзі."] + out_lines[:2]

            for line in out_lines:
                await bot.send_message(chat_id, line)
                await asyncio.sleep(random.uniform(0.4, 1.2))

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

            silence = now - (state.last_activity_ts or 0)
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
