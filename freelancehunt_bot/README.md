# Інструкція з налаштування на VPS

## Крок 1 — Отримай токени

### Telegram Bot Token
1. Відкрий Telegram → знайди @BotFather
2. Надішли `/newbot`
3. Дай боту ім'я і username (напр. `MyFreelancehuntBot`)
4. Скопіюй токен виду `1234567890:AAxxxxxxx`

### Твій Telegram Chat ID
1. Відкрий @userinfobot або @getmyid_bot у Telegram
2. Натисни `/start` — він поверне твій `chat_id` (число)

### Freelancehunt API Token
1. Зайди на https://freelancehunt.com/my/api
2. Створи новий токен
3. Скопіюй його

---

## Крок 2 — Залий файли на VPS

```bash
# На своєму комп'ютері (або через FileZilla / SCP)
scp -r ./freelancehunt_bot ubuntu@YOUR_SERVER_IP:/home/ubuntu/
```

---

## Крок 3 — Налаштуй на сервері

```bash
# Підключись до сервера
ssh ubuntu@YOUR_SERVER_IP

# Перейди в папку бота
cd /home/ubuntu/freelancehunt_bot

# Створи .env з прикладу
cp .env.example .env
nano .env
# Заповни всі 3 токени і збережи (Ctrl+X → Y → Enter)

# Встанови Python (якщо ще немає)
sudo apt update && sudo apt install -y python3 python3-pip python3-venv

# Створи віртуальне середовище
python3 -m venv venv

# Активуй і встанови залежності
source venv/bin/activate
pip install -r requirements.txt
```

---

## Крок 4 — Перевір що все працює

```bash
python bot.py
```

Якщо все ок — побачиш в Telegram повідомлення "Freelancehunt бот запущено!"  
Зупини: `Ctrl + C`

---

## Крок 5 — Налаштуй автозапуск через systemd

```bash
# Відредагуй шлях у файлі сервісу (якщо твій юзер не ubuntu)
nano freelancehunt-bot.service
# Зміни User= і WorkingDirectory= якщо потрібно

# Скопіюй файл сервісу
sudo cp freelancehunt-bot.service /etc/systemd/system/

# Активуй і запусти
sudo systemctl daemon-reload
sudo systemctl enable freelancehunt-bot
sudo systemctl start freelancehunt-bot
```

---

## Корисні команди

```bash
# Перевірити статус
sudo systemctl status freelancehunt-bot

# Переглянути логи в реальному часі
sudo journalctl -u freelancehunt-bot -f

# Переглянути файл логів
tail -f /home/ubuntu/freelancehunt_bot/bot.log

# Перезапустити бота
sudo systemctl restart freelancehunt-bot

# Зупинити бота
sudo systemctl stop freelancehunt-bot
```

---

## Фільтр за навичками (необов'язково)

Якщо хочеш отримувати тільки проекти за своїми навичками — заповни `SKILL_IDS` у `.env`.

Список всіх навичок і їх ID:
https://api.freelancehunt.com/v2/skills

Приклад для Python + Django:
```
SKILL_IDS=22,23
```
