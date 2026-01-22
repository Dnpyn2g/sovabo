from __future__ import annotations
from typing import Optional, Dict

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

# In-memory state for support chat
SUPPORT_REPLY_PENDING: Dict[int, str] = {} # one-shot reply: {sender_id: 'admin'|'user:<id>'}


def _user_title(u) -> str:
    uname = f"@{u.username}" if getattr(u, 'username', None) else "—"
    return f"{u.full_name} {uname} <code>{u.id}</code>"


async def support_start(update: Update, context: ContextTypes.DEFAULT_TYPE, admin_id: int) -> None:
    user = update.effective_user
    uid = user.id
    # One-shot: the very next message will go to admin
    SUPPORT_REPLY_PENDING[uid] = 'admin'
    # Tell user with better instructions
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить", callback_data="support:cancel")]])
    await update.effective_message.reply_text(
        "🆘 <b>Связь с поддержкой</b>\n\n"
        "Опишите вашу проблему или вопрос.\n"
        "Администратор получит ваше сообщение и ответит в ближайшее время.\n\n"
        "✍️ Напишите сообщение ниже:",
        parse_mode='HTML',
        reply_markup=kb
    )


 


async def support_set_reply_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, admin_id: int) -> None:
    if update.effective_user.id != admin_id:
        return
    args = context.args or []
    if not args:
        await update.effective_message.reply_text("Использование: /reply <user_id>")
        return
    try:
        target = int(args[0])
    except Exception:
        await update.effective_message.reply_text("Некорректный user_id")
        return
    # One-shot: next admin message will go to this user
    SUPPORT_REPLY_PENDING[admin_id] = f'user:{target}'
    await update.effective_message.reply_text(f"Следующее сообщение будет отправлено пользователю {target}")


async def support_on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, admin_id: int) -> None:
    data = update.callback_query.data or ''
    uid = update.effective_user.id if update.effective_user else None
    
    # User cancels support request
    if data == 'support:cancel':
        if not uid:
            await update.callback_query.answer()
            return
        # Clear pending state
        SUPPORT_REPLY_PENDING.pop(uid, None)
        await update.callback_query.answer("Отменено")
        try:
            await update.callback_query.edit_message_text(
                "❌ Обращение в поддержку отменено.\n\n"
                "Если понадобится помощь, нажмите 📞 Поддержка в главном меню."
            )
        except Exception as e:
            print(f"Support cancel edit_message_text error: {e}")
        return
    
    # User taps reply button to answer admin
    if data == 'support:reply_admin':
        if not uid:
            await update.callback_query.answer()
            return
        SUPPORT_REPLY_PENDING[uid] = 'admin'
        await update.callback_query.answer("Напишите ответ администратору")
        try:
            await update.callback_query.edit_message_reply_markup(reply_markup=None)
        except Exception as e:
            print(f"Support reply_admin edit_markup error: {e}")
        # Visible instruction for the user
        try:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить", callback_data="support:cancel")]])
            await update.callback_query.message.reply_text(
                "✍️ <b>Ответ администратору</b>\n\n"
                "Напишите ваше сообщение:",
                parse_mode='HTML',
                reply_markup=kb
            )
        except Exception as e:
            print(f"Support reply_admin instruction error: {e}")
        return
    # Admin taps reply under user's message
    if data.startswith('support:reply:'):
        if uid != admin_id:
            await update.callback_query.answer("Только админ")
            return
        try:
            target = int(data.split(':', 2)[2])
        except Exception:
            await update.callback_query.answer("Ошибка")
            return
        SUPPORT_REPLY_PENDING[admin_id] = f'user:{target}'
        await update.callback_query.answer("Напишите ответ")
        try:
            await update.callback_query.edit_message_reply_markup(reply_markup=None)
        except Exception as e:
            print(f"Support reply edit_markup error: {e}")
        # Visible instruction for the admin
        try:
            await update.callback_query.message.reply_text(
                f"✍️ Напишите сообщение для ответа пользователю {target} и отправьте его боту.")
        except Exception as e:
            print(f"Support reply instruction error: {e}")


async def support_router(update: Update, context: ContextTypes.DEFAULT_TYPE, admin_id: int) -> None:
    """Route non-command messages between user and admin when support is active."""
    msg = update.effective_message
    u = update.effective_user
    
    # Save message to database for CRM
    async def save_message_to_db(user_id: int, message_text: str, is_from_user: bool):
        try:
            import aiosqlite
            try:
                from .main import DB_PATH  # type: ignore
            except:
                from main import DB_PATH  # type: ignore
            
            async with aiosqlite.connect(DB_PATH, timeout=30) as db:
                # Create table if not exists
                await db.execute('''
                    CREATE TABLE IF NOT EXISTS support_messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        message_text TEXT,
                        is_from_user INTEGER DEFAULT 1,
                        is_read INTEGER DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                
                # Insert message
                await db.execute('''
                    INSERT INTO support_messages (user_id, message_text, is_from_user, is_read)
                    VALUES (?, ?, ?, 0)
                ''', (user_id, message_text, 1 if is_from_user else 0))
                
                await db.commit()
        except Exception as e:
            print(f"Error saving support message to DB: {e}")
    
    # Admin sends — route only if a target was selected via reply button or /reply
    if u and u.id == admin_id:
        # If admin is currently in another admin flow (e.g., top-up, search, goto),
        # do not intercept the message with support routing.
        try:
            try:
                from .main import ADMIN_ACTION_STATE as _ADMIN_ACTION_STATE  # type: ignore
            except Exception:
                from main import ADMIN_ACTION_STATE as _ADMIN_ACTION_STATE  # type: ignore
        except Exception:
            _ADMIN_ACTION_STATE = {}
        try:
            if _ADMIN_ACTION_STATE.get(admin_id):
                # Let other handlers (unknown_message) process this admin input silently
                return
        except Exception as e:
            print(f"Support ADMIN_ACTION_STATE check error: {e}")
        # One-shot pending has priority
        target_id: Optional[int] = None
        pend = SUPPORT_REPLY_PENDING.pop(admin_id, None)
        if pend and pend.startswith('user:'):
            try:
                target_id = int(pend.split(':', 1)[1])
            except Exception:
                target_id = None
        if target_id:
            try:
                # Notify user that admin replied
                try:
                    await context.bot.send_message(
                        chat_id=target_id,
                        text="💬 <b>Новый ответ от администратора:</b>",
                        parse_mode='HTML'
                    )
                except Exception as e:
                    print(f"Support admin reply header send error: {e}")
                
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("💬 Ответить", callback_data="support:reply_admin")]])
                await context.bot.copy_message(chat_id=target_id, from_chat_id=msg.chat_id, message_id=msg.message_id, reply_markup=kb)
                # Delivery confirmation for admin
                try:
                    await msg.reply_text("✅ Сообщение доставлено пользователю.")
                except Exception as e:
                    print(f"Support admin confirmation error: {e}")
            except Exception:
                await msg.reply_text("❌ Не удалось отправить пользователю.")
        else:
            # No target selected — do not interrupt other admin flows; just ignore.
            return
    # User sends — route only after pressing «Поддержка» (первое сообщение) или «Ответить»
    uid = u.id if u else None
    if uid and admin_id and (SUPPORT_REPLY_PENDING.pop(uid, None) == 'admin'):
        # Save user message to DB
        msg_text = msg.text or msg.caption or '<медиа>'
        await save_message_to_db(uid, msg_text, True)
        
        # Small sender info for admin
        try:
            # quick stats
            orders_cnt = 0
            deposits_cnt = 0
            deposits_sum = 0.0
            try:
                import aiosqlite
                from .main import DB_PATH  # type: ignore
                async with aiosqlite.connect(DB_PATH, timeout=30) as db:
                    cur = await db.execute("SELECT COUNT(*) FROM orders WHERE user_id=?", (uid,))
                    row = await cur.fetchone()
                    orders_cnt = (row[0] or 0) if row else 0
                    cur = await db.execute("SELECT COUNT(*), IFNULL(SUM(expected_amount_usdt),0) FROM deposits WHERE user_id=?", (uid,))
                    row = await cur.fetchone()
                    if row:
                        deposits_cnt = row[0] or 0
                        deposits_sum = float(row[1] or 0)
            except Exception as e:
                print(f"Support user stats fetch error: {e}")
            info = (
                f"📩 <b>Сообщение от пользователя</b>\n\n"
                f"👤 {_user_title(u)}\n"
                f"🕒 {msg.date.strftime('%Y-%m-%d %H:%M:%S') if msg and msg.date else '-'}\n"
                f"🧾 Заказов: <b>{orders_cnt}</b> | 💳 Депозитов: <b>{deposits_cnt}</b> (<b>{deposits_sum:.2f} USDT</b>)"
            )
            await context.bot.send_message(chat_id=admin_id, text=info, parse_mode='HTML')
        except Exception as e:
            print(f"Support admin info send error: {e}")
        sent_ok = False
        try:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("💬 Ответить", callback_data=f"support:reply:{uid}")]])
            await context.bot.copy_message(chat_id=admin_id, from_chat_id=msg.chat_id, message_id=msg.message_id, reply_markup=kb)
            sent_ok = True
        except Exception:
            # Fallback to plain text
            try:
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("💬 Ответить", callback_data=f"support:reply:{uid}")]])
                await context.bot.send_message(
                    chat_id=admin_id, 
                    text=f"📝 Текст сообщения:\n\n{msg.text or '<медиа>'}",
                    parse_mode='HTML',
                    reply_markup=kb
                )
                sent_ok = True
            except Exception:
                sent_ok = False
        # Delivery confirmation for user
        try:
            if sent_ok:
                await msg.reply_text(
                    "✅ <b>Сообщение отправлено администратору</b>\n\n"
                    "Ваше обращение получено. Администратор ответит в ближайшее время.\n"
                    "Вы получите уведомление, когда придёт ответ.",
                    parse_mode='HTML'
                )
            else:
                await msg.reply_text(
                    "❌ <b>Ошибка отправки</b>\n\n"
                    "Не удалось доставить сообщение администратору.\n"
                    "Пожалуйста, попробуйте позже.",
                    parse_mode='HTML'
                )
        except Exception as e:
            print(f"Support user confirmation error: {e}")
        return
    # Otherwise ignore and let other handlers process


def register_support_handlers(app: Application, admin_id: int) -> None:
    """Attach support handlers to the application."""
    # Commands
    app.add_handler(CommandHandler("support", lambda u, c: support_start(u, c, admin_id)))
    app.add_handler(CommandHandler("reply", lambda u, c: support_set_reply_cmd(u, c, admin_id)))
    # Callback for inline buttons
    app.add_handler(CallbackQueryHandler(lambda u, c: support_on_callback(u, c, admin_id), pattern=r"^support:"))
    # Message router:
    #  - Users (non-admin): group 0 (before unknown_message), block others by default
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND & ~filters.User(admin_id), lambda u, c: support_router(u, c, admin_id)), group=0)
    #  - Admin: group 2 (after unknown_message group 1), so admin flows process first
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND & filters.User(admin_id), lambda u, c: support_router(u, c, admin_id)), group=2)
