"""
Модуль управления промокодами
Поддерживает различные типы скидок и бонусов
"""

import aiosqlite
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, List, Tuple
import os

logger = logging.getLogger(__name__)

# Get correct DB path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'bot.db')
DB_TIMEOUT = float(os.getenv('DB_TIMEOUT', '30'))

# Типы промокодов
PROMO_TYPES = {
    'deposit_bonus': 'Бонус к пополнению',
    'vpn_discount': 'Скидка на VPN',
    'country_discount': 'Скидка на страну',
    'protocol_discount': 'Скидка на протокол',
    'first_order': 'Скидка на первый заказ',
}


async def validate_promocode(
    code: str,
    user_id: int,
    order_type: Optional[str] = None,
    country: Optional[str] = None,
    protocol: Optional[str] = None
) -> Tuple[bool, str, Optional[Dict]]:
    """
    Проверить промокод на валидность
    
    Returns:
        (valid, message, promo_data)
    """
    try:
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            # Получаем промокод
            cur = await db.execute(
                """SELECT id, code, type, discount_percent, bonus_amount, 
                   country, protocol, max_uses, current_uses, expires_at, is_active
                   FROM promocodes WHERE LOWER(code) = LOWER(?)""",
                (code,)
            )
            row = await cur.fetchone()
            
            if not row:
                return False, "❌ Промокод не найден", None
            
            promo_id, promo_code, promo_type, discount_percent, bonus_amount, \
                promo_country, promo_protocol, max_uses, current_uses, expires_at, is_active = row
            
            # Проверка активности
            if not is_active:
                return False, "❌ Промокод деактивирован", None
            
            # Проверка срока действия
            if expires_at:
                try:
                    exp_dt = datetime.fromisoformat(expires_at.replace(' ', 'T'))
                    if datetime.now(timezone.utc) > exp_dt:
                        return False, "❌ Срок действия промокода истёк", None
                except Exception:
                    pass
            
            # Проверка лимита использований
            if max_uses and current_uses >= max_uses:
                return False, "❌ Промокод исчерпан (достигнут лимит активаций)", None
            
            # Проверка, использовал ли уже этот пользователь промокод
            cur = await db.execute(
                "SELECT 1 FROM promocode_usage WHERE promocode_id = ? AND user_id = ?",
                (promo_id, user_id)
            )
            if await cur.fetchone():
                return False, "❌ Вы уже использовали этот промокод", None
            
            # Проверка соответствия условиям
            if promo_type == 'country_discount' and promo_country:
                if not country or country != promo_country:
                    return False, f"❌ Промокод действует только для страны: {promo_country}", None
            
            if promo_type == 'protocol_discount' and promo_protocol:
                if not protocol or protocol != promo_protocol:
                    protocol_names = {
                        'wg': 'WireGuard',
                        'awg': 'AmneziaWG',
                        'ovpn': 'OpenVPN',
                        'socks5': 'SOCKS5',
                        'xray': 'Xray VLESS',
                        'trojan': 'Trojan-Go'
                    }
                    proto_label = protocol_names.get(promo_protocol, promo_protocol)
                    return False, f"❌ Промокод действует только для протокола: {proto_label}", None
            
            # Формируем данные промокода
            promo_data = {
                'id': promo_id,
                'code': promo_code,
                'type': promo_type,
                'discount_percent': discount_percent,
                'bonus_amount': bonus_amount,
                'country': promo_country,
                'protocol': promo_protocol,
            }
            
            # Формируем сообщение об успехе
            if promo_type == 'deposit_bonus' and bonus_amount:
                message = f"✅ Промокод активирован! Бонус +{bonus_amount:.0f}₽ к пополнению"
            elif promo_type in ('vpn_discount', 'country_discount', 'protocol_discount', 'first_order') and discount_percent:
                message = f"✅ Промокод активирован! Скидка {discount_percent:.0f}% на заказ"
            else:
                message = "✅ Промокод активирован!"
            
            return True, message, promo_data
            
    except Exception as e:
        logger.error(f"Error validating promocode '{code}' for user {user_id}: {type(e).__name__}: {e}", exc_info=True)
        return False, f"❌ Ошибка проверки промокода: {type(e).__name__}", None


async def apply_promocode_to_deposit(user_id: int, promo_code: str, deposit_amount: float) -> Tuple[float, Optional[int]]:
    """
    Применить промокод к пополнению
    
    Returns:
        (bonus_amount, promocode_id)
    """
    try:
        valid, message, promo_data = await validate_promocode(promo_code, user_id, order_type='deposit')
        
        if not valid or not promo_data:
            return 0.0, None
        
        if promo_data['type'] != 'deposit_bonus':
            return 0.0, None
        
        bonus = promo_data.get('bonus_amount', 0.0)
        
        # Check if usage already recorded (from activation)
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute(
                "SELECT id FROM promocode_usage WHERE promocode_id = ? AND user_id = ?",
                (promo_data['id'], user_id)
            )
            existing = await cur.fetchone()
            
            if existing:
                # Update existing record with actual bonus amount
                await db.execute(
                    """UPDATE promocode_usage 
                       SET discount_applied = ? 
                       WHERE promocode_id = ? AND user_id = ?""",
                    (bonus, promo_data['id'], user_id)
                )
                logger.info(f"Updated existing promocode usage record for deposit by user {user_id}")
            else:
                # Create new record (shouldn't happen if user activated via button, but just in case)
                await db.execute(
                    """INSERT INTO promocode_usage (promocode_id, user_id, discount_applied)
                       VALUES (?, ?, ?)""",
                    (promo_data['id'], user_id, bonus)
                )
                await db.execute(
                    "UPDATE promocodes SET current_uses = IFNULL(current_uses, 0) + 1 WHERE id = ?",
                    (promo_data['id'],)
                )
                logger.info(f"Created new promocode usage record for deposit by user {user_id}")
            
            await db.commit()
        
        return bonus, promo_data['id']
        
    except Exception as e:
        logger.error(f"Error applying promocode to deposit: {e}")
        return 0.0, None


async def apply_promocode_to_order(
    user_id: int,
    promo_code: str,
    order_price: float,
    country: Optional[str] = None,
    protocol: Optional[str] = None
) -> Tuple[float, Optional[int]]:
    """
    Применить промокод к заказу VPN
    
    Returns:
        (discount_amount, promocode_id)
    """
    try:
        valid, message, promo_data = await validate_promocode(
            promo_code, user_id, order_type='vpn', country=country, protocol=protocol
        )
        
        if not valid or not promo_data:
            return 0.0, None
        
        if promo_data['type'] not in ('vpn_discount', 'country_discount', 'protocol_discount', 'first_order'):
            return 0.0, None
        
        discount_percent = promo_data.get('discount_percent', 0.0)
        discount_amount = order_price * (discount_percent / 100.0)
        
        # Check if usage already recorded (from activation)
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute(
                "SELECT id FROM promocode_usage WHERE promocode_id = ? AND user_id = ?",
                (promo_data['id'], user_id)
            )
            existing = await cur.fetchone()
            
            if existing:
                # Update existing record with discount amount and order info will be added later
                await db.execute(
                    """UPDATE promocode_usage 
                       SET discount_applied = ? 
                       WHERE promocode_id = ? AND user_id = ?""",
                    (discount_amount, promo_data['id'], user_id)
                )
                logger.info(f"Updated existing promocode usage record for user {user_id}")
            else:
                # Create new record (shouldn't happen if user activated via button, but just in case)
                await db.execute(
                    """INSERT INTO promocode_usage (promocode_id, user_id, discount_applied)
                       VALUES (?, ?, ?)""",
                    (promo_data['id'], user_id, discount_amount)
                )
                await db.execute(
                    "UPDATE promocodes SET current_uses = IFNULL(current_uses, 0) + 1 WHERE id = ?",
                    (promo_data['id'],)
                )
                logger.info(f"Created new promocode usage record for user {user_id}")
            
            await db.commit()
        
        return discount_amount, promo_data['id']
        
    except Exception as e:
        logger.error(f"Error applying promocode to order: {e}")
        return 0.0, None


async def link_promocode_to_order(promocode_id: int, order_id: int):
    """Привязать промокод к заказу"""
    try:
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            await db.execute(
                "UPDATE promocode_usage SET order_id = ? WHERE promocode_id = ? AND order_id IS NULL",
                (order_id, promocode_id)
            )
            await db.commit()
    except Exception as e:
        logger.error(f"Error linking promocode to order: {e}")


async def create_promocode(
    code: str,
    promo_type: str,
    discount_percent: Optional[float] = None,
    bonus_amount: Optional[float] = None,
    country: Optional[str] = None,
    protocol: Optional[str] = None,
    max_uses: Optional[int] = None,
    expires_at: Optional[datetime] = None,
    created_by: Optional[int] = None,
    description: Optional[str] = None
) -> Tuple[bool, str]:
    """
    Создать новый промокод (для CRM)
    
    Returns:
        (success, message)
    """
    try:
        if promo_type not in PROMO_TYPES:
            return False, f"Неверный тип промокода. Доступные: {', '.join(PROMO_TYPES.keys())}"
        
        # Проверка обязательных полей
        if promo_type == 'deposit_bonus' and not bonus_amount:
            return False, "Для типа 'deposit_bonus' требуется указать bonus_amount"
        
        if promo_type in ('vpn_discount', 'country_discount', 'protocol_discount', 'first_order') and not discount_percent:
            return False, f"Для типа '{promo_type}' требуется указать discount_percent"
        
        if promo_type == 'country_discount' and not country:
            return False, "Для типа 'country_discount' требуется указать country"
        
        if promo_type == 'protocol_discount' and not protocol:
            return False, "Для типа 'protocol_discount' требуется указать protocol"
        
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            # Проверка уникальности кода
            cur = await db.execute("SELECT 1 FROM promocodes WHERE LOWER(code) = LOWER(?)", (code,))
            if await cur.fetchone():
                return False, f"Промокод '{code}' уже существует"
            
            # Создаём промокод
            await db.execute(
                """INSERT INTO promocodes 
                   (code, type, discount_percent, bonus_amount, country, protocol, 
                    max_uses, expires_at, created_by, description)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (code, promo_type, discount_percent, bonus_amount, country, protocol,
                 max_uses, expires_at, created_by, description)
            )
            await db.commit()
        
        return True, f"✅ Промокод '{code}' успешно создан!"
        
    except Exception as e:
        logger.error(f"Error creating promocode: {e}")
        return False, f"Ошибка создания промокода: {str(e)}"


async def get_all_promocodes() -> List[Dict]:
    """Получить все промокоды (для CRM)"""
    try:
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute(
                """SELECT id, code, type, discount_percent, bonus_amount, 
                   country, protocol, max_uses, current_uses, expires_at, 
                   is_active, description, created_at
                   FROM promocodes ORDER BY created_at DESC"""
            )
            rows = await cur.fetchall()
            
            promocodes = []
            for row in rows:
                promo_id, code, promo_type, discount_percent, bonus_amount, \
                    country, protocol, max_uses, current_uses, expires_at, \
                    is_active, description, created_at = row
                
                promocodes.append({
                    'id': promo_id,
                    'code': code,
                    'type': promo_type,
                    'type_label': PROMO_TYPES.get(promo_type, promo_type),
                    'discount_percent': discount_percent,
                    'bonus_amount': bonus_amount,
                    'country': country,
                    'protocol': protocol,
                    'max_uses': max_uses,
                    'current_uses': current_uses,
                    'expires_at': expires_at,
                    'is_active': bool(is_active),
                    'description': description,
                    'created_at': created_at,
                })
            
            return promocodes
            
    except Exception as e:
        logger.error(f"Error getting promocodes: {e}")
        return []


async def toggle_promocode_status(promo_id: int) -> Tuple[bool, str]:
    """Активировать/деактивировать промокод"""
    try:
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            cur = await db.execute("SELECT is_active FROM promocodes WHERE id = ?", (promo_id,))
            row = await cur.fetchone()
            
            if not row:
                return False, "Промокод не найден"
            
            new_status = 0 if row[0] else 1
            await db.execute("UPDATE promocodes SET is_active = ? WHERE id = ?", (new_status, promo_id))
            await db.commit()
            
            status_text = "активирован" if new_status else "деактивирован"
            return True, f"✅ Промокод {status_text}"
            
    except Exception as e:
        logger.error(f"Error toggling promocode status: {e}")
        return False, "Ошибка изменения статуса"


async def get_promocode_stats(promo_id: int) -> Optional[Dict]:
    """Получить статистику по промокоду"""
    try:
        async with aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT) as db:
            # Основная информация
            cur = await db.execute(
                """SELECT code, type, current_uses, max_uses, 
                   SUM(CASE WHEN pu.discount_applied IS NOT NULL 
                       THEN pu.discount_applied ELSE 0 END) as total_discount
                   FROM promocodes p
                   LEFT JOIN promocode_usage pu ON p.id = pu.promocode_id
                   WHERE p.id = ?
                   GROUP BY p.id""",
                (promo_id,)
            )
            row = await cur.fetchone()
            
            if not row:
                return None
            
            code, promo_type, current_uses, max_uses, total_discount = row
            
            # Последние использования
            cur = await db.execute(
                """SELECT user_id, used_at, discount_applied, order_id
                   FROM promocode_usage
                   WHERE promocode_id = ?
                   ORDER BY used_at DESC
                   LIMIT 10""",
                (promo_id,)
            )
            recent_uses = await cur.fetchall()
            
            return {
                'code': code,
                'type': promo_type,
                'current_uses': current_uses or 0,
                'max_uses': max_uses,
                'total_discount': total_discount or 0.0,
                'recent_uses': recent_uses,
            }
            
    except Exception as e:
        logger.error(f"Error getting promocode stats: {e}")
        return None
