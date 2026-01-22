-- Начальная настройка бонусов за пополнение
-- Выполните этот скрипт после запуска бота для создания базовых бонусов

-- Удаление старых бонусов (если нужно начать с чистого листа)
-- DELETE FROM deposit_bonuses;

-- Создание базовой системы бонусов
INSERT INTO deposit_bonuses (min_amount, bonus_amount, is_active, description) VALUES
(20.00, 5.00, 1, 'Стартовый бонус'),
(50.00, 15.00, 1, 'Средний бонус'),
(100.00, 35.00, 1, 'Премиум бонус'),
(200.00, 80.00, 1, 'VIP бонус');

-- Проверка созданных бонусов
SELECT 
    id,
    min_amount || '$ → +' || bonus_amount || '$ (' || 
    ROUND(bonus_amount * 100.0 / min_amount, 1) || '%)' as bonus_info,
    CASE WHEN is_active = 1 THEN 'Активен' ELSE 'Отключён' END as status,
    description
FROM deposit_bonuses
ORDER BY min_amount ASC;
