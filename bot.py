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
# BEHAVIOR CONFIG
# ==========================
CONTEXT_N = 18

# Troll dialog
DIALOG_TURNS_MIN = 3
DIALOG_TURNS_MAX = 5
EXIT_PROB_PER_TURN = 0.35              # шанс "вийти в закат" після мінімуму
IGNORE_AFTER_EXIT_SECONDS = 20 * 60    # 20 хв ігнор ПІСЛЯ виходу — тільки для одного юзера

# Random interjection
AUTO_INTERJECT_CHANCE = 0.10           # інколи щось скаже

# Conflict
BOT_COOLDOWN_SECONDS = 18              # антиспам

# Daily ping
SILENCE_HOURS_FOR_PING = 18
PING_WINDOW_START = 10                 # 10:00
PING_WINDOW_END = 22                   # 22:00
MORNING_PING_HOUR = 7                  # інколи 07:00
MORNING_PING_PROB = 0.15
PING_CHECK_EVERY_SECONDS = 60

# ==========================
# STATE
# ==========================
@dataclass
class ChatState:
    enabled: bool = True
    last_activity_ts: float = 0.0
    last_bot_ts: float = 0.0

    # ІГНОР ПО КОНКРЕТНИХ ЛЮДЯХ: user_id -> until_ts
    ignore_users_until: dict[int, float] = field(default_factory=dict)

    # діалог троля
    dialog_active_until_ts: float = 0.0
    dialog_turns_left: int = 0
    dialog_partner_user_id: int | None = None

    # облік пінгу
    last_ping_ts: float = 0.0

    # контекст
    memory: deque = field(default_factory=lambda: deque(maxlen=CONTEXT_N))

chat_states: dict[int, ChatState] = defaultdict(ChatState)

# ==========================
# LEXICON HEURISTICS
# ==========================
ATTACK_MARKERS = [
    "дебіл", "ідіот", "йоб", "єбан", "сука", "підар", "пидарас", "підорас",
    "лох", "клоун", "тупий", "довбойоб", "долбоёб", "мудак", "гівно", "сміття",
    "заткнись", "завались", "закрий пельку", "відвали", "йди нах", "пішов нах",
    "здохни", "уб'ю", "вбийся"
]

DEFENSE_MARKERS = [
    "я не", "ти не так", "шо ти", "чого ти", "та не", "серйозно?", "я взагалі",
    "поясню", "не треба", "давай без", "спокійно", "ти про шо", "я просто"
]

CALL_WORDS = ["ігнат", "арбітр", "суддя", "модер", "модератор", "бот"]

EXIT_JABS = [
    "Ладно, я погнав — у мене справи, не те що в деяких тут 😏",
    "Все, я зникаю. Робота сама себе не зробить — на відміну від ваших балачок.",
    "Ок, досить. Мені ще жити це життя, а не сидіти тут 24/7.",
    "Я пішов. Як звільнюся від справ — може ще підкину вам розуму.",
]
EXIT_NEUTRAL = [
    "Все, я зникаю. Не рознесіть чат без мене.",
    "Ок, мені час. Тримайтеся тут.",
    "Погнав далі. Без цирку, ок?",
]

PING_TEXTS = [
    "Куди всі пропали, друзяки? 😄",
    "Ей, чат, ви живі там?",
    "Тиша така, що аж підозріло. Хто на зв’язку?",
    "Я щось скучив. Розкажіть, що нового?",
]
MORNING_TEXTS = [
    "Доброго ранку, друзяки ☕️",
    "Доброго ранку. Хто вже в строю?",
    "Ранок. Прокидаємось, легенди 😄",
]
TROLL_SEEDS = [
    "Ну шо, генії, як життя?",
    "Хто сьогодні головний по здоровому глузду?",
    "Я бачу, рівень драматургії стабільний.",
]

# ==========================
# HELPERS
# ==========================
def now_ts() -> float:
    return time.time()

def local_dt(ts: float | None = None) -> datetime:
    return datetime.fromtimestamp(ts or now_ts(), TZ)

def in_group(chat_type: str) -> bool:
    return chat_type in ("group", "supergroup")

def text_lc(msg: Message) -> str:
    return (msg.text or "").strip().lower()

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
    for name, uid, txt in mem[-CONTEXT_N:]:
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
        if len(p) > 220:
            p = p[:220].rstrip() + "…"
        trimmed.append(p)

    r = random.random()
    limit = 1 if r < 0.65 else 2
    return trimmed[:limit] if trimmed else ["Ок."]

async def is_admin(chat_id: int, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, user_id)
        return m.status in ("administrator", "creator")
    except TelegramBadRequest:
        return False

async def llm(system: str, user: str, max_tokens: int = 120) -> str:
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

def is_user_ignored(state: ChatState, user_id: int, now: float) -> bool:
    until = state.ignore_users_until.get(user_id, 0.0)
    if now < until:
        return True
    # подчищаем протухшее
    if until and now >= until:
        state.ignore_users_until.pop(user_id, None)
    return False

# ==========================
# PROMPTS
# ==========================
MOD_SYSTEM = """
Ти — дорослий модератор українського групового чату. Твоя роль: швидко ставити межі і гасити конфлікти.

Вимоги:
- ЛИШЕ українською.
- Коротко (1–2 репліки).
- Жодних жартів, флірту, підйобів у конфлікті.
- Якщо тон культурний, але хтось явно захищається — спитай коротко “що сталося?” і запропонуй перейти на факти.
- Якщо є наїзд/образи — зупини, вистав правило (“без особистого”), запропонуй переформулювати.

Заборони:
- Не принижуй людину.
- Не закликай до насильства/шкоди.
- Не згадуй, що ти бот/ШІ.
"""

TROLL_SYSTEM = """
Ти — умний токсичний троль-арбітр українського чату.
Ти підтримуєш діалог живо, можеш використовувати легкий і жорсткий мат, сленг — але не переходиш у травлю.

Вимоги:
- ЛИШЕ українською.
- 1–2 короткі репліки.
- Дотепно, швидко, по суті.
- Не розпалюй конфлікт: якщо бачиш, що це сварка — перемикайся в режим модератора (стримано).

Заборони:
- Мова ненависті/приниження за груповими ознаками — ні.
- Погрози/насильство — ні.
- Не кажи, що ти бот/ШІ.
"""

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
            await message.reply("Ок, я в строю. Без наїздів — і всі щасливі.")
        else:
            await message.reply("Тільки адміни можуть мене вмикати.")
        return True

    if low.startswith("/status"):
        st = "ON ✅" if state.enabled else "OFF ⛔"
        await message.reply(f"Статус: {st}")
        return True

    return False

# ==========================
# CORE HANDLER
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
    low = text_lc(message)

    u = message.from_user
    name = (u.full_name or u.username or "Хтось").strip()

    # activity + memory
    state.last_activity_ts = now
    state.memory.append((name, u.id, message.text.strip()))

    # commands first
    if await handle_commands(message, low, state):
        return

    if not state.enabled:
        return

    # ✅ игнор только этого пользователя
    if is_user_ignored(state, u.id, now):
        return

    # anti-spam cooldown
    if now - state.last_bot_ts < BOT_COOLDOWN_SECONDS:
        return

    me = await bot.me()
    bot_username = (me.username or "").strip()

    # 1) Conflict detection
    attack = looks_like_attack(low)
    defensive = looks_like_defense(low)
    is_reply = bool(message.reply_to_message and (message.reply_to_message.text or ""))

    must_moderate = attack or (defensive and (is_reply or random.random() < 0.55))

    if must_moderate:
        ctx = format_context(chat_id)
        prompt = (
            f"Контекст (останні повідомлення):\n{ctx}\n\n"
            f"Останнє повідомлення:\n{name}: {message.text}\n\n"
            "Дай коротке втручання модератора згідно правил."
        )
        reply = await llm(MOD_SYSTEM, prompt, max_tokens=110)
        if reply:
            for line in split_short(reply):
                await message.reply(line)
            state.last_bot_ts = now
        return

    # 2) Troll dialog mode
    called = called_bot(low, bot_username)
    in_dialog = now < state.dialog_active_until_ts and state.dialog_turns_left > 0

    # ограничиваем "партнёром" диалога
    partner_ok = (state.dialog_partner_user_id is None) or (u.id == state.dialog_partner_user_id) or called

    if called and not in_dialog:
        state.dialog_turns_left = random.randint(DIALOG_TURNS_MIN, DIALOG_TURNS_MAX)
        state.dialog_active_until_ts = now + 8 * 60
        state.dialog_partner_user_id = u.id

    if in_dialog and not partner_ok:
        return

    if called or in_dialog:
        ctx = format_context(chat_id)
        seed = random.choice(TROLL_SEEDS)
        prompt = (
            f"{seed}\n\n"
            f"Контекст:\n{ctx}\n\n"
            f"Останнє:\n{name}: {message.text}\n\n"
            "Відповідай як умний токсичний троль-арбітр: коротко, дотепно, українською."
        )
        reply = await llm(TROLL_SYSTEM, prompt, max_tokens=120)
        if reply:
            for line in split_short(reply):
                await message.reply(line)
            state.last_bot_ts = now

        # turns down
        if state.dialog_turns_left > 0:
            state.dialog_turns_left -= 1

        # Exit logic
        min_done = state.dialog_turns_left <= (DIALOG_TURNS_MAX - DIALOG_TURNS_MIN)
        should_exit = (state.dialog_turns_left <= 0) or (min_done and random.random() < EXIT_PROB_PER_TURN)

        if should_exit:
            exit_text = random.choice(EXIT_JABS if random.random() < 0.55 else EXIT_NEUTRAL)
            await asyncio.sleep(random.uniform(0.6, 1.8))
            await message.reply(exit_text)

            # ✅ игнорим только партнёра диалога (или текущего автора, если партнёр не задан)
            target_id = state.dialog_partner_user_id or u.id
            state.ignore_users_until[target_id] = now + IGNORE_AFTER_EXIT_SECONDS

            # reset dialog
            state.dialog_active_until_ts = 0
            state.dialog_turns_left = 0
            state.dialog_partner_user_id = None

            state.last_bot_ts = now_ts()

        return

    # 3) Sometimes interject lightly
    if random.random() < AUTO_INTERJECT_CHANCE:
        ctx = format_context(chat_id)
        prompt = (
            f"Контекст:\n{ctx}\n\n"
            f"Останнє:\n{name}: {message.text}\n\n"
            "Дай коротку, нейтрально-дотепну реакцію або питання українською (1 репліка)."
        )
        reply = await llm(TROLL_SYSTEM, prompt, max_tokens=60)
        if reply:
            line = split_short(reply)[0]
            await message.reply(line)
            state.last_bot_ts = now

# ==========================
# DAILY PING LOOP
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
    return (now - state.last_ping_ts) >= 24 * 60 * 60  # max 1 per 24h

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
                state.last_bot_ts = now
            except TelegramBadRequest:
                pass

# ==========================
# START
# ==========================
async def main():
    asyncio.create_task(ping_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
