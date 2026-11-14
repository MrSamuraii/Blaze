"""Blaze Music Bot: что сейчас играет в Blaze, прямо в чате и через inline.
Песни бот НЕ отправляет: только текст, обложка-превью, лайк и текст песни.

Как это работает: у каждого человека свой ключ API Blaze (Настройки → Ключи API в приложении).
Он присылает ключ боту командой /connect, бот запоминает его и ходит на сервер Blaze от его имени.
"""
import asyncio
import html
import logging
import os
import sqlite3
import typing

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultsButton,
    InputTextMessageContent,
    Message,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("blaze-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
BLAZE_URL = os.environ["BLAZE_URL"].strip().rstrip("/")  # например http://212.119.242.23:8000
DB_PATH = os.environ.get("DB_PATH", "blaze_bot.sqlite3")
# Премиум-эмодзи в ТЕКСТЕ сообщений Telegram разрешает только ботам с юзернеймами с Fragment.
# Если у бота такие есть, поставь 1: в тексте появятся твои эмодзи. Иначе остаются обычные.
TEXT_CUSTOM_EMOJI = os.environ.get("TEXT_CUSTOM_EMOJI", "0") == "1"
# Иконки на кнопках (в личке с ботом) работают, если у владельца бота есть Telegram Premium.
BUTTON_CUSTOM_EMOJI = os.environ.get("BUTTON_CUSTOM_EMOJI", "1") == "1"

# Твои премиум-эмодзи: имя → (id, обычный эмодзи на замену)
EMOJI: dict[str, tuple[str, str]] = {
    "music": ("5458765040779832505", "🎵"),
    "pc": ("5445316948420243135", "💻"),
    "phone": ("5445374041420507758", "📱"),
    "volume": ("5445403088284334322", "🔉"),
    "like": ("5445070224023924152", "❤️"),
    "dislike": ("5445328665091027814", "😢"),
    "repeat": ("5444946425886584310", "🔁"),
    "shuffle": ("5445305158735015510", "🔀"),
    "from": ("5444894980768313209", "⭐️"),
    "fire": ("5444963141899302550", "🔥"),
    "refresh": ("5445201177576779364", "⏩"),
    "lyrics": ("5445004661348151035", "🎵"),
    "off": ("5445032441196623691", "🔇"),
    "error": ("5445262436695321760", "😎"),
    # эмодзи для подписи «сейчас играет»
    "t_title": ("5445004661348151035", "🎧"),
    "t_device": ("6039404727542747508", "⌨️"),
    "t_volume": ("6039454987250044861", "🔊"),
    "t_from": ("6039630677182254664", "🗂"),
    "t_blaze": ("5458765040779832505", "🎵"),
}


def e(name: str) -> str:
    """Эмодзи для текста: премиум (если разрешено) или обычный."""
    cid, plain = EMOJI[name]
    return f'<tg-emoji emoji-id="{cid}">{plain}</tg-emoji>' if TEXT_CUSTOM_EMOJI else plain


# ---------- Хранилище ключей ----------

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("CREATE TABLE IF NOT EXISTS users (tg_id INTEGER PRIMARY KEY, token TEXT NOT NULL)")
db.commit()


def get_token(tg_id: int) -> typing.Optional[str]:
    row = db.execute("SELECT token FROM users WHERE tg_id = ?", (tg_id,)).fetchone()
    return row[0] if row else None


def set_token(tg_id: int, token: str) -> None:
    db.execute("INSERT OR REPLACE INTO users (tg_id, token) VALUES (?, ?)", (tg_id, token))
    db.commit()


def drop_token(tg_id: int) -> None:
    db.execute("DELETE FROM users WHERE tg_id = ?", (tg_id,))
    db.commit()


# ---------- Клиент Blaze ----------


class BlazeError(Exception):
    pass


async def blaze(token: str, method: str, path: str, **kwargs):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.request(
                method,
                BLAZE_URL + path,
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=15),
                **kwargs,
            ) as r:
                if r.status == 401:
                    raise BlazeError("bad_token")
                if r.status == 403:
                    raise BlazeError("read_only")
                if r.status == 404:
                    raise BlazeError("not_found")
                if r.status >= 400:
                    raise BlazeError(f"HTTP {r.status}")
                return None if r.status == 204 else await r.json()
    except BlazeError:
        raise
    except Exception as ex:
        raise BlazeError(f"{type(ex).__name__}")


ERRORS = {
    "bad_token": "Сервер не принял ключ. Пришли новый: /connect ключ",
    "read_only": "Этот ключ только для чтения. Создай ключ с полным доступом и пришли его через /connect",
    "not_found": "Не нашлось",
}


def err_text(ex: BlazeError) -> str:
    return ERRORS.get(str(ex), f"Сервер Blaze не ответил ({ex})")


# ---------- Оформление ----------


def fmt_time(ms: int) -> str:
    s = max(0, ms // 1000)
    return f"{s // 60:02d}:{s % 60:02d}"


def bar(progress: int, duration: int, width: int = 10) -> str:
    filled = min(width, int(progress / duration * width)) if duration else 0
    return "▰" * filled + "▱" * (width - filled)


def now_text(now: dict) -> str:
    t = now["track"]
    artists = ", ".join(now.get("artists") or [t["artist"]])
    device = now.get("device") or "неизвестно"
    src = now.get("playing_from") or {}
    kind, title = src.get("type"), html.escape(src.get("title") or "")
    from_ = {
        "wave": "Моя волна",
        "album": f"Альбом «{title}»",
        "playlist": f"Плейлист «{title}»",
        "artist": f"Исполнитель {title}",
        "liked": "Любимые треки",
        "search": "Поиск",
        "library": "Библиотека",
    }.get(kind, "Разное")
    vol = now.get("volume")
    volume = f" ({e('t_volume')} {vol}%)" if vol is not None else ""
    return (
        f"{e('t_title')} <b>{html.escape(artists)} — {html.escape(t['title'])}</b>\n\n"
        f"{e('t_device')} <b>Что за устройство? <code>{html.escape(device)}</code>{volume}</b>\n"
        f"{e('t_from')} <b>Играет из:</b> {from_}\n\n"
        f"{e('t_blaze')} <b>Blaze</b>"
    )


NOTHING = "Сейчас ничего не играет"


def btn(text: str, data: str, emoji: str) -> InlineKeyboardButton:
    if BUTTON_CUSTOM_EMOJI:
        return InlineKeyboardButton(text=text, callback_data=data, icon_custom_emoji_id=EMOJI[emoji][0])
    return InlineKeyboardButton(text=f"{EMOJI[emoji][1]} {text}", callback_data=data)


def chat_keyboard(owner: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("Лайк", f"like:{owner}", "like"), btn("Дизлайк", f"dislike:{owner}", "dislike")],
            [btn("Обновить", f"refresh:{owner}", "refresh"), btn("Текст", f"lyrics:{owner}", "lyrics")],
        ]
    )


def inline_keyboard(owner: int) -> InlineKeyboardMarkup:
    # В inline-сообщениях (через бота) иконки-эмодзи на кнопках Telegram не показывает, поэтому обычные
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=f"{EMOJI['refresh'][1]} Обновить", callback_data=f"refresh:{owner}")]]
    )


# ---------- Бот ----------

router = Router()

START = (
    "Привет! Я показываю, что ты слушаешь в Blaze, и умею делать это в любом чате.\n\n"
    "1. В приложении Blaze открой <b>Настройки → Ключи API</b> и создай ключ "
    "(для лайков нужен полный доступ).\n"
    "2. Пришли его мне: <code>/connect blz_твой_ключ</code>. Сообщение я сразу удалю.\n\n"
    "Дальше в любом чате набери <code>@{username}</code> и выбери «Сейчас играет». "
    "Ещё можно написать <code>@{username} текст</code>, это текст играющей песни, "
    "или <code>@{username} название трека</code>, чтобы найти трек.\n\n"
    "Команды: /now, /like, /dislike, /lyrics, /disconnect"
)


@router.message(CommandStart())
async def start(m: Message, bot: Bot):
    me = await bot.get_me()
    await m.answer(START.format(username=me.username))


@router.message(Command("connect"))
async def connect(m: Message, command: CommandObject):
    key = (command.args or "").strip()
    try:
        await m.delete()  # ключ не должен оставаться в чате
    except TelegramBadRequest:
        pass
    if not key:
        return await m.answer("Пришли ключ так: <code>/connect blz_твой_ключ</code>")
    try:
        await blaze(key, "GET", "/me/now-playing")
    except BlazeError as ex:
        return await m.answer(err_text(ex))
    set_token(m.from_user.id, key)
    await m.answer("Готово, Blaze подключён. Попробуй /now или inline в любом чате. Сообщение с ключом я удалил.")


@router.message(Command("disconnect"))
async def disconnect(m: Message):
    drop_token(m.from_user.id)
    await m.answer("Отключил. Ключ удалён из моей базы.")


async def need_token(m: Message) -> typing.Optional[str]:
    token = get_token(m.from_user.id)
    if not token:
        await m.answer("Сначала подключи Blaze: /start")
    return token


@router.message(Command("now"))
async def now_cmd(m: Message):
    token = await need_token(m)
    if not token:
        return
    try:
        now = await blaze(token, "GET", "/me/now-playing")
    except BlazeError as ex:
        return await m.answer(err_text(ex))
    if not now.get("active"):
        return await m.answer(NOTHING)
    await send_safe(m, now_text(now), chat_keyboard(m.from_user.id))


async def send_safe(m: Message, text: str, kb: InlineKeyboardMarkup):
    """Если Telegram не принял иконки-эмодзи на кнопках (нет Premium у владельца), шлём без них."""
    global BUTTON_CUSTOM_EMOJI
    try:
        await m.answer(text, reply_markup=kb)
    except TelegramBadRequest as ex:
        if BUTTON_CUSTOM_EMOJI and "emoji" in str(ex).lower():
            BUTTON_CUSTOM_EMOJI = False
            await m.answer(text, reply_markup=chat_keyboard(m.from_user.id))
        else:
            raise


async def rate(m: Message, action: str, done: str):
    token = await need_token(m)
    if not token:
        return
    try:
        now = await blaze(token, "POST", f"/me/now-playing/{action}")
    except BlazeError as ex:
        return await m.answer(err_text(ex).replace("Не нашлось", NOTHING))
    t = now["track"]
    await m.answer(f"{done} <code>{html.escape(', '.join(now['artists']))} — {html.escape(t['title'])}</code>")


@router.message(Command("like"))
async def like_cmd(m: Message):
    await rate(m, "like", f"{e('like')} Лайк:")


@router.message(Command("dislike"))
async def dislike_cmd(m: Message):
    await rate(m, "dislike", f"{e('dislike')} Дизлайк, в Моей волне его больше не будет:")


async def lyrics_text(token: str) -> str:
    now = await blaze(token, "GET", "/me/now-playing")
    if not now.get("active"):
        return NOTHING
    data = await blaze(token, "GET", "/me/now-playing/lyrics")
    text = (data.get("text") or "").strip()
    head = f"{e('music')} <b>{html.escape(', '.join(now['artists']))} — {html.escape(now['track']['title'])}</b>\n\n"
    if not text:
        return head + "Для этого трека нет текста"
    if len(text) > 3500:
        text = text[:3500].rsplit("\n", 1)[0] + "\n…"
    return head + html.escape(text)


@router.message(Command("lyrics"))
async def lyrics_cmd(m: Message):
    token = await need_token(m)
    if not token:
        return
    try:
        await m.answer(await lyrics_text(token))
    except BlazeError as ex:
        await m.answer(err_text(ex))


# ---------- Кнопки под сообщениями ----------


@router.callback_query(F.data.regexp(r"^(refresh|like|dislike|lyrics):(\d+)$"))
async def on_button(c: CallbackQuery):
    action, owner = c.data.split(":")
    if c.from_user.id != int(owner):
        return await c.answer("Это чужой плеер: подключи свой Blaze у бота", show_alert=True)
    token = get_token(c.from_user.id)
    if not token:
        return await c.answer("Сначала подключи Blaze: /start", show_alert=True)
    try:
        if action in ("like", "dislike"):
            now = await blaze(token, "POST", f"/me/now-playing/{action}")
            await c.answer("Лайк" if action == "like" else "Дизлайк")
        elif action == "lyrics":
            text = await lyrics_text(token)
            if c.message:
                await c.message.answer(text)
            await c.answer()
            return
        else:
            now = await blaze(token, "GET", "/me/now-playing")
            await c.answer()
        if not now.get("active"):
            text, kb = NOTHING, None
        else:
            text = now_text(now)
            kb = inline_keyboard(c.from_user.id) if c.inline_message_id else chat_keyboard(c.from_user.id)
        try:
            if c.inline_message_id:
                await c.bot.edit_message_text(text, inline_message_id=c.inline_message_id, reply_markup=kb)
            elif c.message:
                await c.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest as ex:
            if "not modified" not in str(ex):
                raise
    except BlazeError as ex:
        await c.answer(err_text(ex), show_alert=True)


# ---------- Inline ----------


def article(rid: str, title: str, desc: str, text: str, thumb: typing.Optional[str] = None,
            kb: typing.Optional[InlineKeyboardMarkup] = None) -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id=rid,
        title=title,
        description=desc,
        thumbnail_url=thumb,
        input_message_content=InputTextMessageContent(message_text=text, parse_mode=ParseMode.HTML),
        reply_markup=kb,
    )


@router.inline_query()
async def inline(q: InlineQuery):
    token = get_token(q.from_user.id)
    if not token:
        return await q.answer(
            [],
            is_personal=True,
            cache_time=0,
            button=InlineQueryResultsButton(text="Подключить Blaze", start_parameter="connect"),
        )
    query = (q.query or "").strip()
    results: list[InlineQueryResultArticle] = []
    try:
        if query.lower() in ("", "now", "сейчас"):
            now = await blaze(token, "GET", "/me/now-playing")
            if not now.get("active"):
                results.append(article("none", NOTHING, "Включи музыку в Blaze", NOTHING))
            else:
                t = now["track"]
                thumb = BLAZE_URL + now["cover_url"] if now.get("cover_url") else None
                results.append(
                    article(
                        "now",
                        f"Сейчас играет: {', '.join(now['artists'])} — {t['title']}",
                        f"{fmt_time(now['progress_ms'])} / {fmt_time(now['duration_ms'])} · "
                        f"{now.get('device') or 'Blaze'}",
                        now_text(now),
                        thumb,
                        inline_keyboard(q.from_user.id),
                    )
                )
        elif query.lower() in ("lyrics", "текст", "text"):
            text = await lyrics_text(token)
            results.append(article("lyrics", "Текст играющей песни", "Отправить в чат", text))
        else:
            found = await blaze(token, "GET", "/tracks/search/query", params={"q": query})
            for t in (found or [])[:10]:
                results.append(
                    article(
                        f"t{t['id']}",
                        t["title"],
                        t["artist"],
                        f"{e('music')} <b>{html.escape(t['artist'])} — {html.escape(t['title'])}</b>\n\n"
                        f"{e('fire')} <b>Blaze</b>",
                        BLAZE_URL + f"/tracks/{t['id']}/cover" if t.get("cover_path") else None,
                    )
                )
            if not results:
                results.append(article("nf", "Ничего не найдено", query, f"Ничего не нашлось по запросу «{html.escape(query)}»"))
    except BlazeError as ex:
        results = [article("err", "Ошибка", err_text(ex), err_text(ex))]
    await q.answer(results, is_personal=True, cache_time=0)


async def main():
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.set_my_commands(
        [
            BotCommand(command="now", description="Что играет сейчас"),
            BotCommand(command="like", description="Лайкнуть играющий трек"),
            BotCommand(command="dislike", description="Дизлайк играющего трека"),
            BotCommand(command="lyrics", description="Текст играющей песни"),
            BotCommand(command="disconnect", description="Отключить Blaze"),
        ]
    )
    await dp.start_polling(bot, allowed_updates=["message", "callback_query", "inline_query"])


if __name__ == "__main__":
    asyncio.run(main())
