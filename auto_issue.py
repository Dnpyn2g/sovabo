"""
Модуль автоматической выдачи VPN конфигураций через API провайдеров
"""

import asyncio
import json
import logging
import os
from typing import Dict, List, Optional
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from dotenv import load_dotenv

# Импорт системы ценообразования
from pricing_config import (
    VOLUME_TARIFFS, 
    TERM_FACTORS, 
    get_tariff_by_configs, 
    calculate_price,
    get_all_term_prices
)

# Импорт API
from fourpvs_api import FourVPSAPI, get_country_name, get_flag_emoji

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOCATIONS_PATH = os.path.join(BASE_DIR, 'locations.json')

load_dotenv()
FOURVPS_API_TOKEN = os.getenv("FOURVPS_API_TOKEN", "")

# ========== КЭШ ЛОКАЦИЙ ==========
_locations_cache: Optional[Dict[str, List[Dict]]] = None
_cache_timestamp: Optional[datetime] = None
_cache_lock = asyncio.Lock()
CACHE_TTL_MINUTES = 30  # Обновлять кэш каждые 30 минут

# ========== КЭШ ДОСТУПНОСТИ ==========
_availability_cache: Dict[int, bool] = {}  # {dc_id: is_available}
_availability_timestamp: Optional[datetime] = None
_availability_lock = asyncio.Lock()
AVAILABILITY_CHECK_MINUTES = 30  # Проверять доступность каждые 15 минут


async def check_4vps_dc_availability(api: FourVPSAPI, dc_id: int) -> bool:
    """
    Проверить доступность дата-центра (есть ли доступные серверы)
    
    Args:
        api: Клиент API
        dc_id: ID дата-центра
    
    Returns:
        True если есть хотя бы один доступный пресет, False если все sold out
    """
    try:
        # Получаем тарифы для этого DC
        all_tariffs = await api.get_tariffs()
        dc_tariffs = all_tariffs.get(str(dc_id))
        
        if not dc_tariffs:
            logger.warning(f"No tariffs found for DC {dc_id}")
            return False
        
        presets = dc_tariffs.get('presets', {})
        if not presets:
            logger.warning(f"No presets found for DC {dc_id}")
            return False
        
        # Проверяем только первый пресет для скорости
        # Если API возвращает образы - DC доступен
        first_preset_id = list(presets.keys())[0]
        images = await api.get_images(int(first_preset_id), dc_id)
        
        # Если есть образы - значит DC доступен
        return bool(images)
        
    except Exception as e:
        logger.error(f"Error checking availability for DC {dc_id}: {e}")
        # В случае ошибки считаем доступным (не скрываем локацию)
        return True


async def update_4vps_availability() -> Dict[int, bool]:
    """
    Обновить статусы доступности всех дата-центров
    
    Returns:
        Словарь {dc_id: is_available}
    """
    if not FOURVPS_API_TOKEN:
        return {}
    
    availability = {}
    
    try:
        api = FourVPSAPI(FOURVPS_API_TOKEN)
        
        # Получаем список всех дата-центров
        datacenters = await api.get_datacenters()
        
        logger.info(f"Checking availability for {len(datacenters)} datacenters...")
        
        # Проверяем доступность каждого DC
        for dc in datacenters:
            dc_id = dc['id']
            dc_name = dc.get('name', f"DC {dc_id}")
            
            is_available = await check_4vps_dc_availability(api, dc_id)
            availability[dc_id] = is_available
            
            status = "✅ Available" if is_available else "❌ Sold out"
            logger.info(f"  DC {dc_id} ({dc_name}): {status}")
        
        logger.info(f"Availability check complete: {sum(availability.values())}/{len(availability)} DCs available")
        
    except Exception as e:
        logger.error(f"Error updating availability: {e}")
    
    return availability


async def get_4vps_availability() -> Dict[int, bool]:
    """
    Получить кэшированные статусы доступности дата-центров
    
    Returns:
        Словарь {dc_id: is_available}
    """
    global _availability_cache, _availability_timestamp
    
    async with _availability_lock:
        now = datetime.now()
        
        # Проверяем, нужно ли обновить кэш
        if _availability_timestamp is None or not _availability_cache:
            # Первая загрузка
            logger.info("First availability check - loading...")
            _availability_cache = await update_4vps_availability()
            _availability_timestamp = now
        else:
            # Проверяем возраст кэша
            cache_age = now - _availability_timestamp
            if cache_age >= timedelta(minutes=AVAILABILITY_CHECK_MINUTES):
                logger.info(f"Availability cache expired ({cache_age.total_seconds():.0f}s old) - refreshing...")
                _availability_cache = await update_4vps_availability()
                _availability_timestamp = now
        
        return _availability_cache.copy()


async def refresh_availability_cache():
    """
    Принудительно обновить кэш доступности (для периодической задачи)
    """
    global _availability_cache, _availability_timestamp
    
    async with _availability_lock:
        logger.info("Forcing availability cache refresh...")
        _availability_cache = await update_4vps_availability()
        _availability_timestamp = datetime.now()
        
        # Log summary
        total = len(_availability_cache)
        available_count = sum(1 for v in _availability_cache.values() if v)
        sold_out_count = total - available_count
        logger.info(f"Availability check complete: {available_count}/{total} DCs available, {sold_out_count} sold out")
        
        # Log sold out DCs
        if sold_out_count > 0:
            sold_out_ids = [dc_id for dc_id, is_avail in _availability_cache.items() if not is_avail]
            logger.warning(f"Sold out DCs: {sold_out_ids}")


def load_locations_data() -> Dict:
    """Загрузить данные локаций и цен из JSON"""
    try:
        with open(LOCATIONS_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Ошибка загрузки locations.json: {e}")
        return {"locations": [], "tariffs": [], "pricing": {}}


async def load_all_locations(protocol: str) -> Dict[str, List[Dict]]:
    """
    Загрузить все доступные локации от провайдеров (с кэшированием)
    
    Returns:
        Словарь {страна: [список городов]}
    """
    global _locations_cache, _cache_timestamp
    
    # Проверяем актуальность кэша
    async with _cache_lock:
        now = datetime.now()
        if _locations_cache is not None and _cache_timestamp is not None:
            cache_age = now - _cache_timestamp
            if cache_age < timedelta(minutes=CACHE_TTL_MINUTES):
                logger.debug(f"Using cached locations (age: {cache_age.seconds}s)")
                return _locations_cache
        
        # Кэш устарел или не существует - загружаем заново
        logger.info("Loading locations (cache expired or empty)")
        countries: Dict[str, List[Dict]] = {}
        
        # 1. Загрузка локаций из locations.json
        data = load_locations_data()
        ruvds_locations = data.get('locations', [])
        
        for loc in ruvds_locations:
            country = loc.get('country', 'Другие')
            if country not in countries:
                countries[country] = []
            loc['provider'] = 'ruvds'  # Помечаем провайдера
            countries[country].append(loc)
        
        # 2. Загрузка локаций из 4VPS (через API)
        if FOURVPS_API_TOKEN:
            try:
                api = FourVPSAPI(FOURVPS_API_TOKEN)
                datacenters = await api.get_datacenters()
                
                # Получаем статусы доступности (если кэш уже существует)
                # Не проверяем при первой загрузке для скорости
                availability = {}
                if _availability_cache:
                    availability = _availability_cache.copy()
                    logger.debug(f"Using cached availability data for filtering")
                else:
                    logger.debug(f"No availability cache yet - showing all DCs")
                
                # Дедупликация: используем только дата-центры с числовым ID
                seen = set()
                filtered_count = 0
                
                for dc in datacenters:
                    dc_name = dc.get('name', '')
                    flag_code = dc.get('flag', '')
                    dc_id = dc.get('id')
                    
                    # Создаем ключ дедупликации
                    dedup_key = (flag_code, dc_name)
                    
                    # Пропускаем дубликаты и дата-центры без числового ID
                    if dedup_key in seen or not isinstance(dc_id, int):
                        continue
                    
                    # ✨ ФИЛЬТРАЦИЯ: Пропускаем недоступные дата-центры (только если есть данные)
                    if availability and not availability.get(dc_id, True):
                        logger.debug(f"Filtering out unavailable DC: {dc_name} (ID {dc_id})")
                        filtered_count += 1
                        continue
                    
                    seen.add(dedup_key)
                    country = get_country_name(flag_code)
                    
                    if country not in countries:
                        countries[country] = []
                    
                    # Добавляем как локацию
                    countries[country].append({
                        'key': f"4vps_{dc_id}",
                        'country': country,
                        'city': dc_name,
                        'flag': get_flag_emoji(flag_code),
                        'provider': '4vps',
                        'dc_id': dc_id,
                        'dc_info': dc
                    })
                
                logger.info(f"Loaded {len(seen)} unique 4VPS locations ({filtered_count} filtered as unavailable)")
            except Exception as e:
                logger.error(f"Error loading 4VPS locations: {e}")
        
        # Сохраняем в кэш
        _locations_cache = countries
        _cache_timestamp = now
        logger.info(f"Locations cached at {now.strftime('%H:%M:%S')}")
        
        return countries


async def refresh_locations_cache() -> None:
    """
    Принудительно обновить кэш локаций (для периодической задачи)
    """
    global _locations_cache, _cache_timestamp
    
    logger.info("Refreshing locations cache...")
    async with _cache_lock:
        _cache_timestamp = None  # Сбрасываем timestamp для принудительного обновления
    
    await load_all_locations("wg")  # Протокол не важен для загрузки локаций
    logger.info("Locations cache refreshed successfully")


async def show_auto_issue_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, protocol: str):
    """
    Показать меню автовыдачи: выбор страны
    
    Args:
        update: Telegram Update объект
        context: Context объекта
        protocol: Выбранный протокол (wg, awg, ovpn, socks5, xray, trojan)
    """
    logger.info(f"show_auto_issue_menu called with protocol: {protocol}")
    query = update.callback_query
    await query.answer()
    
    protocol_names = {
        'wg': 'WireGuard',
        'awg': 'AmneziaWG',
        'ovpn': 'OpenVPN',
        'socks5': 'SOCKS5',
        'xray': 'Xray VLESS',
        'trojan': 'Trojan-Go'
    }
    
    protocol_label = protocol_names.get(protocol, protocol.upper())
    
    # Загружаем локации из обоих источников
    countries = await load_all_locations(protocol)
    
    if not countries:
        text = (
            f"🚀 <b>Автоматическая выдача</b>\n\n"
            f"Протокол: <b>{protocol_label}</b>\n\n"
            f"⚠️ Ошибка загрузки списка локаций.\n"
            f"Обратитесь к администратору."
        )
        keyboard = [
            [InlineKeyboardButton("⬅️ Назад", callback_data=f"wg_pickproto:{protocol}")]
        ]
        await query.edit_message_text(text=text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
        return
    
    text = (
        f"🚀 <b>Автоматическая выдача</b>\n\n"
        f"Протокол: <b>{protocol_label}</b>\n\n"
        f"<i>ℹ️ Сервер будет автоматически арендован и настроен за 3-5 минут.</i>\n\n"
        f"Выберите страну:"
    )
    
    # Создаем кнопки по странам плиточкой (2 колонки)
    buttons: List[List[InlineKeyboardButton]] = []
    row = []
    
    for country, locs in sorted(countries.items()):
        # Берём флаг первого города в стране
        flag = locs[0].get('flag', '🌍')
        btn_text = f"{flag} {country}"
        row.append(InlineKeyboardButton(btn_text, callback_data=f"auto_country:{protocol}|{country}"))
        
        # Добавляем строку из 2 кнопок
        if len(row) == 2:
            buttons.append(row)
            row = []
    
    # Добавляем последнюю неполную строку, если есть
    if row:
        buttons.append(row)
    
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"wg_pickproto:{protocol}")])
    
    try:
        await query.edit_message_text(
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    except Exception as e:
        # Игнорируем ошибку "Message is not modified"
        if "not modified" not in str(e).lower():
            raise


async def show_country_cities(update: Update, context: ContextTypes.DEFAULT_TYPE, protocol: str, country: str):
    """
    Показать города выбранной страны
    
    Args:
        update: Telegram Update объект
        context: Context объекта
        protocol: Выбранный протокол
        country: Выбранная страна
    """
    query = update.callback_query
    await query.answer()
    
    protocol_names = {
        'wg': 'WireGuard',
        'awg': 'AmneziaWG',
        'ovpn': 'OpenVPN',
        'socks5': 'SOCKS5',
        'xray': 'Xray VLESS',
        'trojan': 'Trojan-Go'
    }
    
    protocol_label = protocol_names.get(protocol, protocol.upper())
    
    # Загружаем все локации
    countries = await load_all_locations(protocol)
    country_locations = countries.get(country, [])
    
    if not country_locations:
        text = (
            f"🚀 <b>Автоматическая выдача</b>\n\n"
            f"Протокол: <b>{protocol_label}</b>\n\n"
            f"⚠️ Города в стране {country} не найдены."
        )
        keyboard = [
            [InlineKeyboardButton("⬅️ Назад", callback_data=f"wg_mode:auto|{protocol}")]
        ]
        await query.edit_message_text(text=text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
        return
    
    # Группируем по названию города
    city_groups = {}
    for loc in country_locations:
        city = loc.get('city', 'Город')
        if city not in city_groups:
            city_groups[city] = []
        city_groups[city].append(loc)
    
    # 🎯 ОПТИМИЗАЦИЯ: Если в стране только один город - сразу переходим к выбору тарифа
    if len(city_groups) == 1:
        city = list(city_groups.keys())[0]
        primary_loc = city_groups[city][0]
        # Сразу показываем выбор тарифа
        await show_tariff_selection(update, context, protocol, primary_loc['key'])
        return
    
    flag = country_locations[0].get('flag', '🌍')
    
    text = (
        f"🚀 <b>Автоматическая выдача</b>\n\n"
        f"Протокол: <b>{protocol_label}</b>\n"
        f"Страна: <b>{flag} {country}</b>\n\n"
        f"Выберите дата-центр:"
    )
    
    # Создаем кнопки по городам плиточкой (2 в ряд)
    buttons: List[List[InlineKeyboardButton]] = []
    row = []
    
    for city in sorted(city_groups.keys()):
        locs = city_groups[city]
        # Берем первую локацию (если несколько провайдеров, пользователь не заметит разницы)
        primary_loc = locs[0]
        
        btn_text = f"📍 {city}"
        
        row.append(InlineKeyboardButton(btn_text, callback_data=f"auto_loc:{protocol}|{primary_loc['key']}"))
        
        # Добавляем строку из 2 кнопок
        if len(row) == 2:
            buttons.append(row)
            row = []
    
    # Добавляем последнюю неполную строку, если есть
    if row:
        buttons.append(row)
    
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"wg_mode:auto|{protocol}")])
    
    await query.edit_message_text(
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons)
    )



async def show_tariff_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, protocol: str, location_key: str):
    """Показать выбор тарифа (количество конфигов) с ценами за месяц"""
    query = update.callback_query
    await query.answer()
    
    protocol_names = {
        'wg': 'WireGuard',
        'awg': 'AmneziaWG',
        'ovpn': 'OpenVPN',
        'socks5': 'SOCKS5',
        'xray': 'Xray VLESS',
        'trojan': 'Trojan-Go'
    }
    protocol_label = protocol_names.get(protocol, protocol.upper())
    
    # Проверяем, это локация через API
    if location_key.startswith('4vps_'):
        # Локация через API - загружаем информацию
        try:
            api = FourVPSAPI(FOURVPS_API_TOKEN)
            datacenters = await api.get_datacenters()
            dc_id = int(location_key.replace('4vps_', ''))
            location = next((dc for dc in datacenters if dc['id'] == dc_id), None)
            
            if not location:
                await query.edit_message_text("Ошибка: дата-центр не найден")
                return
            
            location_label = f"{get_flag_emoji(location.get('flag', ''))} {location.get('name', 'Дата-центр')}"
            # Сохраняем информацию в context для дальнейшего использования
            context.user_data['selected_location'] = {
                'provider': '4vps',
                'dc_id': dc_id,
                'name': location.get('name'),
                'flag': location.get('flag')
            }
        except Exception as e:
            logger.error(f"Error loading datacenter: {e}")
            await query.edit_message_text("Ошибка: не удалось загрузить информацию о дата-центре")
            return
    else:
        # Локация из JSON - загружаем из файла
        data = load_locations_data()
        locations = data.get('locations', [])
        location = next((loc for loc in locations if loc['key'] == location_key), None)
        
        if not location:
            await query.edit_message_text("Ошибка: локация не найдена")
            return
        
        location_label = f"{location.get('flag', '🌍')} {location.get('city', 'Город')}"
        # Сохраняем информацию в context
        context.user_data['selected_location'] = {
            'provider': 'ruvds',
            'key': location_key,
            'city': location.get('city'),
            'country': location.get('country'),
            'flag': location.get('flag')
        }
    
    text = (
        f"🚀 <b>Автоматическая выдача</b>\n\n"
        f"Протокол: <b>{protocol_label}</b>\n"
        f"Локация: <b>{location_label}</b>\n\n"
        f"<b>Выберите количество конфигов:</b>\n"
        f"<i>Цены указаны за 1 месяц. На длительных сроках действуют скидки.</i>\n\n"
    )
    
    buttons: List[List[InlineKeyboardButton]] = []
    
    # Показываем тарифы из VOLUME_TARIFFS с ценами
    for tariff in VOLUME_TARIFFS:
        label = tariff['label']
        price_month = tariff['price_month']
        # Используем средний конфиг для ID
        mid_configs = (tariff['min'] + tariff['max']) // 2
        tier_id = f"{tariff['min']}-{tariff['max']}"
        
        btn_text = f"{label} → {price_month:.0f} $/мес"
        buttons.append([
            InlineKeyboardButton(btn_text, callback_data=f"auto_tariff:{protocol}|{location_key}|{tier_id}|{mid_configs}")
        ])
    
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"wg_mode:auto|{protocol}")])
    
    await query.edit_message_text(
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def show_period_selection(update: Update, context: ContextTypes.DEFAULT_TYPE, protocol: str, location_key: str, tier_id: str, configs_count: int):
    """Показать выбор периода аренды с итоговыми ценами"""
    query = update.callback_query
    await query.answer()
    
    protocol_names = {
        'wg': 'WireGuard',
        'awg': 'AmneziaWG',
        'ovpn': 'OpenVPN',
        'socks5': 'SOCKS5',
        'xray': 'Xray VLESS',
        'trojan': 'Trojan-Go'
    }
    protocol_label = protocol_names.get(protocol, protocol.upper())
    
    # Получаем информацию о локации из context (было сохранено в show_configs_count_selection)
    selected_location = context.user_data.get('selected_location')
    if not selected_location:
        await query.edit_message_text("Ошибка: данные не найдены")
        return
    
    provider = selected_location.get('provider', 'ruvds')
    location_label = f"{selected_location.get('flag', '🌍')} {selected_location.get('city', 'Город')} ({provider.upper()})"
    
    # Получаем информацию о тарифе
    tariff = get_tariff_by_configs(configs_count)
    base_price = tariff['price_month']
    
    text = (
        f"🚀 <b>Автоматическая выдача</b>\n\n"
        f"Протокол: <b>{protocol_label}</b>\n"
        f"Локация: <b>{location_label}</b>\n"
        f"Тариф: <b>{tariff['label']}</b>\n"
        f"Базовая цена: <b>{base_price:.2f} $ / месяц</b>\n\n"
        f"<b>Выберите срок аренды:</b>\n"
        f"<i>На длительных сроках действует скидка.\n"
        f"Цены ниже уже с учётом скидки 👇</i>"
    )
    
    buttons: List[List[InlineKeyboardButton]] = []
    
    # Получаем все цены для всех сроков
    all_prices = get_all_term_prices(configs_count)
    
    for price_info in all_prices:
        term_key = price_info['term_key']
        term_label = price_info['term_label']
        total_price = price_info['total_price']
        discount = price_info['discount']
        
        btn_text = f"{term_label} — {total_price:.2f} $"
        if discount > 0:
            btn_text += f" (−{discount}%)"
        
        buttons.append([
            InlineKeyboardButton(
                btn_text,
                callback_data=f"auto_period:{protocol}|{location_key}|{tier_id}|{term_key}|{configs_count}"
            )
        ])
    
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"auto_loc:{protocol}|{location_key}")])
    
    await query.edit_message_text(
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons)
    )

