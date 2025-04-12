import streamlit as st
from streamlit_autorefresh import st_autorefresh
import plotly.graph_objects as go
import threading
import time
from core.websocket_client import start_websocket, close_websocket, connect_websocket, queue_logger
from core.data_processor import process_trade_data, get_user_statistics, get_cumulative_user_statistics
import pandas as pd
import pyperclip
from config.logging_config import setup_logger
import gc
import numpy as np
from datetime import datetime, timedelta
import queue
import traceback

logger = setup_logger(__name__)

# Настройки интерфейса
REFRESH_INTERVAL = 1  # секунды для обновления данных
UPDATE_INTERVAL = 1  # секунды

# Инициализация состояния сессии
if 'connected' not in st.session_state:
    st.session_state.connected = False
if 'last_update' not in st.session_state:
    st.session_state.last_update = None
if 'status_message' not in st.session_state:
    st.session_state.status_message = "Ожидание подключения..."
if 'last_error' not in st.session_state:
    st.session_state.last_error = None
if 'reconnect_attempts' not in st.session_state:
    st.session_state.reconnect_attempts = 0

@st.cache_data(ttl=REFRESH_INTERVAL, show_spinner=False, max_entries=5)
def generate_volume_delta_chart(volume_data, coin):
    """Генерация графика разницы объемов продаж и покупок"""
    if volume_data is None or volume_data.empty:
        return None

    # Расчет дельты объемов
    volume_data = volume_data.copy()
    if 'B' not in volume_data.columns:
        volume_data['B'] = 0
    if 'A' not in volume_data.columns:
        volume_data['A'] = 0
    
    volume_data['delta'] = volume_data['B'] - volume_data['A']
    
    # Подготовка данных для графика
    x = volume_data.index
    y = volume_data['delta']
    
    # Создание графика
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=x, 
        y=y, 
        mode='lines',
        line=dict(color='rgba(0, 100, 255, 0.8)', width=2),
        fill='tozeroy',
        fillcolor='rgba(0, 100, 255, 0.1)',
        name='Дельта объемов'
    ))
    
    fig.update_layout(
        title=f'Дельта объемов для {coin} (Покупка - Продажа)',
        xaxis_title='Время',
        yaxis_title='Объем (USDC)',
        height=500,
        margin=dict(l=50, r=20, t=40, b=20),
        font=dict(size=14),
        yaxis=dict(
            tickfont=dict(size=12),
            gridcolor='rgba(128, 128, 128, 0.1)',
            zerolinecolor='rgba(128, 128, 128, 0.2)'
        ),
        xaxis=dict(
            tickfont=dict(size=12),
            gridcolor='rgba(128, 128, 128, 0.1)'
        ),
        paper_bgcolor='rgba(0,0,0,0)',
        plot_bgcolor='rgba(0,0,0,0)'
    )
    
    return fig

def format_user_stats(user_stats):
    """Форматирование статистики пользователей для отображения"""
    if user_stats is None or user_stats.empty:
        return pd.DataFrame()
    
    # Создаем копию и добавляем производные метрики
    stats = user_stats.copy()
    stats['net_volume'] = stats['buy_volume'] - stats['sell_volume']
    stats['total_volume'] = stats['buy_volume'] + stats['sell_volume']
    stats['total_trades'] = stats['buy_count'] + stats['sell_count']
    
    # Сортировка по общему объему сделок
    stats = stats.sort_values('total_volume', ascending=False)
    
    # Форматирование чисел с долларом и разделителями
    for col in ['buy_volume', 'sell_volume', 'net_volume', 'total_volume']:
        stats[col] = stats[col].apply(lambda x: f"${int(x):,}")
    
    # Добавляем HTML-ссылки для пользователей
    stats['user'] = stats['user'].apply(
        lambda x: f'<a href="https://hypurrscan.io/address/{x}" target="_blank">{x}</a>'
    )
    
    # Переименование колонок для отображения
    stats = stats.rename(columns={
        'user': 'Пользователь',
        'buy_count': 'Покупки',
        'sell_count': 'Продажи',
        'buy_volume': 'Объем покупок',
        'sell_volume': 'Объем продаж',
        'net_volume': 'Чистый объем',
        'total_volume': 'Общий объем',
        'total_trades': 'Всего сделок'
    })
    
    # Сортируем колонки
    column_order = [
        'Пользователь', 
        'Общий объем',
        'Чистый объем', 
        'Объем покупок', 
        'Объем продаж', 
        'Покупки', 
        'Продажи', 
        'Всего сделок'
    ]
    stats = stats[column_order]
    
    return stats

def update_dashboard():
    """Обновление информации на дашборде"""
    try:
        st.session_state.last_update = datetime.now()
        
        # Обработка данных для выбранной монеты
        coin = st.session_state.coin
        time_interval = st.session_state.time_interval
        
        with st.container():
            # Статус подключения и последнее обновление
            col1, col2, col3 = st.columns([1, 1.5, 1.5])
            
            with col1:
                st.metric("Статус", "✅ Подключено" if st.session_state.connected else "❌ Отключено")
            
            with col2:
                st.write("**Последнее обновление:**")
                st.write(st.session_state.last_update.strftime('%H:%M:%S'))
            
            with col3:
                st.write(f"**Монета:** {coin}")
                st.write(f"**Интервал:** {time_interval} мин")
            
            if st.session_state.last_error:
                st.error(f"Ошибка: {st.session_state.last_error}")
        
        # Процесс обработки с индикатором загрузки
        with st.spinner(f"Обработка данных для {coin}..."):
            try:
                data, volume_by_time, sentiment, count_sentiment = process_trade_data(time_interval, coin)
            except Exception as e:
                logger.error(f"Ошибка при обработке данных: {e}")
                st.error(f"Ошибка при обработке данных: {e}")
                data, volume_by_time, sentiment, count_sentiment = None, None, None, None
        
        if data is None:
            st.warning(f"Недостаточно данных для {coin} в выбранном интервале.")
            return
        
        # Отображение настроения рынка
        try:
            col1, col2 = st.columns(2)
            with col1:
                st.markdown('<div style="font-size: 1.2em;">Настроение рынка (% объема покупок)</div>', unsafe_allow_html=True)
                st.metric(
                    label="Volume Sentiment",
                    value=f"{sentiment:.1f}%",
                    delta=f"{sentiment - 50:.1f}" if sentiment != 50 else None,
                    label_visibility="hidden"
                )
            with col2:
                st.markdown('<div style="font-size: 1.2em;">Настроение рынка (% количества покупок)</div>', unsafe_allow_html=True)
                st.metric(
                    label="Count Sentiment", 
                    value=f"{count_sentiment:.1f}%",
                    delta=f"{count_sentiment - 50:.1f}" if count_sentiment != 50 else None,
                    label_visibility="hidden"
                )
        except Exception as e:
            logger.error(f"Ошибка при отображении настроения рынка: {e}")
            st.error(f"Ошибка при отображении настроения рынка: {e}")
            
        # График дельты объемов на всю ширину
        st.subheader("Дельта объемов")
        try:
            volume_chart = generate_volume_delta_chart(volume_by_time, coin)
            if volume_chart:
                timestamp_str = datetime.now().strftime("%H%M%S%f")
                st.plotly_chart(volume_chart, use_container_width=True, key=f"volume_delta_{coin}_{timestamp_str}")
            else:
                st.info("Недостаточно данных для построения графика.")
        except Exception as e:
            logger.error(f"Ошибка при создании графика объемов: {e}")
            st.error(f"Ошибка при создании графика объемов: {e}")
        
        # Накопительная статистика пользователей
        st.subheader("Накопительная статистика пользователей")
        try:
            cumulative_stats = get_cumulative_user_statistics(coin)
            if cumulative_stats is not None and not cumulative_stats.empty:
                formatted_stats = format_user_stats(cumulative_stats)
                if not formatted_stats.empty:
                    # CSS стили для таблицы
                    st.markdown("""
                    <style>
                        .full-width {
                            width: 100%;
                            margin: 0;
                            padding: 0;
                        }
                        .full-width table {
                            width: 100% !important;
                            min-width: 100% !important;
                        }
                        .full-width th, .full-width td {
                            white-space: nowrap;
                            text-align: left;
                            padding: 8px 12px;
                        }
                    </style>
                    """, unsafe_allow_html=True)
                    
                    # Отображение таблицы
                    st.markdown(
                        f'<div class="full-width">{formatted_stats.to_html(escape=False, index=False)}</div>',
                        unsafe_allow_html=True
                    )
                else:
                    st.info("Нет данных для таблицы накопительной статистики.")
            else:
                st.info("Нет накопительных данных по пользователям.")
        except Exception as e:
            logger.error(f"Ошибка при отображении накопительной статистики: {e}")
            st.error(f"Ошибка при отображении накопительной статистики: {e}")
        
        # Обновление статуса
        st.session_state.status_message = f"Данные успешно обновлены в {st.session_state.last_update.strftime('%H:%M:%S')}"
        st.session_state.last_error = None
        st.session_state.reconnect_attempts = 0
        
    except Exception as e:
        st.session_state.last_error = str(e)
        logger.error(f"Ошибка при обновлении дашборда: {e}")
        logger.error(traceback.format_exc())
        st.error(f"Ошибка при обновлении данных: {e}")

def show_dashboard():
    """Основная функция интерфейса"""
    st.set_page_config(
        page_title="HyperPulse - Аналитика торгов HyperLiquid",
        page_icon="📊",
        layout="wide",
    )
    
    # Настройка автоматического обновления страницы
    st_autorefresh(interval=REFRESH_INTERVAL * 1000, key="autorefresh")
    
    st.title("📊 HyperPulse - Аналитика торгов HyperLiquid")
    
    # Боковая панель для настроек
    with st.sidebar:
        st.header("Настройки")
        
        # Выбор монеты
        coin_options = ["BTC", "ETH", "SOL"]
        if 'coin' not in st.session_state:
            st.session_state.coin = "BTC"
        st.session_state.coin = st.selectbox(
            "Выберите монету:", 
            options=coin_options, 
            index=coin_options.index(st.session_state.coin),
            key="coin_selector"
        )
        
        # Выбор временного интервала
        interval_options = [5, 10, 15, 30]
        if 'time_interval' not in st.session_state:
            st.session_state.time_interval = 5
        st.session_state.time_interval = st.selectbox(
            "Временной интервал (мин):", 
            options=interval_options,
            index=interval_options.index(st.session_state.time_interval),
            key="interval_selector"
        )
        
        # Кнопка подключения/отключения
        if not st.session_state.connected:
            if st.button("Подключиться к WebSocket", key="connect_button", use_container_width=True):
                try:
                    connect_websocket()
                    st.session_state.connected = True
                    st.session_state.status_message = "Подключение к WebSocket установлено"
                except Exception as e:
                    st.session_state.last_error = str(e)
                    logger.error(f"Ошибка при подключении к WebSocket: {e}")
                    st.error(f"Ошибка при подключении: {e}")
        else:
            if st.button("Отключиться", key="disconnect_button", use_container_width=True):
                close_websocket()
                st.session_state.connected = False
                st.session_state.status_message = "Отключено от WebSocket"
                
        # Кнопка обновления
        if st.button("Обновить данные", key="refresh_button", use_container_width=True):
            update_dashboard()
            
        # Статус и сообщения
        st.write("---")
        st.write(f"Статус: {st.session_state.status_message}")
        
        # Обработка логов из очереди
        try:
            while not queue_logger.empty():
                log = queue_logger.get_nowait()
                st.session_state.status_message = log
                logger.info(log)
        except queue.Empty:
            pass
            
    # Основная область - дашборд
    if st.session_state.connected:
        update_dashboard()
    
    # Информация об автоматическом обновлении
    st.write(f"Автоматическое обновление каждые {REFRESH_INTERVAL} секунд.")

# Алиас для совместимости
main = show_dashboard

if __name__ == "__main__":
    show_dashboard()
