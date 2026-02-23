import os
import random
import asyncio
import time
from collections import deque

from aiogram import Bot, Dispatcher
from aiogram.types import Message
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

# ==========================
# CONFIG
# ==========================

REPLY_CHANCE = 0.30
ACTIVE_WINDOW = 150
MUTE_SECONDS = 240
CONTEXT_N = 12
MAX_REPLY_LINES = 3
MAX_CHARS_PER_LINE = 180

active_until = 0
mute_until = 0

chat_memory = deque(maxlen=CONTEXT_N)

# ==========================
# PERSONA
# ==========================

SYSTEM_PROMPT = """
Ти максимально токсичний, жорсткий, саркастичний учасник українського чату.

МОВА:
- Завжди тільки українською.

СТИЛЬ:
- Брудний мат допустимий: хуй, залупа, пізда махрова, хуй моржовий і тд.
- Жаргон, грубість, нахабство.
- 1–3 короткі репліки.
- Ніяких довгих пояснень.
- Без "як ШІ..." та моралей.
- Токсик = колкий підкол, не заклики до реальної шкоди.

Чіпляйся за конкретику з останніх повідомлень.
"""

PUSH_WORDS = [
    "заткнись", "завались", "припини", "досить",
    "перегнув", "тормози", "відвали", "закрийся"
]

def is_push(text: str) -> bool:
    return any(w in text.lower() for w in PUSH_WORDS)

def is_calling_bot(text: str, username: str) -> bool:
    t = text.lower()
    return (
        "бот" in t or
        "ігнат" in t or
        (username and f"@{username.lower()}" in t)
    )

def format_context():
    lines = []
    for name, txt in chat_memory:
        if txt:
            lines.append(f"{name}: {txt}")
    return "\n".join(lines[-CONTEXT_N:])

async def generate_reply(context: str, last_text: str):
    resp = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Контекст:\n{context}\n\nОстаннє:\n{last_text}\n\nВідповідай коротко."}
        ],
        temperature=1.2,
        max_tokens=90,
        presence_penalty=0.8,
        frequency_penalty=0.6,
    )
    return resp.choices[0].message.content.strip()

def split_lines(text: str):
    raw = text.replace("\r", "\n").strip()
    if not raw:
        return ["Та шо ти мелеш, хуй моржовий?"]

    parts = [p.strip() for p in raw.split("\n") if p.strip()]

    if len(parts) == 1:
        tmp = raw.replace("! ", "!\n").replace("? ", "?\n").replace(". ", ".\n")
        parts = [p.strip() for p in tmp.split("\n") if p.strip()]

    trimmed = []
    for p in parts:
        if len(p) > MAX_CHARS_PER_LINE:
            p = p[:MAX_CHARS_PER_LINE] + "…"
        trimmed.append(p)

    r = random.random()
    limit = 1 if r < 0.55 else (2 if r < 0.9 else 3)

    return trimmed[:min(limit, MAX_REPLY_LINES)]

# ==========================
# HANDLER
# ==========================

@dp.message()
async def handle_message(message: Message):
    global active_until, mute_until

    if message.chat.type not in ["group", "supergroup"]:
        return
    if not message.text:
        return

    now = time.time()
    text = message.text.strip()
    low = text.lower()

    user = message.from_user
    name = user.full_name or user.username or "Хтось"
    chat_memory.append((name, text))

    # 🔥 СПЕЦТРИГЕР НА ПУТІНА
    if "путін" in low:
        await message.reply("Путін — підарас.")
        return

    if now < mute_until:
        return

    if is_push(low):
        await message.reply(random.choice([
            "Та ок, мовчу.",
            "Здувся, задоволені?",
            "Все, закрився."
        ]))
        mute_until = now + MUTE_SECONDS
        active_until = 0
        return

    me = await bot.me()
    username = me.username or ""

    called = is_calling_bot(low, username)

    if called:
        await asyncio.sleep(random.randint(2, 5))
        ctx = format_context()
        reply = await generate_reply(ctx, text)
        for line in split_lines(reply):
            await message.reply(line)
            await asyncio.sleep(random.randint(1, 2))
        active_until = now + ACTIVE_WINDOW
        return

    if now < active_until:
        await asyncio.sleep(random.randint(2, 5))
        ctx = format_context()
        reply = await generate_reply(ctx, text)
        for line in split_lines(reply):
            await message.reply(line)
            await asyncio.sleep(random.randint(1, 2))
        return

    if random.random() < REPLY_CHANCE:
        await asyncio.sleep(random.randint(2, 5))
        ctx = format_context()
        reply = await generate_reply(ctx, text)
        for line in split_lines(reply):
            await message.reply(line)
            await asyncio.sleep(random.randint(1, 2))
        active_until = now + ACTIVE_WINDOW

# ==========================
# START
# ==========================

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
