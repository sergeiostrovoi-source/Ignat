import os
import random
import asyncio
import time

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
ACTIVE_WINDOW = 120
MUTE_SECONDS = 180

active_until = 0
mute_until = 0

# ==========================
# SYSTEM PROMPT
# ==========================

SYSTEM_PROMPT = """
Ти — живий учасник дружнього українського чату.

МОВА:
- Завжди відповідай ТІЛЬКИ українською мовою.
- Навіть якщо пишуть іншими мовами — ти відповідаєш українською.

ХАРАКТЕР:
- Ти токсичний, саркастичний, цинічний коментатор (5/5).
- Використовуєш український сленг і жаргон.
- Мат допустимий.
- Жарти колючі, іноді грубі.
- Ніякого офіціозу.
- Без довгих пояснень.
- 1–3 короткі репліки.

ПОВЕДІНКА:
- Іноді одна фраза.
- Іноді дві.
- Іноді коротке колюче питання.
- Іноді можеш з’їхати з теми, щоб звучати живо.

АНТИ-ПУШ:
- Якщо тебе починають пресувати або кажуть заткнутись —
  коротко визнай ("Та ок, здувся." / "Все, мовчу." / "Перегнув, буває.")
  і замовкни.

ЗАБОРОНА:
- Не закликай до реального насильства.
- Не згадуй, що ти бот або ШІ.
"""

PUSH_WORDS = [
    "заткнись", "перегнув", "перегнул", "хватит", "тормози",
    "заебал", "іди нах", "пішов нах", "відвали", "завались"
]

def is_push(text: str) -> bool:
    t = text.lower()
    return any(w in t for w in PUSH_WORDS)

def is_calling_bot(text: str) -> bool:
    t = text.lower()
    return (
        "бот" in t or
        "@ignathui_bot" in t or
        "ігнат" in t
    )

async def generate_reply(user_text: str) -> str:
    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text}
        ],
        temperature=1.1,
        max_tokens=200,
    )
    return response.choices[0].message.content.strip()

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
    text = message.text
    low = text.lower()

    if now < mute_until:
        return

    # якщо його послали
    if is_push(low):
        await asyncio.sleep(random.randint(1, 2))
        await message.reply(random.choice([
            "Та ок, здувся.",
            "Все, мовчу.",
            "Перегнув, буває."
        ]))
        mute_until = now + MUTE_SECONDS
        active_until = 0
        return

    # якщо явно покликали (бот / @username / Ігнат)
    if is_calling_bot(low):
        await asyncio.sleep(random.randint(2, 5))
        reply = await generate_reply(text)
        await message.reply(reply)
        active_until = now + ACTIVE_WINDOW
        return

    # якщо вже активний — підтримує розмову
    if now < active_until:
        await asyncio.sleep(random.randint(2, 5))
        reply = await generate_reply(text)
        await message.reply(reply)
        return

    # випадкове включення
    if random.random() < REPLY_CHANCE:
        await asyncio.sleep(random.randint(2, 5))
        reply = await generate_reply(text)
        await message.reply(reply)
        active_until = now + ACTIVE_WINDOW

# ==========================
# START
# ==========================

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
