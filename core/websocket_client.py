import asyncio
from hyperliquid.info import Info
from hyperliquid.utils import constants
from collections import deque, defaultdict
from datetime import datetime
import streamlit as st
import time
import threading
import queue
from config.logging_config import setup_logger
from config.app_config import get_config

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
connection_status = {
    "connected": False,
    "last_message_time": time.time(),
    "last_error": None,
    "error_time": None,
    "reconnect_count": 0
}

# Константы
RECONNECT_INTERVAL = 10  # секунды
HEARTBEAT_INTERVAL = 30  # секунды
MAX_RECONNECT_ATTEMPTS = 5
RECONNECT_DELAY = 5  # секунды между попытками
QUEUE_SIZE = 1000

# Очередь для обновления накопительной статистики
cumulative_stats_queue = queue.Queue(maxsize=QUEUE_SIZE)

def on_message(message):
    """Обработчик входящих сообщений от WebSocket"""
    global connection_status
    
    try:
        # Обновляем время последнего сообщения
        connection_status["last_message_time"] = time.time()
        
        if not isinstance(message, dict) or 'data' not in message or not isinstance(message['data'], list):
            logger.warning(f"Получено некорректное сообщение: {message}")
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
                logger.debug(f"Получено сообщение: {coin}, side={trade['side']}, users={users}")
                
                buyer = seller = "unknown"
                
                # Определяем покупателя и продавца
                if users:
                    if trade['side'] == 'B':  # Покупка
                        buyer = users[0]
                        seller = users[1] if len(users) > 1 else "unknown"
                    else:  # Продажа
                        seller = users[0]
                        buyer = users[1] if len(users) > 1 else "unknown"
                
                # Создаем объект с информацией о сделке
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
                
                # Добавляем в очереди
                trade_data.append(trade_info)
                trade_data_by_coin[coin].append(trade_info)
                
                # Добавляем в очередь статистики без блокировки
                try:
                    cumulative_stats_queue.put_nowait(trade_info)
                except queue.Full:
                    logger.warning("Очередь статистики переполнена")
                
                logger.debug(f"Обработан трейд: {coin} {trade['side']} {trade_info['volume']:.6f} @ {trade_info['price']:.2f}")
                
            except Exception as e:
                logger.error(f"Ошибка при обработке трейда: {str(e)}")
                logger.debug(f"Проблемное сообщение: {trade}")
                
    except Exception as e:
        logger.error(f"Ошибка в обработчике сообщений: {str(e)}")

def check_connection_status():
    """Проверяет статус соединения и переподключается при необходимости"""
    global connection_status
    
    while True:
        try:
            current_time = time.time()
            
            # Проверяем, давно ли было последнее сообщение
            if connection_status["connected"] and current_time - connection_status["last_message_time"] > HEARTBEAT_INTERVAL:
                logger.warning(f"Нет сообщений за последние {HEARTBEAT_INTERVAL} секунд")
                connection_status["connected"] = False
            
            # Если соединение потеряно и есть текущая монета, пытаемся переподключиться
            if not connection_status["connected"] and 'current_coin' in st.session_state:
                if connection_status["reconnect_count"] < MAX_RECONNECT_ATTEMPTS:
                    logger.info(f"Попытка переподключения {connection_status['reconnect_count'] + 1} из {MAX_RECONNECT_ATTEMPTS}")
                    try:
                        close_websocket()
                        time.sleep(RECONNECT_DELAY)
                        
                        ws_thread = threading.Thread(
                            target=start_websocket,
                            args=(st.session_state.current_coin,),
                            daemon=True
                        )
                        ws_thread.start()
                        st.session_state.websocket_thread = ws_thread
                        
                        connection_status["reconnect_count"] += 1
                        
                    except Exception as e:
                        logger.error(f"Ошибка при переподключении: {str(e)}")
                else:
                    logger.error("Достигнут лимит попыток переподключения")
                    time.sleep(RECONNECT_INTERVAL * 2)  # Ждем дольше перед сбросом счетчика
                    connection_status["reconnect_count"] = 0
            
            time.sleep(RECONNECT_INTERVAL)
            
        except Exception as e:
            logger.error(f"Ошибка в мониторинге соединения: {str(e)}")
            time.sleep(RECONNECT_INTERVAL)

def start_connection_monitor():
    """Запускает мониторинг соединения в отдельном потоке"""
    if not hasattr(start_connection_monitor, 'is_running') or not start_connection_monitor.is_running:
        monitor_thread = threading.Thread(target=check_connection_status, daemon=True)
        monitor_thread.start()
        start_connection_monitor.is_running = True
        logger.info("Запущен мониторинг WebSocket соединения")

def close_websocket():
    """Закрывает текущее WebSocket соединение если оно существует"""
    global current_connection, current_loop, connection_status
    
    if current_connection:
        try:
            logger.info("Закрытие WebSocket соединения")
            if current_loop and current_loop.is_running():
                current_loop.stop()
            current_connection = None
            connection_status["connected"] = False
            logger.info("WebSocket соединение закрыто")
        except Exception as e:
            logger.error(f"Ошибка при закрытии соединения: {str(e)}")

def get_trade_data(coin=None):
    """
    Получает данные трейдов с возможностью фильтрации по монете
    
    Args:
        coin (str, optional): Символ криптовалюты
        
    Returns:
        deque: Очередь с данными трейдов
    """
    try:
        if coin and coin in trade_data_by_coin:
            return trade_data_by_coin[coin]
        return trade_data
    except Exception as e:
        logger.error(f"Ошибка при получении данных трейдов: {str(e)}")
        return deque(maxlen=1000)

def start_websocket(coin):
    """Запускает WebSocket соединение для указанной криптовалюты"""
    global current_connection, current_loop, connection_status
    
    try:
        # Закрываем предыдущее соединение
        close_websocket()
        
        # Запускаем мониторинг соединения
        start_connection_monitor()
        
        # Создаем новый event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        current_loop = loop
        
        logger.info(f"Создание WebSocket соединения для {coin}")
        
        # Получаем конфигурацию
        config = get_config()
        use_testnet = config.get('USE_TESTNET', False)
        base_url = constants.TESTNET_API_URL if use_testnet else constants.MAINNET_API_URL
        
        # Инициализация клиента
        info = Info(base_url=base_url, skip_ws=False)
        
        # Создание подписки
        subscription = {"type": "trades", "coin": coin}
        info.subscribe(subscription, on_message)
        
        # Обновляем состояние
        current_connection = info
        connection_status.update({
            "connected": True,
            "last_message_time": time.time(),
            "last_error": None,
            "error_time": None
        })
        
        logger.info(f"WebSocket соединение установлено для {coin}")
        loop.run_forever()
        
    except Exception as e:
        logger.error(f"Ошибка при создании WebSocket: {str(e)}")
        connection_status.update({
            "connected": False,
            "last_error": str(e),
            "error_time": time.time()
        })
    finally:
        if loop:
            loop.close()
        connection_status["connected"] = False
        logger.info("WebSocket loop закрыт")

def connect_websocket():
    """Подключается к WebSocket для текущей выбранной монеты"""
    try:
        if 'coin' not in st.session_state:
            st.session_state.coin = "BTC"
        
        # Сохраняем выбранную монету
        st.session_state.current_coin = st.session_state.coin
        
        # Запускаем WebSocket
        ws_thread = threading.Thread(
            target=start_websocket,
            args=(st.session_state.coin,),
            daemon=True
        )
        ws_thread.start()
        
        # Сохраняем поток
        st.session_state.websocket_thread = ws_thread
        
        queue_logger.put(f"Подключение к WebSocket для {st.session_state.coin} запущено")
        return True
        
    except Exception as e:
        logger.error(f"Ошибка при подключении к WebSocket: {str(e)}")
        queue_logger.put(f"Ошибка подключения: {str(e)}")
        return False