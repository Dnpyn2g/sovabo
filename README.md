# 🦉 SOVA VPN Bot

Telegram бот для автоматической продажи и настройки VPN серверов с поддержкой множества протоколов.

## ✨ Основные возможности

### 🚀 Для пользователей:
- **Автоматическая выдача VPN** - сервер арендуется и настраивается автоматически за 3-5 минут
- **Множество протоколов**: WireGuard, AmneziaWG, OpenVPN, SOCKS5, Xray VLESS, Trojan-Go
- **Гибкое ценообразование** - от 1 до 250 конфигов, сроки от 1 до 12 месяцев
- **Система промокодов** - скидки и бонусы для клиентов
- **Реферальная программа** - зарабатывайте на привлечении новых пользователей
- **Множество способов оплаты**: Telegram Stars, USDT (TRC20), CryptoPay

### ⚙️ Для администратора:
- **CRM панель** - управление пользователями, заказами, промокодами
- **Автоматический провизионинг** - настройка серверов без ручного вмешательства
- **Интеграция с провайдерами** - 4VPS и RUVDS
- **Система рассылок** - отправка уведомлений всем пользователям
- **Статистика и аналитика** - отслеживание продаж и активности

## 🏗️ Архитектура

```
┌─────────────────────────────────────────────────────┐
│                  Telegram Bot                       │
│                    (main.py)                        │
└──────────────────┬──────────────────────────────────┘
                   │
        ┌──────────┴──────────┬─────────────┬─────────┐
        │                     │             │         │
┌───────▼────────┐   ┌───────▼─────┐  ┌────▼────┐   │
│  Auto Issue    │   │   Payment   │  │   CRM   │   │
│  (auto_issue)  │   │  (crypto)   │  │ (Flask) │   │
└───────┬────────┘   └─────────────┘  └─────────┘   │
        │                                             │
┌───────▼────────────────────────────────────────────▼┐
│            VPS Providers Integration                │
│  ┌──────────────────┐  ┌──────────────────┐        │
│  │  4VPS API        │  │   RUVDS API      │        │
│  │ (fourpvs_api.py) │  │ (rent_server.py) │        │
│  └──────────────────┘  └──────────────────┘        │
└───────┬─────────────────────────────────────────────┘
        │
┌───────▼────────────────────────────────────────────┐
│          Server Provisioning Scripts               │
│  provision_wg.py, provision_xray.py, etc.          │
└────────────────────────────────────────────────────┘
```

## 📦 Установка

### Требования:
- Python 3.10+
- SQLite3
- Telegram Bot Token
- API токены провайдеров (4VPS/RUVDS)

### Быстрый старт:

1. **Клонировать репозиторий:**
```bash
git clone https://github.com/yourusername/sova-vpn-bot.git
cd sova-vpn-bot
```

2. **Установить зависимости:**
```bash
pip install -r requirements.txt
```

3. **Настроить переменные окружения:**
```bash
cp .env.example .env
nano .env  # Заполните BOT_TOKEN, ADMIN_CHAT_ID и другие параметры
```

4. **Запустить бота:**
```bash
python main.py
```

## 🔧 Конфигурация

### Основные переменные окружения (.env):

```env
# Telegram Bot
BOT_TOKEN=your_bot_token_here
ADMIN_CHAT_ID=your_telegram_id

# Support
SUPPORT_USERNAME=your_support_username
SUPPORT_TEXT=Текст для кнопки поддержки

# Crypto payments
TRON_ADDRESS=your_tron_wallet
CRYPTO_PAY_TOKEN=your_cryptopay_token

# VPS Providers
FOURVPS_API_TOKEN=your_4vps_token
RUVDS_API_TOKEN=your_ruvds_token

# Pricing
WG_MONTH_OPTIONS=1,2,3,6,12
WG_DISCOUNTS=2:0.05,3:0.10,6:0.15,12:0.25
```

## 📊 Система ценообразования

Цены настраиваются в файле `pricing_config.py`:

```python
VOLUME_TARIFFS = [
    {"min": 1,   "max": 15,   "price_month": 20.0},  # $20/мес
    {"min": 16,  "max": 30,   "price_month": 25.0},  # $25/мес
    {"min": 31,  "max": 100,  "price_month": 60.0},  # $60/мес
    {"min": 101, "max": 250,  "price_month": 120.0}, # $120/мес
]

TERM_FACTORS = {
    1:  {"factor": 1.0,  "discount": 0},   # Без скидки
    3:  {"factor": 2.7,  "discount": 10},  # -10%
    12: {"factor": 9.0,  "discount": 25},  # -25%
}
```

## 🛠️ Модули

### Основные файлы:

- **main.py** - Главный файл бота, обработка команд и callback
- **auto_issue.py** - Автоматическая выдача VPN серверов
- **pricing_config.py** - Система ценообразования
- **promocodes.py** - Управление промокодами
- **fourpvs_api.py** - Интеграция с 4VPS API
- **rent_server_4vps.py** - Логика аренды серверов на 4VPS
- **crm_app.py** - Flask CRM панель
- **web_app.py** - Web интерфейс для пользователей

### Провизионинг:

- **provision_wg.py** - Настройка WireGuard
- **provision_xray.py** - Настройка Xray VLESS + REALITY
- **provision_awg.py** - Настройка AmneziaWG
- **provision_ovpn.py** - Настройка OpenVPN
- **provision_socks5.py** - Настройка SOCKS5
- **provision_trojan.py** - Настройка Trojan-Go

### Управление конфигами:

- **manage_wg.py** - Управление WireGuard peers
- **manage_xray.py** - Управление Xray клиентами
- И т.д. для каждого протокола

## 🗄️ База данных

Используется SQLite с автоматическими миграциями при запуске.

### Основные таблицы:

- **users** - Пользователи и их балансы
- **orders** - Заказы VPN серверов
- **peers** - Конфигурации для клиентов
- **deposits** - История пополнений
- **promocodes** - Промокоды и скидки
- **deposit_bonuses** - Бонусы за пополнение
- **auth_tokens** - Токены для web авторизации

## 🌐 API провайдеров

### 4VPS.SU:
- Документация: https://4vps.su/page/api
- Эндпоинты: `/api/getDcList`, `/api/buyServer`, `/api/getServerInfo`

### RUVDS:
- API Base: https://api.ruvds.com/v2
- Методы: создание серверов, управление, мониторинг

## 🔐 Безопасность

⚠️ **ВАЖНО:** Не коммитьте в репозиторий:
- `.env` файл с токенами
- `bot.db` с пользовательскими данными
- `99.txt`, `servera.txt` с учётными данными серверов
- Папки `artifacts/`, `backups/`, `logs/`

Все чувствительные данные уже добавлены в `.gitignore`

## 📝 Лицензия

MIT License - см. файл LICENSE

## 🤝 Поддержка

- Telegram: [@your_support]
- Issues: [GitHub Issues](https://github.com/yourusername/sova-vpn-bot/issues)

## 📈 Roadmap

- [ ] Поддержка Docker
- [ ] Автоматическое продление серверов
- [ ] Интеграция с большим количеством провайдеров
- [ ] Мобильное приложение
- [ ] Расширенная аналитика

---

Made with ❤️ for the VPN community
