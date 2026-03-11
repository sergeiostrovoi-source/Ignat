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
CONTEXT_N = 40

# базовая активность
BASE_REPLY_CHANCE = 0.03          # редко сам встревает
TEASE_CHANCE = 0.03               # ещё реже слегка подкалывает
BOT_SEND_COOLDOWN = 18            # не чаще 1 ответа раз в 18 сек
ACTIVE_WINDOW_SECONDS = 10 * 60   # если его втянули, держится в теме 10 минут

# тишина
NUDGE_SILENCE_MINUTES = 120
NUDGE_MIN_GAP_SECONDS = 8 * 60 * 60
NUDGE_WINDOW_START = 10
NUDGE_WINDOW_END = 22
NUDGE_CHECK_EVERY_SECONDS = 180

# мало сообщений за сутки
LOW_ACTIVITY_WINDOW_HOURS = 24
LOW_ACTIVITY_MAX_MESSAGES = 3
LOW_ACTIVITY_CHECK_EVERY_SECONDS = 300
LOW_ACTIVITY_WINDOW_START = 10
LOW_ACTIVITY_WINDOW_END = 22

# ==========================
# STATE
# ==========================
@dataclass
class ChatState:
    enabled: bool = True
    memory: deque = field(default_factory=lambda: deque(maxlen=CONTEXT_N))  # (name, user_id, text)
    activity_timestamps: deque = field(default_factory=deque)

    last_activity_ts: float = 0.0
    last_sent_ts: float = 0.0
    active_until_ts: float = 0.0

    last_nudge_ts: float = 0.0
    last_low_activity_ping_ts: float = 0.0

    soften_until_ts: float = 0.0  # после извинения/сбавления тона

chat_states: dict[int, ChatState] = defaultdict(ChatState)

# ==========================
# HEURISTICS
# ==========================
CALL_WORDS = ["ігнат", "бот", "арбітр", "суддя", "модер"]

ATTACK_MARKERS = [
    "заткнись", "завались", "відвали", "йди нах", "пішов нах",
    "нахуй", "хуй", "сука", "бля", "єбан", "йоб",
    "ідіот", "дебіл", "мудак", "лох", "клоун"
]

DEFENSE_MARKERS = [
    "я не", "ти не так", "чого ти", "шо ти", "серйозно",
    "не наїжджай", "давай без", "спокійно", "я просто",
    "ти не прав", "це не так", "перегнув", "занадто"
]

LOGIC_PUSH_MARKERS = [
    "ти не прав", "це не так", "перегнув", "занадто", "по фактах",
    "переформулюй", "без особистого", "ти сам собі суперечиш",
    "це вже зайве", "ти зараз не правий"
]

# ==========================
# PROMPTS
# ==========================
OBSERVER_SYSTEM = """
Ти — україномовний учасник дружнього чату.

Твоя базова поведінка:
- слухати контекст
- говорити доречно
- не шуміти без причини
- підтримувати нормальну бесіду

Правила:
- ЛИШЕ українською.
- 1–2 короткі репліки.
- Якщо нема сенсу щось писати — краще промовчи.
- Відповідай по суті і тримай нитку розмови.
- Можеш бути живим і дотепним, але без клоунади.
- Мат — рідко і тільки якщо доречно.

Заборони:
- Без мови ненависті.
- Без погроз.
- Не кажи, що ти бот або ШІ.
"""

PARTICIPANT_SYSTEM = """
Ти — україномовний учасник чату з характером.
Ти адекватний у спілкуванні, можеш іноді пожартувати або легко підколоти, але головне — нормально підтримувати розмову.

Правила:
- 1–2 короткі репліки.
- Відповідай на зміст сказаного, а не просто на останнє слово.
- Якщо людина пояснює позицію — слухай і реагуй на аргумент.
- Іноді можна легенько підколоти, але без тупої агресії.
- Якщо тебе аргументовано поправили — визнай це спокійно.

Заборони:
- Без мови ненависті.
- Без погроз.
- Не кажи, що ти бот або ШІ.
"""

ARBITER_SYSTEM = """
Ти — дорослий арбітр українського чату.
Твоя задача — зупиняти зайві наїзди і повертати всіх до нормальної розмови.

Правила:
- ЛИШЕ українською.
- 1–2 короткі репліки.
- У конфлікті: без цирку, без флірту.
- Ругай дію або тон, а не людину.
- Якщо хтось захищається культурно — уточни, що сталося, і поверни до фактів.
- Якщо тебе логічно притиснули — визнай перегин коротко і спокійно.

Заборони:
- Без мови ненависті.
- Без погроз.
- Не кажи, що ти бот або ШІ.
"""

APOLOGY_SYSTEM = """
Ти — україномовний учасник чату.
Тебе аргументовано поправили або показали, що ти перегнув.

Правила:
- Коротко визнай перегин або помилку.
- Без ниття.
- Без самоприниження.
- 1 коротка репліка.
- Після цього тон стає спокійніший.

Приклади стилю:
- "Ок, тут я перегнув."
- "Справедливо. Тут я зайшов не туди."
- "Прийнято. Це вже було зайве."
"""

NUDGE_LINES = [
    "Панове, ви там ще існуєте чи чат офіційно заснув?",
    "Ну й тиша. Наче всі зайшли і передумали щось писати.",
    "Альо, громадяни чату. Тут взагалі хтось залишився?",
    "Складається враження, що всі читають, але ніхто не хоче бути першим.",
    "О, тиша. Самий час комусь сказати щось розумне."
]

LOW_ACTIVITY_LINES = [
    "Альо, де всі? Дайте хоч знак, що живі.",
    "Щось чат підозріло тихий. Всі цілі?",
    "Ей, народ, відпишіться хоч хтось. Бо тиша вже нездорова.",
    "Ну й тиша. Хто живий — маякніть."
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

def looks_like_logic_push(low: str) -> bool:
    return any(w in low for w in LOGIC_PUSH_MARKERS)

def trim_activity(state: ChatState, now: float):
    cutoff = now - (LOW_ACTIVITY_WINDOW_HOURS * 3600)
    while state.activity_timestamps and state.activity_timestamps[0] < cutoff:
        state.activity_timestamps.popleft()

def format_context(chat_id: int) -> str:
    mem = list(chat_states[chat_id].memory)
    lines = []
    for name, uid, txt in mem[-CONTEXT_N:]:
        t = txt.strip()
        if len(t) > 260:
            t = t[:260] + "…"
        lines.append(f"{name}: {t}")
    return "\n".join(lines)

def pick_recent_user(chat_id: int) -> tuple[str, int] | None:
    mem = list(chat_states[chat_id].memory)
    seen = set()
    candidates = []
    for name, uid, txt in reversed(mem):
        if uid in seen:
            continue
        seen.add(uid)
        if name:
            candidates.append((name, uid))
        if len(candidates) >= 8:
            break
    if not candidates:
        return None
    return random.choice(candidates)

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

    return trimmed[:2] if trimmed else ["Ок."]

async def llm(system: str, user: str, max_tokens: int = 140) -> str:
    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.9,
            max_tokens=max_tokens,
            presence_penalty=0.35,
            frequency_penalty=0.25,
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
# MAIN MESSAGE HANDLER
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

    text = message.text.strip()
    low = lc_text(text)

    u = message.from_user
    name = (u.full_name or u.username or "Хтось").strip()

    state.last_activity_ts = now
    state.activity_timestamps.append(now)
    trim_activity(state, now)
    state.memory.append((name, u.id, text))

    if await handle_commands(message, low, state):
        return
    if not state.enabled:
        return

    if state.last_sent_ts and (now - state.last_sent_ts) < BOT_SEND_COOLDOWN:
        return

    me = await bot.me()
    bot_username = (me.username or "").strip()

    is_call = called_bot(low, bot_username)
    is_conflict = looks_like_attack(low)
    is_def = looks_like_defense(low)
    is_logic_push = looks_like_logic_push(low)
    in_active = now < state.active_until_ts
    softened = now < state.soften_until_ts

    ctx = format_context(chat_id)

    # 1. Логично прижали — извиняется
    if is_logic_push and (is_call or in_active):
        prompt = (
            f"Контекст:\n{ctx}\n\n"
            f"Останнє повідомлення:\n{name}: {text}\n\n"
            f"Коротко визнай, що перегнув або був не правий."
        )
        reply = await llm(APOLOGY_SYSTEM, prompt, max_tokens=60)
        if reply:
            await message.reply(split_short(reply)[0])
            state.last_sent_ts = now
            state.soften_until_ts = now + 15 * 60
        return

    # 2. Конфликт / защита — арбитр
    if is_conflict or (is_def and random.random() < 0.5):
        prompt = (
            f"Контекст:\n{ctx}\n\n"
            f"Останнє повідомлення:\n{name}: {text}\n\n"
            f"Втруться як арбітр."
        )
        reply = await llm(ARBITER_SYSTEM, prompt, max_tokens=110)
        if reply:
            for line in split_short(reply):
                await message.reply(line)
            state.last_sent_ts = now
            state.active_until_ts = now + ACTIVE_WINDOW_SECONDS
        return

    # 3. Если его позвали — нормальное участие
    if is_call:
        prompt = (
            f"Контекст:\n{ctx}\n\n"
            f"Останнє повідомлення:\n{name}: {text}\n\n"
            f"Відповідай як нормальний учасник чату з характером."
        )
        system = OBSERVER_SYSTEM if softened else PARTICIPANT_SYSTEM
        reply = await llm(system, prompt, max_tokens=120)
        if reply:
            for line in split_short(reply):
                await message.reply(line)
            state.last_sent_ts = now
            state.active_until_ts = now + ACTIVE_WINDOW_SECONDS
        return

    # 4. В активном окне иногда поддерживает беседу
    if in_active and random.random() < (0.12 if not softened else 0.06):
        prompt = (
            f"Контекст:\n{ctx}\n\n"
            f"Останнє повідомлення:\n{name}: {text}\n\n"
            f"Дай коротку реакцію по суті, яка реально підтримує розмову."
        )
        system = OBSERVER_SYSTEM if softened else PARTICIPANT_SYSTEM
        reply = await llm(system, prompt, max_tokens=100)
        if reply:
            await message.reply(split_short(reply)[0])
            state.last_sent_ts = now
        return

    # 5. Редкий лёгкий подкол
    if random.random() < (TEASE_CHANCE if not softened else 0.01):
        target = pick_recent_user(chat_id)
        if target:
            target_name, _ = target
            prompt = (
                f"Контекст:\n{ctx}\n\n"
                f"Завдання: коротко і м'яко підколоти одного з учасників без агресії.\n"
                f"Ім'я для згадки: {target_name}\n"
                f"Останнє повідомлення:\n{name}: {text}"
            )
            reply = await llm(PARTICIPANT_SYSTEM, prompt, max_tokens=80)
            if reply:
                await message.reply(split_short(reply)[0])
                state.last_sent_ts = now
        return

    # 6. Очень редкое самостоятельное участие
    if random.random() < BASE_REPLY_CHANCE:
        prompt = (
            f"Контекст:\n{ctx}\n\n"
            f"Останнє повідомлення:\n{name}: {text}\n\n"
            f"Дай коротку і доречну репліку, тільки якщо вона реально додає щось."
        )
        reply = await llm(OBSERVER_SYSTEM, prompt, max_tokens=80)
        if reply:
            await message.reply(split_short(reply)[0])
            state.last_sent_ts = now

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
            if silence < NUDGE_SILENCE_MINUTES * 60:
                continue

            if (now - state.last_nudge_ts) < NUDGE_MIN_GAP_SECONDS:
                continue

            if random.random() > NUDGE_PROB:
                continue

            try:
                await bot.send_message(chat_id, random.choice(NUDGE_LINES))
                state.last_nudge_ts = now
                state.last_sent_ts = now
            except TelegramBadRequest:
                pass

# ==========================
# LOW ACTIVITY LOOP
# ==========================
def in_low_activity_window(dt: datetime) -> bool:
    return LOW_ACTIVITY_WINDOW_START <= dt.hour < LOW_ACTIVITY_WINDOW_END

def low_activity_ping_limit_ok(state: ChatState, now: float) -> bool:
    if state.last_low_activity_ping_ts <= 0:
        return True
    return (now - state.last_low_activity_ping_ts) >= 24 * 60 * 60

async def low_activity_loop():
    while True:
        await asyncio.sleep(LOW_ACTIVITY_CHECK_EVERY_SECONDS)
        now = now_ts()
        dt = datetime.fromtimestamp(now, TZ)

        if not in_low_activity_window(dt):
            continue

        for chat_id, state in list(chat_states.items()):
            if not state.enabled:
                continue

            trim_activity(state, now)
            if len(state.activity_timestamps) > LOW_ACTIVITY_MAX_MESSAGES:
                continue

            if not low_activity_ping_limit_ok(state, now):
                continue

            try:
                await bot.send_message(chat_id, random.choice(LOW_ACTIVITY_LINES))
                state.last_low_activity_ping_ts = now
                state.last_sent_ts = now
            except TelegramBadRequest:
                pass

# ==========================
# START
# ==========================
async def main():
    asyncio.create_task(nudge_loop())
    asyncio.create_task(low_activity_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
