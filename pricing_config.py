#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Конфигурация ценообразования для автовыдачи VPN серверов.
Все цены рассчитываются по формулам на основе базовых тарифов и коэффициентов.
"""

# Базовые тарифы по количеству конфигов (цена за 1 месяц)
VOLUME_TARIFFS = [
    {"min": 1,   "max": 15,   "price_month": 20.0,  "label": "1-15 конфигов"},
    {"min": 16,  "max": 30,   "price_month": 25.0,  "label": "16-30 конфигов"},
    {"min": 31,  "max": 100,  "price_month": 60.0,  "label": "31-100 конфигов"},
    {"min": 101, "max": 250,  "price_month": 120.0, "label": "101-250 конфигов"},
]

# Коэффициенты для сроков аренды (множитель от месячной цены)
TERM_FACTORS = {
    1:    {"factor": 1.0,  "label": "1 месяц",    "months": 1, "discount": 0},
    2:    {"factor": 1.9,  "label": "2 месяца",   "months": 2, "discount": 5},
    3:    {"factor": 2.7,  "label": "3 месяца",   "months": 3, "discount": 10},
    6:    {"factor": 5.1,  "label": "6 месяцев",  "months": 6, "discount": 15},
    12:   {"factor": 9.0,  "label": "12 месяцев", "months": 12, "discount": 25},
}


def get_tariff_by_configs(configs_count: int) -> dict:
    """
    Найти тариф по количеству конфигов.
    
    Args:
        configs_count: Количество конфигураций
        
    Returns:
        dict с информацией о тарифе
    """
    for tariff in VOLUME_TARIFFS:
        if tariff["min"] <= configs_count <= tariff["max"]:
            return tariff.copy()
    
    # Если не нашли - берём максимальный
    return VOLUME_TARIFFS[-1].copy()


def calculate_price(configs_count: int, term_key) -> float:
    """
    Рассчитать итоговую цену по формуле.
    
    Args:
        configs_count: Количество конфигураций
        term_key: Ключ срока аренды ("1w", 1, 2, 3, 6, 12)
        
    Returns:
        float - итоговая цена
    """
    # Получить базовый тариф
    tariff = get_tariff_by_configs(configs_count)
    base_price = tariff["price_month"]
    
    # Получить коэффициент срока
    term_info = TERM_FACTORS.get(term_key)
    if not term_info:
        term_info = TERM_FACTORS[1]  # По умолчанию 1 месяц
    
    # Рассчитать итоговую цену
    total_price = base_price * term_info["factor"]
    
    return round(total_price, 2)


def calculate_price_detailed(configs_count: int, term_key) -> dict:
    """
    Рассчитать детальную информацию о цене.
    
    Args:
        configs_count: Количество конфигураций
        term_key: Ключ срока аренды ("1w", 1, 2, 3, 6, 12)
        
    Returns:
        dict с расчётом:
        {
            "base_price": float,      # Базовая цена за месяц
            "term_factor": float,     # Коэффициент срока
            "total_price": float,     # Итоговая цена
            "discount": int,          # Процент скидки
            "term_label": str,        # Название срока
            "tariff_label": str,      # Название тарифа
            "price_per_month": float, # Цена за месяц (с учётом скидки)
            "price_per_config": float # Цена за конфиг в месяц
        }
    """
    # Получить базовый тариф
    tariff = get_tariff_by_configs(configs_count)
    base_price = tariff["price_month"]
    
    # Получить коэффициент срока
    term_info = TERM_FACTORS.get(term_key)
    if not term_info:
        term_info = TERM_FACTORS[1]  # По умолчанию 1 месяц
    
    # Рассчитать итоговую цену
    total_price = base_price * term_info["factor"]
    
    # Рассчитать эффективную цену за месяц
    months = term_info["months"] if term_info["months"] > 0 else 0.25  # неделя = 0.25 месяца
    price_per_month = total_price / months if months > 0 else total_price
    
    # Цена за 1 конфиг в месяц
    price_per_config = price_per_month / configs_count if configs_count > 0 else 0
    
    return {
        "base_price": base_price,
        "term_factor": term_info["factor"],
        "total_price": round(total_price, 2),
        "discount": term_info["discount"],
        "term_label": term_info["label"],
        "tariff_label": tariff["label"],
        "months": term_info["months"],
        "price_per_month": round(price_per_month, 2),
        "price_per_config": round(price_per_config, 2)
    }


def get_all_term_prices(configs_count: int) -> list:
    """
    Получить список всех доступных сроков с ценами.
    
    Args:
        configs_count: Количество конфигураций
        
    Returns:
        list of dict с информацией по каждому сроку
    """
    result = []
    for term_key in TERM_FACTORS.keys():
        calc = calculate_price_detailed(configs_count, term_key)
        calc["term_key"] = term_key
        result.append(calc)
    return result


if __name__ == "__main__":
    # Тестирование расчётов
    print("=== ТЕСТИРОВАНИЕ ЦЕНООБРАЗОВАНИЯ ===\n")
    
    test_cases = [
        (10, "1w"),
        (10, 1),
        (10, 2),
        (10, 12),
        (50, 3),
        (150, 6),
    ]
    
    for configs, term in test_cases:
        total = calculate_price(configs, term)
        calc = calculate_price_detailed(configs, term)
        print(f"{configs} конфигов, {calc['term_label']}:")
        print(f"  Базовая цена: {calc['base_price']} $/мес")
        print(f"  Коэффициент: {calc['term_factor']}")
        print(f"  Скидка: -{calc['discount']}%")
        print(f"  ИТОГО: {total} $ (проверка: {calc['total_price']} $)")
        print(f"  За месяц: {calc['price_per_month']} $")
        print(f"  За конфиг/мес: {calc['price_per_config']} $")
        print()
