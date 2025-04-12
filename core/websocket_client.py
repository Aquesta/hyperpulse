import asyncio
from hyperliquid.info import Info
from hyperliquid.utils import constants
from collections import deque, defaultdict
from datetime import datetime
from config.logging_config import setup_logger
import streamlit as st
import time
import threading
import queue

logger = setup_logger(__name__)

# Очередь для логов, которые будут отображаться в интерфейсе
queue_logger = queue.Queue()

# Используем отдельные очереди для каждой монеты
trade_data_by_coin = defaultdict(lambda: deque(maxlen=1000))
# Общая очередь для совместимости с предыдущим кодом
trade_data = deque(maxlen=1000)

# Глобальные переменные для управления соединением
current_connection = None
current_loop = None
is_connected = False
last_message_time = time.time()
reconnect_interval = 10  # секунды
heartbeat_interval = 30  # секунды

# Очередь для обновления накопительной статистики
# Будет обрабатываться в data_processor
cumulative_stats_queue = queue.Queue(maxsize=1000)

def on_message(message):
    """Обработчик входящих сообщений от WebSocket"""
    global last_message_time
    
    # Обновляем время последнего сообщения
    last_message_time = time.time()
    
    if 'data' not in message or not isinstance(message['data'], list):
        return
        
    for trade in message['data']:
        try:
            # Проверка наличия всех необходимых полей
            required_fields = ['side', 'time', 'px', 'sz', 'coin']
            if not all(field in trade for field in required_fields):
                logger.warning(f"Пропуск сообщения с отсутствующими полями: {trade}")
                continue
                
            coin = trade.get('coin')
            users = trade.get('users', [])
            
            # Добавляем логирование для отладки
            logger.info(f"Получено сообщение: {coin}, side={trade['side']}, users={users}")
            
            buyer = seller = None

            if trade['side'] == 'B':  # Покупка
                buyer = users[0] if len(users) > 0 else "unknown"
                seller = users[1] if len(users) > 1 else "unknown"
            elif trade['side'] == 'A':  # Продажа
                seller = users[0] if len(users) > 0 else "unknown"
                buyer = users[1] if len(users) > 1 else "unknown"

            trade_info = {
                'timestamp': datetime.fromtimestamp(trade['time'] / 1000),
                'side': trade['side'],
                'price': float(trade['px']),
                'volume': float(trade['sz']),
                'total': float(trade['sz']) * float(trade['px']),
                'coin': coin,
                'buyer': buyer,
                'seller': seller
            }

            # Добавляем в обе очереди: общую и специфичную для монеты
            trade_data.append(trade_info)
            trade_data_by_coin[coin].append(trade_info)
            
            # Добавляем информацию о сделке в очередь для обновления накопительной статистики
            try:
                cumulative_stats_queue.put(trade_info, block=False)
            except queue.Full:
                logger.warning("Очередь для накопительной статистики переполнена, данные будут потеряны")
            
            # Логируем добавленные данные
            logger.info(f"Добавлены данные о сделке: {coin}, buyer={buyer}, seller={seller}")
            
        except Exception as e:
            logger.error(f"Ошибка при обработке трейда: {e}")

def check_connection_status():
    """Проверяет статус соединения и переподключается при необходимости"""
    global is_connected, last_message_time
    
    while True:
        current_time = time.time()
        
        # Проверяем, давно ли было последнее сообщение
        if is_connected and current_time - last_message_time > heartbeat_interval:
            logger.warning(f"Нет сообщений за последние {heartbeat_interval} секунд. Возможно соединение потеряно.")
            is_connected = False
        
        # Если соединение потеряно и есть текущая монета, пытаемся переподключиться
        if not is_connected and 'current_coin' in st.session_state and st.session_state.current_coin:
            logger.info(f"Попытка переподключения к {st.session_state.current_coin}...")
            try:
                close_websocket()
                # Даем время на закрытие соединения
                time.sleep(1)  
                # Создаем новый поток для соединения
                ws_thread = threading.Thread(
                    target=start_websocket,
                    args=(st.session_state.current_coin,),
                    daemon=True
                )
                ws_thread.start()
                st.session_state.websocket_thread = ws_thread
            except Exception as e:
                logger.error(f"Ошибка при переподключении: {e}")
        
        # Проверяем каждые reconnect_interval секунд
        time.sleep(reconnect_interval)

def start_connection_monitor():
    """Запускает мониторинг соединения в отдельном потоке"""
    if not hasattr(start_connection_monitor, 'is_running') or not start_connection_monitor.is_running:
        monitor_thread = threading.Thread(target=check_connection_status, daemon=True)
        monitor_thread.start()
        start_connection_monitor.is_running = True
        logger.info("Запущен мониторинг WebSocket соединения")

def close_websocket():
    """Закрывает текущее WebSocket соединение если оно существует"""
    global current_connection, current_loop, is_connected
    
    if current_connection:
        try:
            logger.info("Закрытие текущего WebSocket соединения")
            # Остановить event loop
            if current_loop and current_loop.is_running():
                current_loop.stop()
            current_connection = None
            is_connected = False
            logger.info("WebSocket соединение успешно закрыто")
        except Exception as e:
            logger.error(f"Ошибка при закрытии WebSocket соединения: {e}")

def get_trade_data(coin=None):
    """
    Получает данные трейдов с возможностью фильтрации по монете
    
    Args:
        coin (str, optional): Символ криптовалюты. По умолчанию None (все монеты).
        
    Returns:
        deque: Очередь с данными трейдов
    """
    if coin and coin in trade_data_by_coin:
        return trade_data_by_coin[coin]
    return trade_data

def start_websocket(coin):
    """Запускает WebSocket соединение для указанной криптовалюты"""
    global current_connection, current_loop, is_connected, last_message_time
    
    # Закрыть предыдущее соединение если оно существует
    close_websocket()
    
    # Запустить мониторинг соединения, если еще не запущен
    start_connection_monitor()
    
    # Создаем новый event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    current_loop = loop

    try:
        logger.info(f"Создание нового WebSocket соединения для {coin}")
        # Инициализация клиента HyperLiquid
        info = Info(base_url=constants.MAINNET_API_URL, skip_ws=False)
        # Создание подписки на трейды
        subscription = {"type": "trades", "coin": coin}
        # Подписка на события
        info.subscribe(subscription, on_message)
        
        # Обновляем состояние соединения
        current_connection = info
        is_connected = True
        last_message_time = time.time()
        
        logger.info(f"WebSocket соединение успешно создано для {coin}")
        loop.run_forever()
    except Exception as e:
        logger.error(f"Ошибка в WebSocket: {e}")
        is_connected = False
    finally:
        loop.close()
        logger.info("WebSocket loop закрыт")
        is_connected = False

def connect_websocket():
    """Подключается к WebSocket для текущей выбранной монеты"""
    if 'coin' not in st.session_state:
        st.session_state.coin = "BTC"
    
    # Сохраняем выбранную монету как текущую для переподключения
    st.session_state.current_coin = st.session_state.coin
    
    # Запускаем WebSocket в отдельном потоке
    ws_thread = threading.Thread(
        target=start_websocket,
        args=(st.session_state.coin,),
        daemon=True
    )
    ws_thread.start()
    
    # Сохраняем поток в состоянии сессии для возможности управления
    st.session_state.websocket_thread = ws_thread
    
    # Отправляем сообщение в очередь логов
    queue_logger.put(f"Подключение к WebSocket для {st.session_state.coin} запущено")
    
    return True