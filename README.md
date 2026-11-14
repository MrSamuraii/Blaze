# Blaze Music Bot

Показывает, что ты слушаешь в Blaze, в личке и через inline в любом чате. Песни не отправляет.

## Запуск
1. В @BotFather: создай бота, затем `/setinline` и включи inline-режим.
2. Положи папку на сервер, например в `/opt/blaze/bot`, и сделай `.env` по образцу `.env.example`.
3. `/opt/blaze/venv/bin/pip install -r requirements.txt`
4. Проверка: `set -a; . ./.env; set +a; python bot.py`
5. Постоянно: `cp blaze-bot.service /etc/systemd/system/ && systemctl enable --now blaze-bot`

## Как пользоваться
- `/connect blz_ключ`: привязать свой ключ API Blaze (сообщение бот удалит).
- `@бот` в любом чате: «Сейчас играет». `@бот текст`: текст песни. `@бот слова`: поиск трека.
- `/now`, `/like`, `/dislike`, `/lyrics`, `/disconnect`.
Ключи лежат в `blaze_bot.sqlite3` рядом с ботом, храни файл закрытым.
