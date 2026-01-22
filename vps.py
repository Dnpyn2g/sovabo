from __future__ import annotations
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes


async def _on_vps_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Placeholder page for VPS purchasing.
    Shows a simple info text and a back button to main menu.
    """
    query = update.callback_query
    await query.answer()
    text = (
        "<b>🖥️ Купить VPS</b>\n\n"
        "Раздел в разработке. Скоро здесь можно будет выбрать тариф и оформить заказ.\n\n"
        "Пока что, если нужно — напишите в поддержку."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ В меню", callback_data="back:main")]
    ])
    # Use edit to keep the chat clean
    try:
        await query.edit_message_text(text=text, reply_markup=kb, parse_mode='HTML')
    except Exception:
        # Fallback: send as a new message
        try:
            await context.bot.send_message(chat_id=update.effective_user.id, text=text, reply_markup=kb, parse_mode='HTML')
        except Exception:
            pass


def register_vps_handlers(app: Application) -> None:
    """Register handlers related to VPS menu."""
    app.add_handler(CallbackQueryHandler(_on_vps_menu, pattern=r"^menu:vps$"), group=0)
