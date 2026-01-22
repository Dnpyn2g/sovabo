#!/usr/bin/env python3
"""
Скрипт проверки всех зависимостей requirements.txt
"""

import sys

# Список критичных пакетов для проверки
CRITICAL_PACKAGES = [
    ('telegram', 'python-telegram-bot'),
    ('aiosqlite', 'aiosqlite'),
    ('dotenv', 'python-dotenv'),
    ('aiohttp', 'aiohttp'),
    ('requests', 'requests'),
    ('paramiko', 'paramiko'),
    ('qrcode', 'qrcode'),
    ('PIL', 'Pillow'),
    ('flask', 'Flask'),
    ('flask_cors', 'Flask-Cors'),
    ('cryptography', 'cryptography'),
    ('bcrypt', 'bcrypt'),
    ('nacl', 'PyNaCl'),
]

def check_packages():
    """Проверить установку всех пакетов."""
    missing = []
    installed = []
    
    print("🔍 Проверка зависимостей...\n")
    
    for import_name, package_name in CRITICAL_PACKAGES:
        try:
            __import__(import_name)
            installed.append(package_name)
            print(f"✅ {package_name}")
        except ImportError:
            missing.append(package_name)
            print(f"❌ {package_name}")
    
    print(f"\n{'='*50}")
    print(f"Установлено: {len(installed)}/{len(CRITICAL_PACKAGES)}")
    
    if missing:
        print(f"\n⚠️  Отсутствуют пакеты:")
        for pkg in missing:
            print(f"   - {pkg}")
        print(f"\nУстановите их командой:")
        print(f"pip install {' '.join(missing)}")
        return 1
    else:
        print("\n✅ Все критичные пакеты установлены!")
        return 0

if __name__ == '__main__':
    sys.exit(check_packages())
