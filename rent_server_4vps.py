#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Автоматический заказ VPS на 4VPS.SU с автопланированием ресурсов.
Адаптация логики RUVDS для 4VPS API.
"""

import os
import time
import logging
from typing import Dict, Optional
import asyncio

from dotenv import load_dotenv
from fourpvs_api import FourVPSAPI

# Setup logging
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, '.env'))

FOURVPS_API_TOKEN = os.getenv("FOURVPS_API_TOKEN", "")


# ----------------------------------------------------------------------
# Маппинг тарифов 4VPS на количество конфигов
# ----------------------------------------------------------------------

def auto_plan_preset_4vps(protocol: str, configs_count: int, available_presets: Dict) -> int:
    """
    Автоматический подбор пресета 4VPS на основе количества конфигов
    
    Логика аналогична RUVDS:
    - 1-15 конфигов: минимальный пресет (1 CPU, 1GB RAM)
    - 16-30 конфигов: средний пресет (1 CPU, 2GB RAM)
    - 31-100 конфигов: продвинутый пресет (2 CPU, 4GB RAM)
    - 101-250 конфигов: мощный пресет (4 CPU, 8GB RAM)
    
    Args:
        protocol: Код протокола
        configs_count: Количество конфигураций
        available_presets: Словарь доступных пресетов из getTarifList
    
    Returns:
        preset_id: ID тарифа из доступных для данного DC
    """
    # Определяем требуемые ресурсы (МИНИМАЛЬНЫЕ для экономии)
    if configs_count <= 15:
        target_cpu = 1
        target_ram = 1  # GB
    elif configs_count <= 30:
        target_cpu = 1
        target_ram = 2
    elif configs_count <= 100:
        target_cpu = 2
        target_ram = 4
    else:
        target_cpu = 4
        target_ram = 8
    
    # Ищем САМЫЙ ДЕШЕВЫЙ пресет среди подходящих
    best_preset = None
    best_price = float('inf')
    
    logger.info(f"auto_plan_preset_4vps: Target resources - CPU={target_cpu}, RAM={target_ram}GB, Configs={configs_count}")
    logger.info(f"auto_plan_preset_4vps: Available presets: {list(available_presets.keys())}")
    
    for preset_id_str, preset_info in available_presets.items():
        cpu = preset_info.get('cpu_number', 0)
        ram_mib = preset_info.get('ram_mib', 0)
        ram_gb = ram_mib / 1024.0
        price = preset_info.get('price', 9999999)
        preset_name = preset_info.get('name', preset_id_str)
        
        logger.info(f"  Preset {preset_name} (ID {preset_id_str}): {cpu} CPU, {ram_gb:.1f}GB RAM, {price}₽/month")
        
        # Проверяем, что ресурсы не меньше требуемых
        if cpu >= target_cpu and ram_gb >= target_ram:
            # Выбираем самый дешевый
            if price < best_price:
                best_price = price
                best_preset = int(preset_id_str)
                logger.info(f"    ✓ MATCHES requirements and cheaper than previous ({price} < {best_price}₽)")
    
    # Если не нашли подходящий, берём самый дешевый из всех доступных
    if best_preset is None and available_presets:
        cheapest_preset = None
        cheapest_price = float('inf')
        
        for preset_id_str, preset_info in available_presets.items():
            price = preset_info.get('price', 9999999)
            if price < cheapest_price:
                cheapest_price = price
                cheapest_preset = int(preset_id_str)
        
        best_preset = cheapest_preset
    
    if best_preset is None:
        raise RuntimeError("Не найдены доступные тарифы для выбранного дата-центра")
    
    return best_preset


def get_os_id_4vps(protocol: str, available_os: Dict) -> int:
    """
    Получить ID образа ОС для 4VPS из доступных
    
    Приоритет: Ubuntu 22.04 > Ubuntu 20.04 > Debian 11 > первый доступный
    
    Args:
        protocol: Код протокола
        available_os: Словарь доступных ОС из getImages
    
    Returns:
        os_id: ID образа ОС
    """
    # Приоритетный список ОС
    preferred_os = [
        "ubuntu 22.04",
        "ubuntu 20.04", 
        "debian 11",
        "debian 10",
        "ubuntu"  # любая ubuntu
    ]
    
    # Поиск по приоритету
    for pref in preferred_os:
        for os_id_str, os_name in available_os.items():
            if pref in os_name.lower():
                return int(os_id_str)
    
    # Если не нашли предпочтительную, берём первую доступную Linux-систему
    for os_id_str, os_name in available_os.items():
        os_lower = os_name.lower()
        if any(x in os_lower for x in ['ubuntu', 'debian', 'centos', 'rocky', 'alma']):
            return int(os_id_str)
    
    # В крайнем случае берём первую доступную
    if available_os:
        return int(list(available_os.keys())[0])
    
    raise RuntimeError("Не найдены доступные образы ОС для выбранного дата-центра")


def map_period_to_4vps(period_key: str) -> Optional[int]:
    """
    Конвертировать период из формата бота в формат 4VPS (часы)
    
    4VPS поддерживает:
    - 720 часов = 1 месяц
    - 2160 часов = 3 месяца
    - 4320 часов = 6 месяцев
    - 8640 часов = 12 месяцев
    
    Бот использует: 1w, 1m, 2m, 3m, 6m, 12m
    """
    period_map = {
        "1w": 720,      # 1 неделя → аренда на 1 месяц (минимум)
        "1m": 720,      # 1 месяц → 720 часов
        "2m": 2160,     # 2 месяца → округляем до 3 месяцев
        "3m": 2160,     # 3 месяца → 2160 часов
        "6m": 4320,     # 6 месяцев → 4320 часов
        "12m": 8640     # 12 месяцев → 8640 часов
    }
    return period_map.get(period_key, 720)  # По умолчанию 1 месяц


async def rent_server_for_bot_4vps(
    protocol: str,
    configs_count: int,
    dc_id: int,
    payment_period: str = "1m",
) -> Dict[str, str]:
    """
    Основная функция для бота: арендовать сервер на 4VPS и вернуть credentials.
    
    Аналогична rent_server_for_bot() из rent_server.py (RUVDS)
    
    Args:
        protocol: Код протокола (wg, awg, ovpn, socks5, xray, trojan)
        configs_count: Количество конфигураций
        dc_id: ID дата-центра 4VPS
        payment_period: Период оплаты (1w, 1m, 2m, 3m, 6m, 12m)
    
    Returns:
        dict: {"ip": "...", "login": "...", "password": "...", "server_id": "..."}
    """
    if not FOURVPS_API_TOKEN:
        raise RuntimeError("FOURVPS_API_TOKEN не найден в .env")
    
    # Маппинг протоколов (аналогично RUVDS)
    protocol_map = {
        "wg": "wireguard",
        "awg": "amneziawg",
        "ovpn": "openvpn",
        "socks5": "socks5",
        "xray": "xray_vless",
        "trojan": "trojan_go"
    }
    protocol_code = protocol_map.get(protocol, "wireguard")
    
    # Создаем API клиент
    api = FourVPSAPI(FOURVPS_API_TOKEN)
    
    # Проверяем баланс (опционально, как в RUVDS)
    balance = await api.get_balance()
    if balance is not None and balance < 500:
        raise RuntimeError(f"Недостаточно средств на балансе: {balance}₽ (минимум 500₽)")
    
    # Шаг 1: Получаем список тарифов для всех дата-центров
    all_tariffs = await api.get_tariffs()
    if not all_tariffs:
        raise RuntimeError("Не удалось получить список тарифов")
    
    # Шаг 2: Находим тарифы для нашего дата-центра
    # Ключи в all_tariffs - это ID кластеров (совпадают с ID дата-центров)
    dc_tariff_info = all_tariffs.get(str(dc_id))
    
    if not dc_tariff_info:
        raise RuntimeError(f"Не найдены тарифы для дата-центра {dc_id}")
    
    # Получаем доступные пресеты
    available_presets = dc_tariff_info.get('presets', {})
    if not available_presets:
        raise RuntimeError(f"Нет доступных пресетов для дата-центра {dc_id}")
    
    # Шаг 3: Автоподбор пресета на основе конфигов
    preset_id = auto_plan_preset_4vps(protocol_code, configs_count, available_presets)
    
    # Шаг 4: Получаем список доступных образов ОС для этого тарифа
    available_os = await api.get_images(preset_id, dc_id)
    if not available_os:
        raise RuntimeError(f"Не удалось получить список образов ОС для тарифа {preset_id}")
    
    # Шаг 5: Выбираем образ ОС
    os_id = get_os_id_4vps(protocol, available_os)
    
    # Шаг 6: Конвертация периода
    period_hours = map_period_to_4vps(payment_period)
    
    # Шаг 7: Генерация имени сервера (аналогично RUVDS)
    import random
    import string
    random_suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
    server_name = f"vpn-{protocol}-{random_suffix}"
    
    # Шаг 8: Создание сервера через 4VPS API
    result = await api.buy_server(
        tariff_id=preset_id,
        datacenter_id=dc_id,
        os_template=os_id,
        name=server_name,
        period=period_hours
    )
    
    if not result:
        raise RuntimeError("Провайдер не вернул данные сервера")
    
    server_id = result.get("serverid")
    password = result.get("password")
    
    if not server_id or not password:
        raise RuntimeError(f"Провайдер вернул неполные данные: {result}")
    
    # Шаг 9: Ожидание готовности сервера (аналогично RUVDS wait_for_server_ready)
    await wait_for_4vps_server_ready(api, int(server_id))
    
    # Шаг 10: Получение IP адреса сервера с повторными попытками
    ip_addr = None
    max_retries = 40  # Максимум 40 попыток
    retry_delay = 8  # Задержка между попытками в секундах
    
    for attempt in range(1, max_retries + 1):
        logger.info(f"[4VPS] Попытка {attempt}/{max_retries} получить данные сервера {server_id}...")
        
        server_info = await api.get_server_info(int(server_id))
        
        if server_info and server_info.get('serverInfo'):
            ip_addr = server_info['serverInfo'].get('ipv4')
            
            if ip_addr:
                logger.info(f"[4VPS] IP адрес получен: {ip_addr}")
                break
            else:
                logger.warning(f"[4VPS] IP адрес еще не назначен, ожидание {retry_delay}с...")
        else:
            logger.warning(f"[4VPS] Информация о сервере пока недоступна, ожидание {retry_delay}с...")
        
        if attempt < max_retries:
            await asyncio.sleep(retry_delay)
    
    if not ip_addr:
        raise RuntimeError(f"Не удалось получить IP адрес сервера {server_id} после {max_retries} попыток. Возможно, сервер еще не полностью развернут.")
    
    # Определяем логин (для Linux всегда root)
    login = "root"
    
    return {
        "ip": ip_addr,
        "login": login,
        "password": password,
        "server_id": str(server_id)
    }


async def wait_for_4vps_server_ready(api: FourVPSAPI, server_id: int, timeout: int = 300) -> None:
    """
    Ожидание готовности сервера на 4VPS
    
    Аналогично wait_for_server_ready() из rent_server.py (RUVDS)
    
    Args:
        api: Клиент 4VPS API
        server_id: ID сервера
        timeout: Максимальное время ожидания в секундах
    """
    start_time = time.time()
    last_status = None
    
    while True:
        elapsed = time.time() - start_time
        if elapsed > timeout:
            raise RuntimeError(f"Таймаут ожидания готовности сервера {server_id} (>{timeout}s). Последний статус: {last_status}")
        
        # Получаем информацию о сервере
        server_info = await api.get_server_info(server_id)
        
        if not server_info or not server_info.get('serverInfo'):
            logger.warning(f"[4VPS] Сервер {server_id}: информация пока недоступна, ожидание...")
            await asyncio.sleep(10)
            continue
        
        status = server_info['serverInfo'].get('status')
        
        # Логируем изменение статуса
        if status != last_status:
            logger.info(f"[4VPS] Сервер {server_id}: статус изменился на '{status}'")
            last_status = status
        
        # Проверяем статус (аналогично RUVDS: is_running)
        if status == 'active':
            # Сервер активен и готов к использованию
            logger.info(f"[4VPS] Сервер {server_id} активен, дополнительное ожидание 30с для SSH...")
            # Дополнительно ждем 30 секунд для полной инициализации SSH
            await asyncio.sleep(30)
            logger.info(f"[4VPS] Сервер {server_id} готов к использованию")
            return
        
        # Ждем 10 секунд перед следующей проверкой
        await asyncio.sleep(10)


async def delete_server_4vps(server_id: str) -> bool:
    """
    Удаляет VPS сервер на 4VPS по ID.
    
    Аналогично delete_server() из rent_server.py (RUVDS)
    
    Args:
        server_id: ID сервера в 4VPS
        
    Returns:
        True если удаление успешно, False если ошибка
    """
    if not FOURVPS_API_TOKEN:
        print(f"[Удаление 4VPS] FOURVPS_API_TOKEN не найден, пропускаю удаление сервера {server_id}")
        return False
    
    try:
        api = FourVPSAPI(FOURVPS_API_TOKEN)
        success = await api.delete_server(int(server_id))
        
        if success:
            print(f"[Удаление 4VPS] Сервер {server_id} успешно удален")
            return True
        else:
            print(f"[Удаление 4VPS] Не удалось удалить сервер {server_id}")
            return False
            
    except Exception as e:
        print(f"[Удаление 4VPS] Ошибка при удалении сервера {server_id}: {e}")
        return False


# Синхронные обертки для совместимости с существующим кодом
def rent_server_for_bot_4vps_sync(
    protocol: str,
    configs_count: int,
    dc_id: int,
    payment_period: str = "1m",
) -> Dict[str, str]:
    """Синхронная версия rent_server_for_bot_4vps для вызова из sync-контекста"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(
            rent_server_for_bot_4vps(protocol, configs_count, dc_id, payment_period)
        )
    finally:
        loop.close()


def delete_server_4vps_sync(server_id: str) -> bool:
    """Синхронная версия delete_server_4vps для вызова из sync-контекста"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(delete_server_4vps(server_id))
    finally:
        loop.close()
