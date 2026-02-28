# Freelancehunt → Telegram Bot

Бот надсилає в Telegram нові проекти, повідомлення та сповіщення з Freelancehunt.
Хоститься на Railway, автодеплой з GitHub.

---

## Деплой на Railway (через GitHub)

### Крок 1 — Залий файли в репо

Потрібні файли в корені репо `Sk1ttels/Freelancehunt`:
```
bot.py
requirements.txt
Procfile
nixpacks.toml
```

> `.env` файл НЕ заливай в GitHub — токени додаються через Railway Variables.

### Крок 2 — Додай змінні в Railway

Відкрий свій сервіс → вкладка Variables → додай:

| Key                      | Value                        |
|--------------------------|------------------------------|
| TELEGRAM_BOT_TOKEN       | токен від @BotFather         |
| TELEGRAM_CHAT_ID         | твій chat_id                 |
| FREELANCEHUNT_TOKEN      | токен з freelancehunt.com/my/api |
| CHECK_INTERVAL_SECONDS   | 300                          |
| SKILL_IDS                | (залиш пустим)               |

### Крок 3 — Deploy

Натисни Deploy або зроби git push — Railway сам збере і запустить бота.

Перевір вкладку Logs — має з'явитися запис про запуск,
і в Telegram прийде повідомлення "Freelancehunt бот запущено!"

---

## Що надсилає бот

- Нові проекти — з кнопками "Відкрити проект" і "Профіль замовника"
- Нові повідомлення — непрочитані листи від клієнтів
- Сповіщення — виграш тендеру, відгуки, зміни статусу

Перевірка кожні 5 хвилин.
