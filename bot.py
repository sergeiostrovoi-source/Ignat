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
ACTIVE_WINDOW = 150           # чуть дольше поддерживает беседу
MUTE_SECONDS = 240            # дольше молчит после "послали"
CONTEXT_N = 12                # сколько последних сообщений помнить
MAX_REPLY_LINES = 3           # 1–3 реплики

active_until = 0
mute_until = 0

# Память последних сообщений (только пока процесс жив; после рестарта обнулится)
# Формат: deque of tuples (name, text)
chat_memory = deque(maxlen=CONTEXT_N)

# ==========================
# PERSONA
# ==========================

SYSTEM_PROMPT = """
Ти — живий учасник дружнього українського чату. Твій стиль — максимально токсичний, саркастичний, нахабний і дотепний.
Можеш матюкатися та використовувати український сленг/жаргон.

АЛЕ:
- Без погроз реальної шкоди.
- Без закликів до насильства.
- Без мови ненависті, расизму, приниження за груповими ознаками.
- Без “цькування” однієї людини в нуль: токсик — це підкол, а не травля.

МОВА:
- Відповідаєш ТІЛЬКИ українською. Завжди.

ФОРМАТ:
- 1–3 короткі репліки (кожна до ~220 символів).
- Ніяких довгих пояснень, моралей, “як ШІ я…”.
- Інколи став коротке колюче питання.
- Пиши як свій у чаті: живо, з емоцією, інколи уривчасто.

КОНТЕКСТ:
- Ти бачиш останні повідомлення чату. Чіпляйся за конкретику з них (імена, теми, підколи).
- Не відповідай “в вакуумі”.

АНТИ-ПУШ:
- Якщо тебе послали/просять заткнутись — коротко визнай і замовкни.
"""

PUSH_WORDS = [
    "заткнись", "завались", "закрийся", "припини", "досить", "хватит",
    "перегнув", "перегнул", "тормози", "заєбав", "заебал",
    "іди нах", "йди нах", "пішов нах", "пошел нах", "відвали", "нахер"
]

def is_push(text: str) -> bool:
    t = text.lower()
    return any(w in t for w in PUSH_WORDS)

def is_calling_bot(text: str, bot_username: str) -> bool:
    t = text.lower()
    return (
        "бот" in t or
        "ігнат" in t or
        (bot_username and f"@{bot_username.lower()}" in t)
    )

def format_context() -> str:
    # Склеиваем последние сообщения в компактный контекст
    # Пример:
    # Даня: ...
    # Сергій: ...
    lines = []
    for name, txt in chat_memory:
        txt = (txt or "").strip()
        if not txt:
            continue
        if len(txt) > 280:
            txt = txt[:280] + "…"
        lines.append(f"{name}: {txt}")
    return "\n".join(lines[-CONTEXT_N:])

async def generate_reply(context: str, last_user_text: str) -> str:
    resp = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Останні повідомлення чату:\n{context}\n\nОстаннє повідомлення:\n{last_user_text}\n\nВідповідай у стилі персонажа."}
        ],
        temperature=1.15,
        max_tokens=220,
    )
    return resp.choices[0].message.content.strip()

def split_into_lines(text: str) -> list[str]:
    parts = [p.strip() for p in text.replace("\r", "\n").split("\n") if p.strip()]
    # Если модель выдала одним полотном — разрежем по предложениям грубо
    if len(parts) <= 1 and len(text) > 260:
        # Очень грубое разбиение, но помогает сделать 2–3 реплики
        tmp = text.replace("! ", "!\n").replace("? ", "?\n").replace(". ", ".\n")
        parts = [p.strip() for p in tmp.split("\n") if p.strip()]
    return parts[:MAX_REPLY_LINES] if parts else ["Та шо ти несеш, га?"]

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

    # записываем в память
    user = message.from_user
    name = (user.full_name or user.username or "Хтось").strip()
    chat_memory.append((name, text))

    if now < mute_until:
        return

    me = await bot.me()
    bot_username = (me.username or "").strip()

    # если его послали — сдаёт назад и молчит
    if is_push(low):
        await asyncio.sleep(random.randint(1, 2))
        await message.reply(random.choice([
            "Та ок, здувся. Мовчу.",
            "Окей-окей, зрозумів. Сбавляю.",
            "Поняв. Стихаю."
        ]))
        mute_until = now + MUTE_SECONDS
        active_until = 0
        return

    called = is_calling_bot(low, bot_username)

    # если явно позвали — отвечаем сразу
    if called:
        await bot.send_chat_action(message.chat.id, "typing")
        await asyncio.sleep(random.randint(2, 6))

        ctx = format_context()
        reply = await generate_reply(ctx, text)
        for line in split_into_lines(reply):
            await message.reply(line)
            await asyncio.sleep(random.randint(1, 3))

        active_until = now + ACTIVE_WINDOW
        return

    # если активен — поддерживает разговор
    if now < active_until:
        await bot.send_chat_action(message.chat.id, "typing")
        await asyncio.sleep(random.randint(2, 6))

        ctx = format_context()
        reply = await generate_reply(ctx, text)
        for line in split_into_lines(reply):
            await message.reply(line)
            await asyncio.sleep(random.randint(1, 3))
        return

    # самовключение (30%)
    if random.random() < REPLY_CHANCE:
        await bot.send_chat_action(message.chat.id, "typing")
        await asyncio.sleep(random.randint(2, 6))

        ctx = format_context()
        reply = await generate_reply(ctx, text)
        for line in split_into_lines(reply):
            await message.reply(line)
            await asyncio.sleep(random.randint(1, 3))

        active_until = now + ACTIVE_WINDOW

# ==========================
# START
# ==========================

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
