#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Автоматический подбор и заказ VPS на RUVDS с выбором ДЦ и автопланированием ресурсов.
Интегрировано с ботом для автовыдачи.
"""

import argparse
import base64
import json
import os
import sys
import time
from typing import Dict, List, Tuple, Optional, Literal

import requests
from dotenv import load_dotenv

# Загрузка переменных окружения
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.abspath(os.path.join(BASE_DIR, os.pardir))
load_dotenv(os.path.join(ROOT_DIR, '.env'))
load_dotenv(os.path.join(BASE_DIR, '.env'))

API_BASE_URL = "https://api.ruvds.com/v2"
API_TOKEN = os.getenv("RUVDS_API_TOKEN", "")

# ----------------------------------------------------------------------
# Локации / дата-центры
# ----------------------------------------------------------------------

LOCATION_MAP: Dict[str, Dict[str, object]] = {
    "ru_moscow_main": {
        "label": "Россия: Москва",
        "search": ["москва", "moscow", "m9", "rucloud"],
    },
    "ru_moscow_korolev": {
        "label": "Россия: Москва, Королёв",
        "search": ["бункер", "bunker", "королев", "korolev"],
    },
    "de_zurich": {
        "label": "Швейцария: Цюрих",
        "search": ["цюрих", "zurich", "zur1"],
    },
    "de_frankfurt": {
        "label": "Германия: Франкфурт",
        "search": ["франкфурт", "frankfurt", "fra", "telehouse"],
    },
    "uk_london": {
        "label": "UK: Лондон",
        "search": ["лондон", "london", "lon", "ld8"],
    },
    "ru_spb": {
        "label": "Россия: Санкт-Петербург",
        "search": ["петербург", "saint petersburg", "st. petersburg", "spb", "linxdatacenter"],
    },
    "ru_kazan": {
        "label": "Россия: Казань",
        "search": ["казань", "kazan", "itpark"],
    },
    "ru_yekaterinburg": {
        "label": "Россия: Екатеринбург",
        "search": ["екатеринбург", "yekaterinburg", "ekb"],
    },
    "ru_novosibirsk": {
        "label": "Россия: Новосибирск",
        "search": ["новосибирск", "novosibirsk", "sibtelco"],
    },
    "nl_amsterdam": {
        "label": "Амстердам",
        "search": ["амстердам", "amsterdam", "ams", "ams9"],
    },
    "ru_moscow_ostankino": {
        "label": "Россия: Москва, Останкино",
        "search": ["останкино", "ostankino"],
    },
    "kz_almaty": {
        "label": "Казахстан: Алматы",
        "search": ["алматы", "almaty", "ttc"],
    },
    "kz_astana": {
        "label": "Казахстан: Астана",
        "search": ["астана", "astana", "ttc"],
    },
    "ru_vladivostok": {
        "label": "Россия: Владивосток",
        "search": ["владивосток", "vladivostok", "порттелеком", "porttelecom"],
    },
    "tr_izmir": {
        "label": "Турция: Измир",
        "search": ["измир", "izmir", "netdirekt"],
    },
    "ru_krasnodar": {
        "label": "Россия: Краснодар",
        "search": ["краснодар", "krasnodar", "телемакс", "telemax"],
    },
    "ru_omsk": {
        "label": "Россия: Омск",
        "search": ["омск", "omsk", "смартком", "smartcom"],
    },
    "ru_murmansk": {
        "label": "Россия: Мурманск",
        "search": ["мурманск", "murmansk", "арктический", "arctic"],
    },
    "ge_yerevan": {
        "label": "Армения: Ереван",
        "search": ["ереван", "yerevan", "ovio"],
    },
    "ru_ufa": {
        "label": "Россия: Уфа",
        "search": ["уфа", "ufa", "уфанет", "ufanet"],
    },
}

ProtocolCode = Literal["wireguard", "amneziawg", "openvpn", "socks5", "xray_vless", "trojan_go"]

PROTOCOL_CLASS: Dict[ProtocolCode, str] = {
    "wireguard":  "vpn_light",
    "amneziawg":  "vpn_light",
    "openvpn":    "vpn_heavy",
    "socks5":     "proxy",
    "xray_vless": "vpn_heavy",
    "trojan_go":  "vpn_heavy",
}

TARIFF_RANGES = {
    "TIER_1": (1, 15),
    "TIER_2": (16, 30),
    "TIER_3": (31, 100),
    "TIER_4": (101, 250),
}


def pick_tier_by_configs(configs_count: int) -> str:
    for code, (min_c, max_c) in TARIFF_RANGES.items():
        if min_c <= configs_count <= max_c:
            return code
    raise ValueError(f"Нельзя подобрать уровень по количеству конфигов: {configs_count}")


def auto_plan_resources(protocol: ProtocolCode, configs_count: int) -> Tuple[int, float, int]:
    """Автоматический подбор ресурсов сервера."""
    tier = pick_tier_by_configs(configs_count)
    pclass = PROTOCOL_CLASS[protocol]

    cpu = 1
    ram = 1.0
    drive = 20

    if pclass == "proxy":
        if tier == "TIER_1":
            cpu, ram, drive = 1, 1.0, 20
        elif tier == "TIER_2":
            cpu, ram, drive = 1, 2.0, 20
        elif tier == "TIER_3":
            cpu, ram, drive = 2, 3.0, 25
        elif tier == "TIER_4":
            cpu, ram, drive = 3, 4.0, 30

    elif pclass == "vpn_light":
        if tier == "TIER_1":
            cpu, ram, drive = 1, 1.0, 20
        elif tier == "TIER_2":
            cpu, ram, drive = 2, 2.0, 20
        elif tier == "TIER_3":
            cpu, ram, drive = 3, 4.0, 25
        elif tier == "TIER_4":
            cpu, ram, drive = 4, 6.0, 30

    elif pclass == "vpn_heavy":
        if tier == "TIER_1":
            cpu, ram, drive = 1, 2.0, 20
        elif tier == "TIER_2":
            cpu, ram, drive = 2, 3.0, 25
        elif tier == "TIER_3":
            cpu, ram, drive = 4, 6.0, 30
        elif tier == "TIER_4":
            cpu, ram, drive = 6, 8.0, 40

    if ram < 1.0:
        ram = 1.0
    if drive < 20:
        drive = 20

    return cpu, ram, drive


def get_headers() -> Dict[str, str]:
    if not API_TOKEN:
        raise RuntimeError("RUVDS_API_TOKEN не задан в .env файле")
    return {
        "Authorization": f"Bearer {API_TOKEN}",
        "Accept": "application/json",
    }


def fetch_datacenters(headers: Dict[str, str]) -> List[Dict[str, object]]:
    url = f"{API_BASE_URL}/datacenters"
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"/datacenters {resp.status_code}: {resp.text}")
    data = resp.json()
    if isinstance(data, dict) and "datacenters" in data:
        dcs = data["datacenters"]
    else:
        dcs = data
    if not isinstance(dcs, list):
        raise RuntimeError(f"Непонятный формат ответа /datacenters")
    return dcs


def select_datacenter_by_location(
    datacenters: List[Dict[str, object]],
    location_key: str,
) -> Dict[str, object]:
    info = LOCATION_MAP.get(location_key)
    if not info:
        raise RuntimeError(f"Неизвестная локация: {location_key}")

    search_terms = [str(s).lower() for s in info.get("search", [])]
    if not search_terms:
        raise RuntimeError(f"Для локации {location_key} не заданы search-термы.")

    for dc in datacenters:
        name = str(dc.get("name", "")).lower()
        if any(term in name for term in search_terms):
            return dc

    available = ", ".join(str(dc.get("name", "?")) for dc in datacenters)
    raise RuntimeError(
        f"Для локации '{info['label']}' не найден подходящий дата-центр.\n"
        f"Доступные ДЦ: {available}"
    )


def fetch_tariffs(headers: Dict[str, str]) -> Dict[str, List[Dict[str, object]]]:
    url = f"{API_BASE_URL}/tariffs"
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"/tariffs {resp.status_code}: {resp.text}")
    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"Непонятный формат /tariffs")
    return data


def fetch_os_list(headers: Dict[str, str]) -> List[Dict[str, object]]:
    url = f"{API_BASE_URL}/os"
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"/os {resp.status_code}: {resp.text}")
    data = resp.json()
    if isinstance(data, dict) and "os" in data:
        os_list = data["os"]
    else:
        os_list = data
    if not isinstance(os_list, list):
        raise RuntimeError(f"Непонятный формат /os")
    return os_list


def find_os_id(os_list: List[Dict[str, object]], name_part: str) -> int:
    name_part = name_part.lower()
    for os_item in os_list:
        name = str(os_item.get("name", "")).lower()
        if name_part in name:
            return int(os_item["id"])
    raise RuntimeError(f"ОС с именем '{name_part}' не найдена.")


def compute_cheapest_configuration(
    dc: Dict[str, object],
    tariffs: Dict[str, List[Dict[str, object]]],
    cpu: int,
    ram_gb: float,
    drive_gb: int,
    ip: int = 1,
) -> Tuple[int, int, float]:
    vps_tariffs = {int(t["id"]): t for t in tariffs.get("vps", []) if "id" in t}
    drive_tariffs = {int(t["id"]): t for t in tariffs.get("drive", []) if "id" in t}

    available_vps = [int(i) for i in dc.get("vps_tariffs", [])]
    available_drive = [int(i) for i in dc.get("drive_tariffs", [])]

    if not available_vps:
        raise RuntimeError("В дата-центре нет доступных VPS-тарифов.")
    if not available_drive:
        raise RuntimeError("В дата-центре нет доступных тарифов дисков.")

    best: Optional[Tuple[int, int, float]] = None

    for vps_id in available_vps:
        vps = vps_tariffs.get(vps_id)
        if not vps or not vps.get("is_active", True):
            continue

        price_cpu = float(vps.get("cpu_price") or vps.get("price_cpu") or 0.0)
        price_ram = float(vps.get("ram_price") or vps.get("price_ram") or 0.0)
        base_price = float(vps.get("price") or 0.0)

        vps_cost = base_price + cpu * price_cpu + ram_gb * price_ram

        for drv_id in available_drive:
            drv = drive_tariffs.get(drv_id)
            if not drv or not drv.get("is_active", True):
                continue

            price_gb = float(drv.get("price") or drv.get("hdd_price") or drv.get("price_gb") or 0.0)
            drive_cost = drive_gb * price_gb
            total = vps_cost + drive_cost

            if best is None or total < best[2]:
                best = (vps_id, drv_id, total)

    if best is None:
        for vps_id in available_vps:
            vps = vps_tariffs.get(vps_id)
            if vps and vps.get("is_active", True):
                for drv_id in available_drive:
                    drv = drive_tariffs.get(drv_id)
                    if drv and drv.get("is_active", True):
                        return vps_id, drv_id, 0.0
        raise RuntimeError("Не удалось подобрать комбинацию тарифов.")

    return best


def create_server(
    headers: Dict[str, str],
    datacenter_id: int,
    vps_tariff_id: int,
    drive_tariff_id: int,
    os_id: int,
    payment_period: int,
    cpu: int,
    ram_gb: float,
    drive_gb: int,
    ip: int,
    computer_name: str,
    user_comment: str,
    get_price_only: bool = True,
) -> Dict[str, object]:
    url = f"{API_BASE_URL}/servers"
    params = {"get_price_only": str(get_price_only).lower()}
    payload = {
        "datacenter": datacenter_id,
        "tariff_id": vps_tariff_id,
        "os_id": os_id,
        "payment_period": payment_period,
        "cpu": cpu,
        "ram": ram_gb,
        "drive": drive_gb,
        "drive_tariff_id": drive_tariff_id,
        "ip": ip,
        "computer_name": computer_name,
        "user_comment": user_comment,
    }
    resp = requests.post(url, headers=headers, params=params, json=payload, timeout=30)
    if resp.status_code not in (200, 202):
        raise RuntimeError(f"/servers {resp.status_code}: {resp.text}")
    return resp.json()


def wait_for_server_ready(
    headers: Dict[str, str],
    virtual_server_id: int,
    poll_interval: int = 15,
    timeout: int = 1800,
) -> Dict[str, object]:
    url = f"{API_BASE_URL}/servers/{virtual_server_id}"
    deadline = time.time() + timeout
    last_status = None
    last_progress = None

    while True:
        if time.time() > deadline:
            raise RuntimeError(f"Сервер слишком долго не переходит в статус active. Последний статус: {last_status}, прогресс: {last_progress}%")

        resp = requests.get(url, headers=headers, timeout=30)

        if resp.status_code == 404:
            raise RuntimeError(
                "Сервер в выбранном дата-центре недоступен. "
                "Выберите другой город/дата-центр."
            )

        if resp.status_code != 200:
            raise RuntimeError(f"/servers/{virtual_server_id} {resp.status_code}: {resp.text}")

        data = resp.json()
        status = data.get("status")
        progress = data.get("create_progress")

        # Логируем изменения статуса или прогресса
        if status != last_status or progress != last_progress:
            print(f"[RUVDS] Сервер {virtual_server_id}: статус='{status}', прогресс={progress}%")
            last_status = status
            last_progress = progress

        if status == "active" and (progress is None or progress >= 100):
            print(f"[RUVDS] Сервер {virtual_server_id} готов к использованию")
            return data

        if status in ("notpaid", "blocked", "deleted"):
            raise RuntimeError(f"Сервер перешёл в проблемный статус: {status}")

        time.sleep(poll_interval)


def fetch_start_password(
    headers: Dict[str, str],
    virtual_server_id: int,
    decode: bool = True,
) -> Dict[str, Optional[str]]:
    url = f"{API_BASE_URL}/servers/{virtual_server_id}/start_password"
    params = {"response_format": "base64"}
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"/servers/{virtual_server_id}/start_password {resp.status_code}: {resp.text}")

    data = resp.json()
    login = data.get("login")
    login_type = data.get("login_type")
    password_b64 = data.get("password")

    password_plain: Optional[str] = None
    if decode and password_b64:
        try:
            password_plain = base64.b64decode(password_b64).decode("utf-8", errors="replace")
        except Exception:
            password_plain = None

    return {
        "login": login,
        "login_type": login_type,
        "password_b64": password_b64,
        "password": password_plain,
    }


def fetch_server_ip(headers: Dict[str, str], virtual_server_id: int) -> Optional[str]:
    url = f"{API_BASE_URL}/servers/{virtual_server_id}/networks"
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"/servers/{virtual_server_id}/networks {resp.status_code}: {resp.text}")

    data = resp.json()
    v4_list = data.get("v4") or []
    if isinstance(v4_list, list) and v4_list:
        return v4_list[0].get("ip_address")
    return None


def rent_server_for_bot(
    protocol: str,
    configs_count: int,
    location_key: str,
    payment_period: int = 2,
) -> Dict[str, str]:
    """
    Основная функция для бота: арендовать сервер и вернуть credentials.
    
    Returns:
        dict: {"ip": "...", "login": "...", "password": "...", "server_id": "..."}
    """
    # Маппинг протоколов бота на внутренние коды
    protocol_map = {
        "wg": "wireguard",
        "awg": "amneziawg",
        "ovpn": "openvpn",
        "socks5": "socks5",
        "xray": "xray_vless",
        "trojan": "trojan_go"
    }
    
    protocol_code = protocol_map.get(protocol, "wireguard")
    
    # Автоподбор ресурсов
    cpu, ram, drive = auto_plan_resources(protocol_code, configs_count)  # type: ignore
    
    # Ограничения API
    if ram < 1.0:
        ram = 1.0
    if drive < 20:
        drive = 20
    
    headers = get_headers()
    
    # Получение дата-центра
    dcs = fetch_datacenters(headers)
    dc = select_datacenter_by_location(dcs, location_key)
    datacenter_id = int(dc["id"])
    
    # Подбор тарифов
    tariffs = fetch_tariffs(headers)
    vps_id, drive_id, _ = compute_cheapest_configuration(
        dc, tariffs, cpu=cpu, ram_gb=ram, drive_gb=drive, ip=1
    )
    
    # Получение ОС
    os_list = fetch_os_list(headers)
    os_id = find_os_id(os_list, "ubuntu 22.04")
    
    # Создание сервера
    create_resp = create_server(
        headers=headers,
        datacenter_id=datacenter_id,
        vps_tariff_id=vps_id,
        drive_tariff_id=drive_id,
        os_id=os_id,
        payment_period=payment_period,
        cpu=cpu,
        ram_gb=ram,
        drive_gb=drive,
        ip=1,
        computer_name=f"vpn-{protocol}-auto",
        user_comment=f"Auto-provisioned for {protocol} ({configs_count} configs)",
        get_price_only=False,
    )
    
    virtual_server_id = create_resp.get("virtual_server_id")
    if not virtual_server_id:
        raise RuntimeError("API не вернул virtual_server_id")
    
    # Ожидание готовности
    wait_for_server_ready(headers, virtual_server_id)
    
    # Получение IP и credentials с повторными попытками
    max_retries = 40
    retry_delay = 8
    ip_addr = None
    creds = None
    
    for attempt in range(1, max_retries + 1):
        print(f"[RUVDS] Попытка {attempt}/{max_retries} получить данные сервера {virtual_server_id}...")
        
        try:
            # Пытаемся получить IP
            ip_addr = fetch_server_ip(headers, virtual_server_id)
            
            # Пытаемся получить credentials
            creds = fetch_start_password(headers, virtual_server_id, decode=True)
            
            # Проверяем, что оба значения получены
            if ip_addr and creds and creds.get("password"):
                print(f"[RUVDS] Данные сервера успешно получены: IP={ip_addr}")
                break
            else:
                if not ip_addr:
                    print(f"[RUVDS] IP адрес еще не назначен, ожидание {retry_delay}с...")
                if not creds or not creds.get("password"):
                    print(f"[RUVDS] Пароль еще не доступен, ожидание {retry_delay}с...")
        except Exception as e:
            print(f"[RUVDS] Ошибка при получении данных: {e}, ожидание {retry_delay}с...")
        
        if attempt < max_retries:
            time.sleep(retry_delay)
    
    if not ip_addr:
        raise RuntimeError(f"Не удалось получить IP адрес сервера после {max_retries} попыток")
    if not creds or not creds.get("password"):
        raise RuntimeError(f"Не удалось получить пароль сервера после {max_retries} попыток")
    
    return {
        "ip": ip_addr,
        "login": creds.get("login") or "root",
        "password": creds["password"],
        "server_id": str(virtual_server_id)
    }


def delete_server(server_id: str) -> bool:
    """
    Удаляет VPS сервер на RUVDS по ID.
    
    Args:
        server_id: ID сервера в RUVDS
        
    Returns:
        True если удаление успешно, False если ошибка
    """
    if not API_TOKEN:
        print(f"[Удаление] RUVDS_API_TOKEN не найден, пропускаю удаление сервера {server_id}")
        return False
    
    headers = {
        "Authorization": f"Bearer {API_TOKEN}",
        "Content-Type": "application/json"
    }
    
    try:
        url = f"{API_BASE_URL}/servers/{server_id}"
        resp = requests.delete(url, headers=headers, timeout=30)
        
        if resp.status_code in (200, 202, 204):
            print(f"[Удаление] Сервер id={server_id} успешно удалён")
            return True
        else:
            print(f"[Удаление] Ошибка при удалении сервера {server_id}: {resp.status_code}: {resp.text}")
            return False
    except Exception as e:
        print(f"[Удаление] Исключение при удалении сервера {server_id}: {e}")
        return False


if __name__ == "__main__":
    # CLI interface для тестирования
    parser = argparse.ArgumentParser()
    parser.add_argument("--protocol", required=True)
    parser.add_argument("--configs-count", type=int, required=True)
    parser.add_argument("--location", required=True)
    parser.add_argument("--payment-period", type=int, default=2)
    
    args = parser.parse_args()
    
    result = rent_server_for_bot(
        protocol=args.protocol,
        configs_count=args.configs_count,
        location_key=args.location,
        payment_period=args.payment_period
    )
    
    print(json.dumps(result, indent=2, ensure_ascii=False))
