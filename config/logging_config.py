import logging
import os
from datetime import datetime
import sys

# Создаем папку для логов, если ее нет
LOGS_DIR = "logs"
if not os.path.exists(LOGS_DIR):
    os.makedirs(LOGS_DIR)

# Текущая дата для имени файла лога
current_date = datetime.now().strftime("%Y-%m-%d")
LOG_FILE = os.path.join(LOGS_DIR, f"hyperpulse_{current_date}.log")

# Цветовое форматирование сообщений в консоли
class ColoredFormatter(logging.Formatter):
    """Formatter для цветного вывода логов в консоль"""
    
    COLORS = {
        'DEBUG': '\033[94m',    # Синий
        'INFO': '\033[92m',     # Зеленый
        'WARNING': '\033[93m',  # Желтый
        'ERROR': '\033[91m',    # Красный
        'CRITICAL': '\033[91m\033[1m',  # Красный жирный
        'RESET': '\033[0m'      # Сброс форматирования
    }
    
    def format(self, record):
        log_message = super().format(record)
        level_name = record.levelname
        
        # Добавляем цвет только если вывод в консоль для терминала
        # Упрощенная версия проверки без доступа к хэндлерам логгера
        if level_name in self.COLORS:
            log_message = f"{self.COLORS[level_name]}{log_message}{self.COLORS['RESET']}"
        
        return log_message

def setup_logger(name=__name__, level=logging.INFO):
    """
    Настраивает и возвращает логгер с указанным именем
    
    Args:
        name (str): Имя логгера
        level (int): Уровень логирования (logging.DEBUG, logging.INFO и т.д.)
        
    Returns:
        logging.Logger: Настроенный логгер
    """
    logger = logging.getLogger(name)
    
    # Если у логгера уже есть обработчики, не добавляем новые
    if logger.handlers:
        return logger
    
    # Устанавливаем уровень логирования
    logger.setLevel(level)
    
    # Форматтеры для вывода
    console_formatter = ColoredFormatter(
        "%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%H:%M:%S"
    )
    file_formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(name)s - %(filename)s:%(lineno)d - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    
    # Обработчик для вывода в консоль
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(console_formatter)
    console_handler.setLevel(level)
    
    # Обработчик для вывода в файл
    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setFormatter(file_formatter)
    file_handler.setLevel(level)
    
    # Добавляем обработчики к логгеру
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    
    return logger