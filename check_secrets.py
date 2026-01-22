#!/usr/bin/env python3
"""
Скрипт проверки проекта перед публикацией на GitHub.
Проверяет отсутствие чувствительных данных в коде.
"""

import os
import re
import sys
from pathlib import Path

# Паттерны для поиска чувствительных данных
SENSITIVE_PATTERNS = [
    (r'\d{10}:\w{35}', 'Telegram Bot Token'),
    (r'sk-[a-zA-Z0-9]{48}', 'OpenAI API Key'),
    (r'ghp_[a-zA-Z0-9]{36}', 'GitHub Personal Access Token'),
    (r'AKIA[0-9A-Z]{16}', 'AWS Access Key'),
    (r'mongodb\+srv://[^"\']+', 'MongoDB Connection String'),
    (r'postgres://[^"\']+', 'PostgreSQL Connection String'),
    (r'mysql://[^"\']+', 'MySQL Connection String'),
]

# Файлы и папки для игнорирования
IGNORE_PATTERNS = [
    '.git',
    '__pycache__',
    '*.pyc',
    'venv',
    'env',
    '.env',
    'artifacts',
    'backups',
    'logs',
    'bot.db',
    'check_secrets.py',  # Этот файл
]

# Файлы которые ДОЛЖНЫ существовать
REQUIRED_FILES = [
    '.gitignore',
    'README.md',
    'LICENSE',
    '.env.example',
    'requirements.txt',
]

def should_ignore(path: Path) -> bool:
    """Проверить, нужно ли игнорировать файл."""
    path_str = str(path)
    for pattern in IGNORE_PATTERNS:
        if pattern in path_str:
            return True
    return False

def check_file_for_secrets(filepath: Path) -> list:
    """Проверить файл на наличие секретов."""
    issues = []
    
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
            
        for pattern, description in SENSITIVE_PATTERNS:
            matches = re.findall(pattern, content)
            if matches:
                issues.append({
                    'file': str(filepath),
                    'type': description,
                    'matches': len(matches)
                })
    except Exception as e:
        pass  # Игнорируем ошибки чтения
    
    return issues

def check_required_files(base_path: Path) -> list:
    """Проверить наличие обязательных файлов."""
    missing = []
    for required_file in REQUIRED_FILES:
        if not (base_path / required_file).exists():
            missing.append(required_file)
    return missing

def main():
    """Основная функция проверки."""
    base_path = Path(__file__).parent
    print("🔍 Проверка проекта перед публикацией на GitHub...\n")
    
    # Проверка обязательных файлов
    print("📋 Проверка наличия обязательных файлов...")
    missing_files = check_required_files(base_path)
    if missing_files:
        print("❌ Отсутствуют обязательные файлы:")
        for f in missing_files:
            print(f"   - {f}")
        print()
    else:
        print("✅ Все обязательные файлы на месте\n")
    
    # Поиск чувствительных данных
    print("🔐 Поиск чувствительных данных в коде...")
    all_issues = []
    
    for filepath in base_path.rglob('*.py'):
        if should_ignore(filepath):
            continue
        
        issues = check_file_for_secrets(filepath)
        all_issues.extend(issues)
    
    if all_issues:
        print("❌ ВНИМАНИЕ! Найдены потенциально чувствительные данные:\n")
        for issue in all_issues:
            print(f"   Файл: {issue['file']}")
            print(f"   Тип: {issue['type']}")
            print(f"   Совпадений: {issue['matches']}")
            print()
        print("⚠️  УДАЛИТЕ эти данные перед публикацией!\n")
        return 1
    else:
        print("✅ Чувствительные данные не найдены\n")
    
    # Проверка .gitignore
    gitignore_path = base_path / '.gitignore'
    if gitignore_path.exists():
        with open(gitignore_path, 'r') as f:
            gitignore_content = f.read()
        
        critical_ignores = ['.env', 'bot.db', '99.txt', 'servera.txt']
        missing_ignores = [ig for ig in critical_ignores if ig not in gitignore_content]
        
        if missing_ignores:
            print("⚠️  В .gitignore отсутствуют критичные записи:")
            for ig in missing_ignores:
                print(f"   - {ig}")
            print()
        else:
            print("✅ .gitignore корректно настроен\n")
    
    # Итоговый результат
    if all_issues or missing_files:
        print("❌ Проект НЕ ГОТОВ к публикации")
        print("   Исправьте указанные проблемы и запустите проверку снова")
        return 1
    else:
        print("✅ Проект ГОТОВ к публикации на GitHub!")
        print("\nСледующие шаги:")
        print("1. git init")
        print("2. git add .")
        print("3. git commit -m 'Initial commit'")
        print("4. git remote add origin https://github.com/yourusername/sova-vpn-bot.git")
        print("5. git push -u origin main")
        return 0

if __name__ == '__main__':
    sys.exit(main())
