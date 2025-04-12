"""
Конфигурация приложения
"""

import os
from typing import Dict, Any

def get_config() -> Dict[str, Any]:
    """
    Возвращает конфигурацию приложения
    
    Returns:
        Dict[str, Any]: Словарь с настройками
    """
    config = {
        'USE_TESTNET': os.getenv('USE_TESTNET', 'false').lower() == 'true',  # По умолчанию используем основную сеть
        'LOG_LEVEL': os.getenv('LOG_LEVEL', 'INFO'),
        'WEBSOCKET_PING_INTERVAL': 30,  # секунды
        'WEBSOCKET_PING_TIMEOUT': 15,   # секунды
        'MAX_RECONNECT_ATTEMPTS': 5,
        'RECONNECT_DELAY': 5,           # секунды
        'QUEUE_SIZE': 1000,
    }
    
    return config 