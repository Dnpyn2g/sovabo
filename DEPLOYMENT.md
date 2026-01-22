# 🚀 Deployment Guide

## Варианты развертывания

### 1️⃣ Локальный запуск (для разработки)

```bash
# Клонировать репозиторий
git clone https://github.com/yourusername/sova-vpn-bot.git
cd sova-vpn-bot

# Создать виртуальное окружение
python -m venv venv
source venv/bin/activate  # Linux/Mac
# или
venv\Scripts\activate  # Windows

# Установить зависимости
pip install -r requirements.txt

# Настроить переменные окружения
cp .env.example .env
nano .env  # Заполнить все необходимые токены

# Запустить бота
python main.py
```

---

### 2️⃣ VPS (Ubuntu/Debian)

#### Установка

```bash
# Обновить систему
sudo apt update && sudo apt upgrade -y

# Установить Python 3.10+
sudo apt install python3 python3-pip python3-venv git -y

# Клонировать проект
git clone https://github.com/yourusername/sova-vpn-bot.git
cd sova-vpn-bot

# Создать venv
python3 -m venv venv
source venv/bin/activate

# Установить зависимости
pip install -r requirements.txt

# Настроить .env
cp .env.example .env
nano .env
```

#### Запуск как systemd сервис

```bash
# Создать сервисный файл
sudo nano /etc/systemd/system/sova-bot.service
```

Содержимое файла:
```ini
[Unit]
Description=SOVA VPN Telegram Bot
After=network.target

[Service]
Type=simple
User=your_username
WorkingDirectory=/home/your_username/sova-vpn-bot
Environment="PATH=/home/your_username/sova-vpn-bot/venv/bin"
ExecStart=/home/your_username/sova-vpn-bot/venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
# Применить и запустить
sudo systemctl daemon-reload
sudo systemctl enable sova-bot
sudo systemctl start sova-bot

# Проверить статус
sudo systemctl status sova-bot

# Логи
sudo journalctl -u sova-bot -f
```

---

### 3️⃣ Docker (рекомендуется)

#### Создать Dockerfile

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Установить системные зависимости
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Копировать requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копировать код
COPY . .

# Создать директории
RUN mkdir -p artifacts backups logs data

CMD ["python", "main.py"]
```

#### docker-compose.yml

```yaml
version: '3.8'

services:
  bot:
    build: .
    container_name: sova-vpn-bot
    restart: unless-stopped
    env_file:
      - .env
    volumes:
      - ./bot.db:/app/bot.db
      - ./artifacts:/app/artifacts
      - ./backups:/app/backups
      - ./logs:/app/logs
    ports:
      - "5000:5000"  # CRM панель
      - "5001:5001"  # Web app
```

#### Запуск

```bash
# Сборка
docker-compose build

# Запуск
docker-compose up -d

# Логи
docker-compose logs -f

# Остановка
docker-compose down
```

---

### 4️⃣ Cloud Platforms

#### Heroku

```bash
# Установить Heroku CLI
# https://devcenter.heroku.com/articles/heroku-cli

# Создать Procfile
echo "worker: python main.py" > Procfile

# Создать runtime.txt
echo "python-3.11.0" > runtime.txt

# Деплой
heroku login
heroku create sova-vpn-bot
heroku config:set BOT_TOKEN=your_token
heroku config:set ADMIN_CHAT_ID=your_id
# ... остальные переменные

git push heroku main
heroku ps:scale worker=1
```

#### Railway.app

1. Создать аккаунт на railway.app
2. New Project → Deploy from GitHub
3. Выбрать репозиторий
4. Добавить переменные окружения
5. Deploy

#### DigitalOcean App Platform

1. Создать App
2. Выбрать GitHub репозиторий
3. Environment Variables → добавить из .env
4. Deploy

---

## 🔧 Настройка после деплоя

### 1. Настройка вебхука (опционально)

Для production рекомендуется использовать webhook вместо polling:

```python
# В main.py добавить:
app.run_webhook(
    listen="0.0.0.0",
    port=int(os.environ.get('PORT', '8443')),
    url_path=BOT_TOKEN,
    webhook_url=f"https://yourdomain.com/{BOT_TOKEN}"
)
```

### 2. Настройка CRM панели

CRM панель автоматически стартует на порту 5000. Для доступа извне:

```bash
# Nginx reverse proxy
sudo apt install nginx

sudo nano /etc/nginx/sites-available/sova-crm
```

```nginx
server {
    listen 80;
    server_name crm.yourdomain.com;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/sova-crm /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx
```

### 3. SSL сертификат (Let's Encrypt)

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d crm.yourdomain.com
```

### 4. Настройка бэкапов

```bash
# Создать скрипт бэкапа
nano backup.sh
```

```bash
#!/bin/bash
DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="/home/user/backups"
DB_PATH="/home/user/sova-vpn-bot/bot.db"

mkdir -p $BACKUP_DIR
cp $DB_PATH "$BACKUP_DIR/bot_$DATE.db"

# Удалить старые бэкапы (старше 30 дней)
find $BACKUP_DIR -name "bot_*.db" -mtime +30 -delete
```

```bash
chmod +x backup.sh

# Добавить в crontab (каждые 6 часов)
crontab -e
0 */6 * * * /home/user/backup.sh
```

---

## 🔍 Мониторинг

### Логи

```bash
# Systemd
sudo journalctl -u sova-bot -f

# Docker
docker-compose logs -f bot

# Файлы
tail -f logs/bot.log
```

### Метрики

Добавить интеграцию с monitoring сервисами:
- **Sentry** - для отслеживания ошибок
- **Prometheus + Grafana** - для метрик
- **UptimeRobot** - для проверки доступности

---

## 🆘 Troubleshooting

### Бот не отвечает

```bash
# Проверить процесс
ps aux | grep python
systemctl status sova-bot

# Проверить логи
tail -100 logs/bot.log
```

### База данных заблокирована

```bash
# Найти и убить процессы
fuser bot.db
kill -9 <PID>

# Восстановить из бэкапа
cp backups/bot_latest.db bot.db
```

### Не работает провизионинг

```bash
# Проверить доступность VPS провайдеров
python -c "from fourpvs_api import FourVPSAPI; import asyncio; api = FourVPSAPI('token'); print(asyncio.run(api.get_balance()))"

# Проверить SSH доступ
ssh root@server_ip
```

---

## 📊 Performance Tips

1. **SQLite → PostgreSQL** для > 1000 пользователей
2. **Redis** для кэширования
3. **Celery** для фоновых задач
4. **Load Balancer** для масштабирования

---

Нужна помощь? Создайте [Issue](https://github.com/yourusername/sova-vpn-bot/issues)
