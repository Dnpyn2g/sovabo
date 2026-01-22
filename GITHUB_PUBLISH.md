# 📤 Публикация на GitHub - Пошаговая инструкция

## ✅ Чек-лист перед публикацией

- [x] `.gitignore` создан и настроен
- [x] `README.md` написан
- [x] `LICENSE` добавлен
- [x] `.env.example` обновлён (БЕЗ реальных токенов)
- [x] `CONTRIBUTING.md` создан
- [x] `SECURITY.md` создан
- [x] `DEPLOYMENT.md` создан
- [ ] Все чувствительные данные удалены из кода
- [ ] Проверка на секреты пройдена

---

## 🚀 Шаг 1: Проверка проекта

Запустите скрипт проверки:

```bash
python check_secrets.py
```

Если всё OK, увидите:
```
✅ Проект ГОТОВ к публикации на GitHub!
```

⚠️ **Если найдены проблемы** - исправьте их и запустите снова.

---

## 🔧 Шаг 2: Инициализация Git

```bash
# Инициализировать репозиторий
git init

# Добавить все файлы
git add .

# Проверить что добавилось (НЕ должно быть .env, bot.db, etc)
git status

# Первый коммит
git commit -m "Initial commit: SOVA VPN Bot v1.0"
```

---

## 🌐 Шаг 3: Создание репозитория на GitHub

### Вариант A: Через веб-интерфейс

1. Зайти на [github.com](https://github.com)
2. Нажать **New repository**
3. Заполнить:
   - **Repository name**: `sova-vpn-bot`
   - **Description**: `Telegram bot for automated VPN server provisioning`
   - **Public** или **Private** (на ваш выбор)
   - ❌ **НЕ** добавлять README, .gitignore, LICENSE (уже есть)
4. Нажать **Create repository**

### Вариант B: Через GitHub CLI

```bash
gh repo create sova-vpn-bot --public --source=. --remote=origin --push
```

---

## 📤 Шаг 4: Push в GitHub

Если создали через веб-интерфейс:

```bash
# Добавить remote
git remote add origin https://github.com/ваш-username/sova-vpn-bot.git

# Переименовать ветку в main (если нужно)
git branch -M main

# Push
git push -u origin main
```

---

## 🎨 Шаг 5: Настройка репозитория

### Topics (теги)

Добавьте теги для поиска:
```
telegram-bot, vpn, wireguard, xray, python, automation, vps
```

### About

Описание:
```
🦉 Automated VPN provisioning bot for Telegram with support for WireGuard, Xray VLESS, OpenVPN and more
```

Website (если есть):
```
https://yourdomain.com
```

### Branches

- Защитите ветку `main` от прямых push
- Настройте branch protection rules

### Settings → Security

- [x] Enable Dependabot alerts
- [x] Enable Dependabot security updates
- [x] Code scanning (optional)

---

## 📋 Шаг 6: Добавить документацию

Создайте Wiki страницы:
1. **Home** - Краткий обзор
2. **Installation** - Детальная установка
3. **Configuration** - Настройка параметров
4. **API Integration** - Работа с провайдерами
5. **Troubleshooting** - Решение проблем

---

## 🏷️ Шаг 7: Создать Release

```bash
# Создать тег
git tag -a v1.0.0 -m "Release v1.0.0: Initial public release"
git push origin v1.0.0
```

Затем на GitHub:
1. Releases → **Create a new release**
2. Choose tag: `v1.0.0`
3. Release title: `v1.0.0 - Initial Release`
4. Description:
```markdown
## 🎉 First Public Release

### Features
- ✅ Automated VPN server provisioning
- ✅ Multiple protocols: WireGuard, Xray VLESS, OpenVPN, SOCKS5, Trojan-Go
- ✅ Integration with 4VPS and RUVDS providers
- ✅ CRM panel for administration
- ✅ Referral program and promocodes
- ✅ Multiple payment methods (Telegram Stars, USDT, CryptoPay)

### Installation
See [DEPLOYMENT.md](DEPLOYMENT.md) for detailed instructions.

### Security
Before deploying, make sure to:
1. Copy `.env.example` to `.env`
2. Fill in all required tokens and credentials
3. Never commit `.env` to git

### Support
- Documentation: [README.md](README.md)
- Issues: [GitHub Issues](https://github.com/username/sova-vpn-bot/issues)
```

---

## 🔄 Шаг 8: Workflow для обновлений

### Работа с фичами

```bash
# Создать ветку для фичи
git checkout -b feature/new-payment-method

# Разработка...
git add .
git commit -m "Add new payment method"

# Push
git push origin feature/new-payment-method

# Создать Pull Request на GitHub
```

### Хотфиксы

```bash
git checkout -b hotfix/security-patch
# Фикс...
git commit -m "Security: Fix XXX vulnerability"
git push origin hotfix/security-patch
# PR → Review → Merge → Delete branch
```

---

## 📊 Шаг 9: Добавить бейджи в README

Добавьте в начало README.md:

```markdown
![Python](https://img.shields.io/badge/python-3.10+-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Stars](https://img.shields.io/github/stars/username/sova-vpn-bot)
![Issues](https://img.shields.io/github/issues/username/sova-vpn-bot)
![Last Commit](https://img.shields.io/github/last-commit/username/sova-vpn-bot)
```

---

## 🎯 Шаг 10: Маркетинг (опционально)

1. **Reddit**: Post на r/selfhosted, r/vpn, r/privacy
2. **Hacker News**: Submit на news.ycombinator.com
3. **Product Hunt**: Launch на producthunt.com
4. **Dev.to**: Написать статью
5. **Twitter/X**: Анонс с хэштегами #opensource #vpn #python

---

## 🔒 Безопасность после публикации

### Мониторинг

- ⚠️ Следите за issues на предмет security уязвимостей
- ⚠️ Регулярно обновляйте зависимости
- ⚠️ Используйте Dependabot

### Rotate Secrets

Если случайно закоммитили секреты:

```bash
# 1. Немедленно ротируйте ВСЕ токены
# 2. Удалите коммит из истории
git filter-branch --force --index-filter \
  "git rm --cached --ignore-unmatch .env" \
  --prune-empty --tag-name-filter cat -- --all

# 3. Force push
git push origin --force --all
git push origin --force --tags

# 4. Всем контрибьюторам:
git pull --rebase
```

---

## ✅ Финальный чеклист

Перед анонсом:

- [ ] README полный и понятный
- [ ] Все ссылки работают
- [ ] Примеры кода актуальны
- [ ] Screenshots/GIF демо добавлены
- [ ] DEPLOYMENT.md протестирован
- [ ] Issue templates созданы
- [ ] Contributing guidelines ясны
- [ ] License корректна
- [ ] Security policy опубликована
- [ ] First release создан

---

## 🎉 Готово!

Ваш проект теперь на GitHub! 🚀

**Следующие шаги:**
1. Ответить на первые issues
2. Принять первые PR
3. Собрать feedback от community
4. Итерировать и улучшать

**Удачи! ⭐**
