import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from core.websocket_client import trade_data, get_trade_data, cumulative_stats_queue
from config.logging_config import setup_logger
import time
from functools import lru_cache
import random
import threading
import queue
import json
import os
import atexit

logger = setup_logger(__name__)

# Директория для хранения данных
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
# Создаем директорию, если она не существует
os.makedirs(DATA_DIR, exist_ok=True)
# Путь к файлу с накопительной статистикой
STATS_FILE = os.path.join(DATA_DIR, 'cumulative_stats.json')

# Глобальная переменная для хранения накопленных данных по каждой монете
# Используем словарь: {монета: DataFrame}
historical_data = {}

# Глобальная переменная для хранения накопительной статистики пользователей
# Формат: {coin: {user: {'buy_count': int, 'sell_count': int, 'buy_volume': float, 'sell_volume': float}}}
cumulative_user_stats = {}

# Ограничение хранения истории
MAX_HISTORY_MINUTES = 30
# Минимальное количество записей для валидных расчетов
MIN_RECORDS_THRESHOLD = 5
# Максимальный размер кэша
CACHE_SIZE = 32

def profile_execution(func):
    """Декоратор для профилирования времени выполнения функций"""
    def wrapper(*args, **kwargs):
        start_time = time.time()
        result = func(*args, **kwargs)
        end_time = time.time()
        execution_time = end_time - start_time
        logger.debug(f"Функция {func.__name__} выполнена за {execution_time:.4f} секунд")
        return result
    return wrapper

# Кэширование результатов с учетом временной метки
def time_aware_cache(seconds=5):
    """
    Декоратор для кэширования результатов с учетом времени
    Результаты кэшируются на указанное количество секунд
    """
    cache = {}
    
    def decorator(func):
        def wrapper(*args, **kwargs):
            # Создаем ключ из аргументов функции
            key = str(args) + str(kwargs)
            
            # Проверяем кэш
            if key in cache:
                result, timestamp = cache[key]
                # Проверяем актуальность кэша
                if time.time() - timestamp < seconds:
                    return result
            
            # Выполняем функцию и кэшируем результат
            result = func(*args, **kwargs)
            cache[key] = (result, time.time())
            
            # Очищаем старые записи из кэша
            current_time = time.time()
            expired_keys = [k for k, (_, ts) in cache.items() if current_time - ts > seconds]
            for k in expired_keys:
                del cache[k]
                
            return result
        return wrapper
    return decorator

@profile_execution
def clean_historical_data():
    """Очищает устаревшие данные из исторического хранилища"""
    global historical_data
    
    now = datetime.now()
    max_history_threshold = now - timedelta(minutes=MAX_HISTORY_MINUTES)
    
    cleaned_count = 0
    for coin in list(historical_data.keys()):
        if not historical_data[coin].empty:
            # Измеряем размер до очистки
            size_before = len(historical_data[coin])
            
            # Очистка по времени
            historical_data[coin] = historical_data[coin][
                historical_data[coin]['timestamp'] >= max_history_threshold
            ]
            
            # Удаляем пустые датафреймы
            if historical_data[coin].empty:
                del historical_data[coin]
                logger.debug(f"Удалены все исторические данные для {coin}")
            else:
                cleaned_count += (size_before - len(historical_data[coin]))
    
    if cleaned_count > 0:
        logger.debug(f"Очищено {cleaned_count} устаревших записей из исторического хранилища")

@profile_execution
def preprocess_new_data(new_data, coin):
    """
    Предварительная обработка новых данных перед добавлением в историю
    
    Args:
        new_data (list): Список новых данных трейдов
        coin (str): Символ криптовалюты
        
    Returns:
        DataFrame: Обработанный DataFrame с новыми данными
    """
    if not new_data:
        return pd.DataFrame()

    # Преобразуем в DataFrame
    df = pd.DataFrame(new_data)
    
    # Оптимизация типов данных для экономии памяти
    if not df.empty:
        # Преобразование текстовых колонок в категории
        for col in ['side', 'coin', 'buyer', 'seller']:
            if col in df.columns:
                df[col] = df[col].astype('category')
                
        # Преобразование числовых колонок в более экономичные типы
        for col in ['price', 'volume', 'total']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], downcast='float')
    
    logger.debug(f"Предобработано {len(df)} новых записей для {coin}")
    return df

@time_aware_cache(seconds=2)
@profile_execution
def process_trade_data(time_interval, coin):
    """
    Обрабатывает торговые данные для указанной монеты и временного интервала.
    
    Args:
        time_interval (int): Временной интервал в минутах
        coin (str): Символ криптовалюты (BTC, ETH, SOL)
        
    Returns:
        tuple: (DataFrame с данными, объемы по времени, настроение рынка)
    """
    global historical_data
    
    # Периодическая очистка исторических данных
    clean_historical_data()
    
    # Получаем данные для заданной монеты
    new_data = list(get_trade_data(coin))
    if not new_data:
        logger.info(f"Нет торговых данных для {coin}")
        return None, None, None, None

    # Предобработка новых данных
    new_df = preprocess_new_data(new_data, coin)
    if new_df.empty:
        return None, None, None, None

    # Инициализируем DataFrame для монеты, если его еще нет
    if coin not in historical_data or historical_data[coin].empty:
        historical_data[coin] = new_df
    else:
        # Объединяем с историей более эффективным способом
        # Используем только новые данные, которых еще нет в истории
        existing_timestamps = set(historical_data[coin]['timestamp'])
        truly_new_data = new_df[~new_df['timestamp'].isin(existing_timestamps)]
        
        if not truly_new_data.empty:
            historical_data[coin] = pd.concat([historical_data[coin], truly_new_data], ignore_index=True)
            # Сортируем по времени для более эффективной работы
            historical_data[coin].sort_values('timestamp', inplace=True)
            logger.debug(f"Добавлено {len(truly_new_data)} новых записей в историю для {coin}")

    # Фильтрация по заданному временному интервалу
    now = datetime.now()
    time_threshold = now - timedelta(minutes=time_interval)
    # Используем copy() только если необходимо изменять DataFrame
    interval_df = historical_data[coin][historical_data[coin]['timestamp'] >= time_threshold]

    if len(interval_df) < MIN_RECORDS_THRESHOLD:
        logger.warning(f"Недостаточно данных в интервале ({time_interval} мин) для {coin}: {len(interval_df)} записей")
        return None, None, None, None
    
    # Эффективный расчет метрик - используем векторизованные операции
    buy_mask = interval_df['side'] == 'B'
    sell_mask = interval_df['side'] == 'A'
    
    buy_volume = interval_df.loc[buy_mask, 'total'].sum()
    sell_volume = interval_df.loc[sell_mask, 'total'].sum()
    total_volume = buy_volume + sell_volume
    
    # Расчет настроения рынка по объему
    sentiment = (buy_volume / total_volume * 100) if total_volume > 0 else 50

    # Расчет настроения рынка по количеству сделок
    buy_count = buy_mask.sum()
    sell_count = sell_mask.sum()
    total_count = buy_count + sell_count
    count_sentiment = (buy_count / total_count * 100) if total_count > 0 else 50
    
    # Оптимизация для графиков - предварительно создаем time_bin
    if 'time_bin' not in interval_df.columns:
        interval_df.loc[:, 'time_bin'] = interval_df['timestamp'].dt.floor('1s')
    
    # Группировка с использованием оптимизированных методов и устранение предупреждения
    volume_by_time = interval_df.pivot_table(
        index='time_bin', 
        columns='side', 
        values='total', 
        aggfunc='sum',
        observed=True
    ).fillna(0)
    
    logger.debug(f"Обработаны данные: {len(interval_df)} записей, настроение рынка по объему: {sentiment:.1f}%, по количеству: {count_sentiment:.1f}%")

    return interval_df, volume_by_time, sentiment, count_sentiment

# Избегаем использования lru_cache с DataFrame, так как он не хешируемый
def get_user_statistics(df_input):
    """
    Рассчитывает статистику пользователей
    
    Args:
        df_input (DataFrame): DataFrame с данными трейдов
        
    Returns:
        DataFrame: Статистика пользователей
    """
    if df_input is None or df_input.empty:
        return pd.DataFrame()
        
    # Работаем с копией DataFrame для безопасности
    df = df_input.copy()
    
    # Группировка и агрегация для покупателей
    buyers = df[df['side'] == 'B'].groupby('buyer').agg(
        buy_count=('buyer', 'count'),
        buy_volume=('total', 'sum')
    ).reset_index().rename(columns={'buyer': 'user'})

    # Группировка и агрегация для продавцов
    sellers = df[df['side'] == 'A'].groupby('seller').agg(
        sell_count=('seller', 'count'),
        sell_volume=('total', 'sum')
    ).reset_index().rename(columns={'seller': 'user'})

    # Объединение результатов
    user_stats = pd.merge(buyers, sellers, on='user', how='outer').fillna(0)
    user_stats[['buy_count', 'sell_count']] = user_stats[['buy_count', 'sell_count']].astype(int)
    
    return user_stats

def update_cumulative_stats(trade_info):
    """
    Обновляет накопительную статистику пользователей при получении новых данных
    
    Args:
        trade_info (dict): Информация о сделке
    """
    global cumulative_user_stats
    
    if not trade_info:
        return
    
    coin = trade_info.get('coin')
    side = trade_info.get('side')
    buyer = trade_info.get('buyer')
    seller = trade_info.get('seller')
    total = trade_info.get('total', 0)
    
    if not coin or not side or not (buyer or seller):
        return
    
    # Инициализация структур при необходимости
    if coin not in cumulative_user_stats:
        cumulative_user_stats[coin] = {}
    
    # Обновление статистики покупателя
    if side == 'B' and buyer:
        if buyer not in cumulative_user_stats[coin]:
            cumulative_user_stats[coin][buyer] = {'buy_count': 0, 'sell_count': 0, 'buy_volume': 0, 'sell_volume': 0}
        cumulative_user_stats[coin][buyer]['buy_count'] += 1
        cumulative_user_stats[coin][buyer]['buy_volume'] += total
    
    # Обновление статистики продавца
    if side == 'A' and seller:
        if seller not in cumulative_user_stats[coin]:
            cumulative_user_stats[coin][seller] = {'buy_count': 0, 'sell_count': 0, 'buy_volume': 0, 'sell_volume': 0}
        cumulative_user_stats[coin][seller]['sell_count'] += 1
        cumulative_user_stats[coin][seller]['sell_volume'] += total

def get_cumulative_user_statistics(coin):
    """
    Возвращает накопительную статистику пользователей для указанной монеты
    
    Args:
        coin (str): Символ криптовалюты
        
    Returns:
        DataFrame: DataFrame с накопительной статистикой пользователей
    """
    global cumulative_user_stats
    
    if coin not in cumulative_user_stats or not cumulative_user_stats[coin]:
        # Возвращаем пустой DataFrame с нужной структурой
        return pd.DataFrame(columns=['user', 'buy_count', 'sell_count', 'buy_volume', 'sell_volume'])
    
    # Преобразуем словарь в DataFrame
    data = []
    for user, stats in cumulative_user_stats[coin].items():
        row = {
            'user': user,
            'buy_count': stats['buy_count'],
            'sell_count': stats['sell_count'],
            'buy_volume': stats['buy_volume'],
            'sell_volume': stats['sell_volume']
        }
        data.append(row)
    
    df = pd.DataFrame(data)
    
    return df

def save_cumulative_stats():
    """Сохраняет накопительную статистику в файл"""
    global cumulative_user_stats
    try:
        # Преобразуем структуру данных в формат, подходящий для JSON
        serializable_stats = {}
        for coin, users in cumulative_user_stats.items():
            serializable_stats[coin] = {
                user: stats for user, stats in users.items()
            }
        
        # Создаем директорию, если она не существует
        os.makedirs(os.path.dirname(STATS_FILE), exist_ok=True)
        
        # Сохраняем в файл
        with open(STATS_FILE, 'w') as f:
            json.dump(serializable_stats, f)
        logger.info(f"Накопительная статистика успешно сохранена в {STATS_FILE}")
    except Exception as e:
        logger.error(f"Ошибка при сохранении накопительной статистики: {e}")

def load_cumulative_stats():
    """Загружает накопительную статистику из файла"""
    global cumulative_user_stats
    try:
        if os.path.exists(STATS_FILE):
            with open(STATS_FILE, 'r') as f:
                loaded_stats = json.load(f)
            
            # Восстанавливаем структуру данных
            for coin, users in loaded_stats.items():
                if coin not in cumulative_user_stats:
                    cumulative_user_stats[coin] = {}
                for user, stats in users.items():
                    cumulative_user_stats[coin][user] = stats
            
            logger.info(f"Накопительная статистика успешно загружена из {STATS_FILE}")
        else:
            logger.info(f"Файл с накопительной статистикой не найден: {STATS_FILE}")
    except Exception as e:
        logger.error(f"Ошибка при загрузке накопительной статистики: {e}")

def clean_cumulative_stats(max_users_per_coin=1000):
    """
    Очищает накопительную статистику, оставляя только самых активных пользователей
    
    Args:
        max_users_per_coin (int): Максимальное количество пользователей для хранения по каждой монете
    """
    global cumulative_user_stats
    total_cleaned = 0
    
    try:
        for coin in list(cumulative_user_stats.keys()):
            if len(cumulative_user_stats[coin]) > max_users_per_coin:
                # Создаем DataFrame для сортировки
                data = []
                for user, stats in cumulative_user_stats[coin].items():
                    total_volume = stats['buy_volume'] + stats['sell_volume']
                    total_count = stats['buy_count'] + stats['sell_count']
                    data.append({
                        'user': user,
                        'total_volume': total_volume,
                        'total_count': total_count
                    })
                
                df = pd.DataFrame(data)
                # Сортируем по объему и количеству сделок
                df = df.sort_values(['total_volume', 'total_count'], ascending=False)
                
                # Оставляем только самых активных пользователей
                active_users = set(df.head(max_users_per_coin)['user'].tolist())
                
                # Удаляем неактивных пользователей
                inactive_users = set(cumulative_user_stats[coin].keys()) - active_users
                for user in inactive_users:
                    del cumulative_user_stats[coin][user]
                
                total_cleaned += len(inactive_users)
                logger.info(f"Очищено {len(inactive_users)} неактивных пользователей для {coin}")
        
        # Если были удаления, сохраняем обновленную статистику
        if total_cleaned > 0:
            save_cumulative_stats()
            
        return total_cleaned
    except Exception as e:
        logger.error(f"Ошибка при очистке накопительной статистики: {e}")
        return 0

# Периодическая очистка и сохранение статистики
def start_stats_maintenance(save_interval=300, clean_interval=3600):
    """
    Запускает периодическое сохранение и очистку накопительной статистики
    
    Args:
        save_interval (int): Интервал сохранения в секундах
        clean_interval (int): Интервал очистки в секундах
    """
    def maintenance_worker():
        last_save_time = time.time()
        last_clean_time = time.time()
        
        while True:
            current_time = time.time()
            
            # Проверяем необходимость сохранения
            if current_time - last_save_time > save_interval:
                save_cumulative_stats()
                last_save_time = current_time
            
            # Проверяем необходимость очистки
            if current_time - last_clean_time > clean_interval:
                clean_cumulative_stats()
                last_clean_time = current_time
            
            # Спим перед следующей проверкой
            time.sleep(10)
    
    # Запускаем обслуживание в отдельном потоке
    maintenance_thread = threading.Thread(target=maintenance_worker, daemon=True)
    maintenance_thread.start()
    logger.info("Запущен поток обслуживания накопительной статистики")

def process_cumulative_stats_queue():
    """
    Функция для обработки очереди с данными для накопительной статистики
    Запускается в отдельном потоке
    """
    global cumulative_user_stats
    
    logger.info("Запуск обработчика накопительной статистики")
    
    while True:
        try:
            # Проверяем очередь на наличие новых данных (с меньшим таймаутом)
            trade_info = cumulative_stats_queue.get(timeout=0.5)
            
            # Обновляем накопительную статистику
            update_cumulative_stats(trade_info)
            
            # Сообщаем, что задача выполнена
            cumulative_stats_queue.task_done()
        except queue.Empty:
            # Если очередь пуста, просто ждем немного
            time.sleep(0.05)
        except Exception as e:
            logger.error(f"Ошибка при обработке накопительной статистики: {e}")
            time.sleep(0.5)  # Ждем немного перед повторной попыткой

# Запускаем обработчик накопительной статистики в отдельном потоке
def start_cumulative_stats_processor():
    """Запускает обработчик накопительной статистики в отдельном потоке"""
    # Загружаем сохраненную статистику при запуске
    load_cumulative_stats()
    
    # Запускаем поток обработки очереди
    stats_thread = threading.Thread(target=process_cumulative_stats_queue, daemon=True)
    stats_thread.start()
    logger.info("Запущен поток обработки накопительной статистики")
    
    # Запускаем поток обслуживания (сохранение и очистка)
    start_stats_maintenance()

# Регистрируем сохранение статистики при выходе
atexit.register(save_cumulative_stats)

# Запускаем обработчик при импорте модуля
start_cumulative_stats_processor()