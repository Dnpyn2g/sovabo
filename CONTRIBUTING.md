# Contributing to SOVA VPN Bot

Спасибо за интерес к проекту! 🎉

## 🐛 Reporting Bugs

Если вы нашли баг:
1. Проверьте, не был ли он уже зарепорчен в [Issues](https://github.com/yourusername/sova-vpn-bot/issues)
2. Создайте новый Issue с подробным описанием:
   - Шаги для воспроизведения
   - Ожидаемое поведение
   - Фактическое поведение
   - Версия Python и ОС
   - Логи (если есть)

## 💡 Feature Requests

Есть идея улучшения? Создайте Issue с меткой `enhancement`

## 🔧 Pull Requests

1. Fork проекта
2. Создайте feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit изменения (`git commit -m 'Add some AmazingFeature'`)
4. Push в branch (`git push origin feature/AmazingFeature`)
5. Откройте Pull Request

### Требования к коду:
- Следуйте PEP 8
- Добавьте docstrings к функциям
- Протестируйте изменения
- Обновите README.md если нужно

## 📝 Code Style

```python
# Хорошо ✅
async def get_balance(user_id: int) -> float:
    """
    Получить баланс пользователя.
    
    Args:
        user_id: ID пользователя в Telegram
        
    Returns:
        float: Баланс в долларах
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        return float(row[0]) if row else 0.0

# Плохо ❌
def get_balance(uid):
    db = sqlite3.connect('bot.db')
    return db.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()[0]
```

## 🧪 Testing

Перед отправкой PR убедитесь что:
- [ ] Код запускается без ошибок
- [ ] Все новые функции протестированы
- [ ] Нет конфликтов с main веткой

## 📄 License

Отправляя PR вы соглашаетесь с MIT лицензией проекта.
