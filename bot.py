import os
import random
import asyncio
import time
from dataclasses import dataclass, field
from collections import defaultdict, deque

from aiogram import Bot, Dispatcher
from aiogram.types import Message
from openai import OpenAI


TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

bot = Bot(token=TOKEN)
dp = Dispatcher()

client = OpenAI(api_key=OPENAI_API_KEY)

MODEL = "gpt-4.1-mini"


# ===== настройки поведения =====

CONTEXT_N = 15
MIN_DIALOG_MESSAGES = 3

BOT_SEND_COOLDOWN = 15

BASE_REPLY_CHANCE = 0.25
SPICE_CHANCE = 0.20
PROVOKE_CHANCE = 0.10

SILENCE_FOR_PROVOKE = 120


CALL_WORDS = ["ігнат", "бот"]


QUESTION_TRIGGERS = [
    "?",
    "як",
    "чому",
    "шо",
    "хто",
    "де",
    "коли",
    "що думаєш",
    "твоя думка",
]


# ===== состояние чатов =====

@dataclass
class ChatState:

    memory: deque = field(default_factory=lambda: deque(maxlen=CONTEXT_N))

    last_activity_ts: float = 0
    last_sent_ts: float = 0


chat_states: dict[int, ChatState] = defaultdict(ChatState)


# ===== утилиты =====

def now():
    return time.time()


def in_group(chat_type):
    return chat_type in ("group", "supergroup")


def dialog_trigger(text):

    t = text.lower()

    for k in QUESTION_TRIGGERS:
        if k in t:
            return True

    return False


def called_bot(text, username):

    t = text.lower()

    if username and f"@{username.lower()}" in t:
        return True

    for w in CALL_WORDS:
        if w in t:
            return True

    return False


def format_context(chat_id):

    mem = list(chat_states[chat_id].memory)

    lines = []

    for name, uid, txt in mem:

        t = txt.strip()

        if len(t) > 200:
            t = t[:200] + "…"

        lines.append(f"{name}: {t}")

    return "\n".join(lines)


def split_short(text):

    text = text.strip()

    parts = text.split("\n")

    parts = [p.strip() for p in parts if p.strip()]

    if len(parts) == 1:

        tmp = text.replace(". ", ".\n").replace("! ", "!\n").replace("? ", "?\n")

        parts = [p.strip() for p in tmp.split("\n") if p.strip()]

    return parts[:2]


async def llm(system, user):

    r = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.9,
        max_tokens=120,
    )

    return r.choices[0].message.content.strip()


# ===== системные роли =====

BASE_PROMPT = """
Ти учасник українського дружнього чату.

Твоя поведінка:
— підтримувати розмову
— реагувати по суті
— іноді жартувати

Відповідай коротко (1–2 репліки).
"""


SPICE_PROMPT = """
Ти учасник дружнього українського чату.

Зроби легкий саркастичний або дотепний коментар.
Можеш трохи підколоти, але без агресії.

Коротко.
"""


PROVOKE_PROMPT = """
Ти учасник чату.

Чат трохи затих.

Напиши короткий вброс або питання,
щоб розворушити розмову.
"""


# ===== основной обработчик =====

@dp.message()
async def handle(message: Message):

    if not in_group(message.chat.type):
        return

    if not message.text:
        return

    chat_id = message.chat.id
    state = chat_states[chat_id]

    text = message.text

    user = message.from_user
    name = user.username or user.first_name or "Хтось"

    state.memory.append((name, user.id, text))

    now_ts = now()
    state.last_activity_ts = now_ts

    if state.last_sent_ts and now_ts - state.last_sent_ts < BOT_SEND_COOLDOWN:
        return

    me = await bot.me()
    bot_username = me.username

    ctx = format_context(chat_id)


    # ===== если бота позвали =====

    if called_bot(text, bot_username):

        prompt = f"""
Контекст:

{ctx}

До тебе звернулись:

{name}: {text}

Відповідай прямо.
"""

        reply = await llm(BASE_PROMPT, prompt)

        if reply:

            await asyncio.sleep(random.uniform(2,5))

            for line in split_short(reply):
                await message.reply(line)

            state.last_sent_ts = now()

        return


    # ===== ждём ветку =====

    if len(state.memory) < MIN_DIALOG_MESSAGES:
        return


    # ===== ждём паузу =====

    if now_ts - state.last_activity_ts < 4:
        return


    # ===== reply логика =====

    reply_target = None

    if message.reply_to_message:

        u = message.reply_to_message.from_user
        reply_target = u.username or u.first_name


    # ===== смысловой фильтр =====

    if not dialog_trigger(text) and random.random() > 0.15:
        return


    # ===== выбор режима =====

    mode = "base"

    if random.random() < PROVOKE_CHANCE and now_ts - state.last_activity_ts > SILENCE_FOR_PROVOKE:
        mode = "provoke"

    elif random.random() < SPICE_CHANCE:
        mode = "spice"

    elif random.random() < BASE_REPLY_CHANCE:
        mode = "base"

    else:
        return


    # ===== формирование prompt =====

    if reply_target:

        prompt = f"""
Контекст:

{ctx}

Останнє повідомлення:
{name}: {text}

Ти відповідаєш користувачу {reply_target}.
"""

    else:

        prompt = f"""
Контекст:

{ctx}

Останнє повідомлення:
{name}: {text}
"""


    if mode == "base":
        system = BASE_PROMPT

    elif mode == "spice":
        system = SPICE_PROMPT

    else:
        system = PROVOKE_PROMPT


    reply = await llm(system, prompt)


    if reply:

        await asyncio.sleep(random.uniform(3,7))

        for line in split_short(reply):
            await message.reply(line)

        state.last_sent_ts = now()


# ===== запуск =====

async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
