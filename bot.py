import os
import random
import asyncio
import time

from aiogram import Bot, Dispatcher
from aiogram.types import Message
from aiogram.utils import executor
from openai import OpenAI

# ==========================
# ENV
# ==========================

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

bot = Bot(token=TOKEN)
dp = Dispatcher(bot)

client = OpenAI(api_key=OPENAI_API_KEY)

# ==========================
# CONFIG
# ==========================

REPLY_CHANCE = 0.30        # 30% шанс влезть самому
ACTIVE_WINDOW = 120       # 2 минуты поддерживает разговор
MUTE_SECONDS = 180        # пауза если его послали

active_until = 0
mute_until = 0

# ==========================
# SYSTEM PROMPT
# ==========================

SYSTEM_PROMPT = """
Ты — участник дружеского группового чата. Ведёшь себя как живой человек.

ЯЗЫК:
- Всегда отвечай на языке последнего сообщения.
- Если языки смешаны — используй доминирующий.

ХАРАКТЕР:
- Ты циничный, саркастичный, токсичный (5/5).
- Мат допустим.
- Поддеваешь умно.
- 1–3 коротких сообщения.
- Не лекции.
- Не говори, что ты ИИ.

ПОВЕДЕНИЕ:
- Иногда одна фраза.
- Иногда две.
- Иногда задавай колкий вопрос.
- Не будь предсказуемым.

ЕСЛИ ТЕБЯ ПОСЛАЛИ:
- Признай коротко: "Ок, торможу." / "Ладно." / "Понял."
- Замолчи.
"""

# ==========================
# HELPER
# ==========================

async def generate_reply(user_text: str):
    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Ответь на том же языке: {user_text}"}
        ],
        max_tokens=200,
        temperature=1.1,
    )
    return response.choices[0].message.content.strip()

def is_push(text: str):
    push_words = ["заткнись", "перегнул", "хватит", "тормози", "заебал", "иди нах", "пошел нах"]
    return any(word in text.lower() for word in push_words)

# ==========================
# MAIN HANDLER
# ==========================

@dp.message_handler()
async def handle_message(message: Message):
    global active_until, mute_until

    now = time.time()

    if not message.text:
        return

    text = message.text
    lower_text = text.lower()

    # если бот в муте
    if now < mute_until:
        return

    # если его послали
    if is_push(lower_text):
        await asyncio.sleep(random.randint(1, 2))
        await message.reply(random.choice([
            "Ок, торможу.",
            "Ладно.",
            "Понял."
        ]))
        mute_until = now + MUTE_SECONDS
        active_until = 0
        return

    # если явно позвали
    if "бот" in lower_text or "@ignathui_bot" in lower_text:
        await asyncio.sleep(random.randint(2, 5))
        reply = await generate_reply(text)
        await message.reply(reply)
        active_until = now + ACTIVE_WINDOW
        return

    # если он уже активен — поддерживает разговор
    if now < active_until:
        await asyncio.sleep(random.randint(2, 5))
        reply = await generate_reply(text)
        await message.reply(reply)
        return

    # случайное влезание
    if random.random() < REPLY_CHANCE:
        await asyncio.sleep(random.randint(2, 5))
        reply = await generate_reply(text)
        await message.reply(reply)
        active_until = now + ACTIVE_WINDOW

# ==========================
# START
# ==========================

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)
