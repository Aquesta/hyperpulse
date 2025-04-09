import asyncio
from hyperliquid.info import Info
from hyperliquid.utils import constants
from collections import deque
from datetime import datetime
from config.logging_config import setup_logger
import streamlit as st

logger = setup_logger(__name__)
trade_data = deque(maxlen=1000)

from datetime import datetime

def on_message(message):
    # logger.info(f"СООБЩЕНИЕ ОТ СЕРВЕРА: {message}")
    if 'data' in message and isinstance(message['data'], list):
        for trade in message['data']:
            try:
                users = trade.get('users', [])
                buyer = seller = None

                if trade['side'] == 'B':  # Покупка
                    buyer = users[0] if len(users) > 0 else None
                    seller = users[1] if len(users) > 1 else None
                elif trade['side'] == 'A':  # Продажа
                    seller = users[0] if len(users) > 0 else None
                    buyer = users[1] if len(users) > 1 else None

                trade_info = {
                    'timestamp': datetime.fromtimestamp(trade['time'] / 1000),
                    'side': trade['side'],
                    'price': float(trade['px']),
                    'volume': float(trade['sz']),
                    'total': float(trade['sz']) * float(trade['px']),
                    'coin': trade['coin'],
                    'buyer': buyer,
                    'seller': seller
                }

                trade_data.append(trade_info)
                # logger.info(f"Trade received: {trade_info}")
            except Exception as e:
                logger.error(f"Ошибка при обработке трейда: {e}")

def start_websocket(coin):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        info = Info(base_url=constants.MAINNET_API_URL, skip_ws=False)
        subscription = {"type": "trades", "coin": coin}
        info.subscribe(subscription, on_message)
        loop.run_forever()
    except Exception as e:
        print(f"Ошибка в WebSocket: {e}")
    finally:
        loop.close()