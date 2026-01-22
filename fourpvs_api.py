"""
Модуль для работы с API 4VPS.SU
Документация: https://4vps.su/page/api
"""

import logging
from typing import Dict, List, Optional
import aiohttp
import asyncio
import ssl

logger = logging.getLogger(__name__)

API_BASE_URL = "https://4vps.su/api"

# Настройки для повторных попыток
MAX_RETRIES = 3
RETRY_DELAY = 2  # секунды
TIMEOUT = 30  # секунды


class FourVPSAPI:
    """Клиент для работы с API 4VPS.SU"""
    
    def __init__(self, api_token: str):
        """
        Args:
            api_token: API токен из личного кабинета 4VPS.SU
        """
        self.api_token = api_token
        self.headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json"
        }
        # Создаем SSL контекст с более мягкими требованиями
        self.ssl_context = ssl.create_default_context()
        # Можно раскомментировать, если нужно отключить проверку сертификата
        # self.ssl_context.check_hostname = False
        # self.ssl_context.verify_mode = ssl.CERT_NONE
    
    async def _get(self, endpoint: str) -> Dict:
        """Выполнить GET запрос к API с повторными попытками"""
        url = f"{API_BASE_URL}/{endpoint}"
        
        for attempt in range(MAX_RETRIES):
            try:
                timeout = aiohttp.ClientTimeout(total=TIMEOUT)
                connector = aiohttp.TCPConnector(ssl=self.ssl_context)
                
                async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
                    async with session.get(url, headers=self.headers) as response:
                        data = await response.json()
                        if data.get('error'):
                            logger.error(f"API error: {data.get('errorMessage')}")
                        return data
                        
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                logger.warning(f"API request attempt {attempt + 1}/{MAX_RETRIES} failed: {e}")
                
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1))  # Экспоненциальная задержка
                else:
                    logger.error(f"API request failed after {MAX_RETRIES} attempts: {e}")
                    return {"error": True, "errorMessage": f"Connection failed after {MAX_RETRIES} attempts: {str(e)}", "data": False}
                    
            except Exception as e:
                logger.error(f"Unexpected API error: {e}")
                return {"error": True, "errorMessage": str(e), "data": False}
    
    async def _post(self, endpoint: str, payload: Dict) -> Dict:
        """Выполнить POST запрос к API с повторными попытками"""
        url = f"{API_BASE_URL}/{endpoint}"
        
        for attempt in range(MAX_RETRIES):
            try:
                timeout = aiohttp.ClientTimeout(total=TIMEOUT)
                connector = aiohttp.TCPConnector(ssl=self.ssl_context)
                
                async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
                    async with session.post(url, headers=self.headers, json=payload) as response:
                        data = await response.json()
                        if data.get('error'):
                            logger.error(f"API error: {data.get('errorMessage')}")
                        return data
                        
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                logger.warning(f"API POST attempt {attempt + 1}/{MAX_RETRIES} failed: {e}")
                
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1))  # Экспоненциальная задержка
                else:
                    logger.error(f"API POST request failed after {MAX_RETRIES} attempts: {e}")
                    return {"error": True, "errorMessage": f"Connection failed after {MAX_RETRIES} attempts: {str(e)}", "data": False}
                    
            except Exception as e:
                logger.error(f"Unexpected API POST error: {e}")
                return {"error": True, "errorMessage": str(e), "data": False}
    
    async def get_balance(self) -> Optional[float]:
        """
        Получить текущий баланс пользователя
        
        Returns:
            Баланс в рублях или None при ошибке
        """
        result = await self._get("userBalance")
        if not result.get('error') and result.get('data'):
            return result['data'].get('userBalance')
        return None
    
    async def get_datacenters(self) -> List[Dict]:
        """
        Получить список доступных дата-центров
        
        Returns:
            Список дата-центров с информацией о локациях
            Пример: [{"id": 1, "dc_name": "ОАЭ ДЦ1", "flag": "ae", ...}, ...]
        """
        result = await self._get("getDcList")
        if not result.get('error') and result.get('data'):
            dc_list = result['data'].get('dcList', {})
            datacenters = []
            seen_names = {}  # Словарь для отслеживания по базовому имени
            
            for dc_id, dc_info in dc_list.items():
                # Пропускаем строковые ключи (типа "r9H1"), берем только числовые ID
                try:
                    numeric_id = int(dc_id)
                    dc_name = dc_info.get('dc_name', '')
                    flag = dc_info.get('flag', '')
                    
                    # Удаляем префиксы вида [xxx] из названия для проверки дубликатов
                    clean_name = dc_name
                    if clean_name.startswith('[') and ']' in clean_name:
                        clean_name = clean_name.split(']', 1)[1].strip()
                    
                    # Ключ для дедупликации: флаг + очищенное имя
                    dedup_key = (flag, clean_name)
                    
                    # Пропускаем дубликаты (берем первый встреченный)
                    if dedup_key in seen_names:
                        continue
                    
                    seen_names[dedup_key] = numeric_id
                    
                    datacenters.append({
                        "id": numeric_id,
                        "name": clean_name,  # Используем очищенное имя
                        "flag": flag,
                        "cpu_name": dc_info.get('cpu_name', ''),
                        "frequency": dc_info.get('frequency', ''),
                        "ip_price": int(dc_info.get('ip_price', 0)),
                        "core_price": int(dc_info.get('core_price', 0)),
                        "ram_price": int(dc_info.get('ram_price', 0)),
                        "disk_price": int(dc_info.get('disk_price', 0)),
                        "max_core": int(dc_info.get('max_core', 0)),
                        "max_ram": int(dc_info.get('max_ram', 0)),
                        "max_disk": int(dc_info.get('max_disk', 0))
                    })
                except (ValueError, TypeError):
                    # Пропускаем нечисловые ключи
                    continue
            return datacenters
        return []
    
    async def get_tariffs(self) -> Dict:
        """
        Получить список тарифов
        
        Returns:
            Словарь с информацией о тарифах по дата-центрам
        """
        result = await self._get("getTarifList")
        if not result.get('error') and result.get('data'):
            return result['data'].get('tarifList', {})
        return {}
    
    async def get_tariff_info(self, tariff_id: int, dc_id: int) -> Optional[Dict]:
        """
        Получить информацию о конкретном тарифе
        
        Args:
            tariff_id: ID тарифа
            dc_id: ID дата-центра
            
        Returns:
            Информация о тарифе или None
        """
        result = await self._get(f"getTarifInfo/{tariff_id}/{dc_id}")
        if not result.get('error') and result.get('data'):
            return result['data'].get('tarifInfo')
        return None
    
    async def get_images(self, tariff_id: int, dc_id: int) -> Dict:
        """
        Получить список доступных образов ОС для тарифа
        
        Args:
            tariff_id: ID тарифа
            dc_id: ID дата-центра
            
        Returns:
            Словарь образов {id: "название ОС"}
        """
        result = await self._get(f"getImages/{tariff_id}/{dc_id}")
        if not result.get('error') and result.get('data'):
            return result['data'].get('images', {})
        return {}
    
    async def buy_server(
        self,
        tariff_id: int,
        datacenter_id: int,
        os_template: int,
        name: str,
        period: Optional[int] = None,
        domain: Optional[str] = None
    ) -> Optional[Dict]:
        """
        Арендовать новый сервер
        
        Args:
            tariff_id: ID тарифа
            datacenter_id: ID дата-центра
            os_template: ID образа ОС
            name: Имя сервера (3-125 символов, только A-Za-z0-9-_)
            period: Период аренды в часах (720=1мес, 2160=3мес, 4320=6мес, 8640=12мес)
            domain: Домен сервера (необязательно)
            
        Returns:
            {"serverid": "12345", "password": "abc123"} или None при ошибке
        """
        payload = {
            "tarif": tariff_id,
            "datacenter": datacenter_id,
            "ostempl": os_template,
            "name": name
        }
        
        if period:
            payload["period"] = period
        if domain:
            payload["domain"] = domain
        
        result = await self._post("action/buyServer", payload)
        if not result.get('error') and result.get('data'):
            return result['data']
        return None
    
    async def delete_server(self, server_id: int) -> bool:
        """
        Удалить сервер
        
        Args:
            server_id: ID сервера
            
        Returns:
            True если успешно удален
        """
        result = await self._post("action/deleteServer", {"serverid": server_id})
        return not result.get('error')
    
    async def get_my_servers(self) -> List[Dict]:
        """
        Получить список арендованных серверов
        
        Returns:
            Список серверов с подробной информацией
        """
        result = await self._get("myservers")
        if not result.get('error') and result.get('data'):
            return result['data'].get('serverlist', [])
        return []
    
    async def get_server_info(self, server_id: int) -> Optional[Dict]:
        """
        Получить информацию о сервере
        
        Args:
            server_id: ID сервера
            
        Returns:
            Полная информация о сервере
        """
        result = await self._get(f"getServerInfo/{server_id}")
        if not result.get('error') and result.get('data'):
            return result['data']
        return None
    
    async def power_on(self, server_id: int) -> bool:
        """Включить сервер"""
        result = await self._post("action/power_on", {"serverid": server_id})
        return not result.get('error')
    
    async def shutdown(self, server_id: int) -> bool:
        """Выключить сервер"""
        result = await self._post("action/shutdown", {"serverid": server_id})
        return not result.get('error')
    
    async def reboot(self, server_id: int) -> bool:
        """Перезагрузить сервер"""
        result = await self._post("action/reboot", {"serverid": server_id})
        return not result.get('error')


# Маппинг флагов стран на русские названия
COUNTRY_NAMES = {
    "ae": "ОАЭ",
    "ca": "Канада",
    "nl": "Нидерланды",
    "ru": "Россия",
    "us": "США",
    "de": "Германия",
    "es": "Испания",
    "fi": "Финляндия",
    "gb": "Великобритания",
    "fr": "Франция",
    "ua": "Украина",
    "at": "Австрия",
    "it": "Италия",
    "hk": "Гонконг",
    "ch": "Швейцария",
    "pt": "Португалия",
    "se": "Швеция",
    "tr": "Турция",
    "pl": "Польша",
    "ro": "Румыния",
    "bg": "Болгария",
    "lt": "Литва",
    "lv": "Латвия",
    "ee": "Эстония",
    "cz": "Чехия",
    "sk": "Словакия",
    "hu": "Венгрия",
    "gr": "Греция",
    "no": "Норвегия",
    "dk": "Дания",
    "is": "Исландия",
    "ie": "Ирландия",
    "be": "Бельгия",
    "lu": "Люксембург",
    "sg": "Сингапур",
    "jp": "Япония",
    "kr": "Южная Корея",
    "au": "Австралия",
    "nz": "Новая Зеландия",
    "br": "Бразилия",
    "ar": "Аргентина",
    "cl": "Чили",
    "mx": "Мексика",
    "za": "ЮАР",
    "eg": "Египет",
    "il": "Израиль",
    "sa": "Саудовская Аравия",
    "in": "Индия",
    "th": "Таиланд",
    "vn": "Вьетнам",
    "id": "Индонезия",
    "my": "Малайзия",
    "ph": "Филиппины"
}


def get_country_name(flag_code: str) -> str:
    """Получить русское название страны по коду флага"""
    return COUNTRY_NAMES.get(flag_code.lower(), flag_code.upper())


def get_flag_emoji(flag_code: str) -> str:
    """Конвертировать код страны в эмодзи флага"""
    if not flag_code or len(flag_code) != 2:
        return "🌍"
    
    # Конвертируем буквы в региональные индикаторы
    code_points = [ord(c) + 127397 for c in flag_code.upper()]
    return chr(code_points[0]) + chr(code_points[1])
