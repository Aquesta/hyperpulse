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
REFRESH_INTERVAL = 3  # секунды для обновления данных
UPDATE_INTERVAL = 2  # секунды
MAX_USERS_IN_TABLE = 10

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

# Кэширование графиков для плавного обновления
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
        line=dict(color='blue', width=2),
        fill='tozeroy',
        fillcolor='rgba(0, 100, 255, 0.2)',
        name='Дельта объемов'
    ))
    
    fig.update_layout(
        title=f'Дельта объемов для {coin} (Покупка - Продажа)',
        xaxis_title='Время',
        yaxis_title='Объем (USDC)',
        height=400,
        margin=dict(l=0, r=0, t=40, b=0)
    )
    
    return fig

@st.cache_data(ttl=REFRESH_INTERVAL, show_spinner=False, max_entries=5)
def generate_trade_count_chart(user_stats, title="Количество сделок по пользователям", key_suffix=""):
    """Генерация графика количества сделок по пользователям"""
    if user_stats is None or user_stats.empty:
        logger.warning(f"Пустой DataFrame для графика пользователей ({key_suffix})")
        return None
    
    try:
        # Проверяем наличие необходимых колонок
        required_columns = ['user', 'buy_count', 'sell_count']
        if not all(col in user_stats.columns for col in required_columns):
            missing_cols = [col for col in required_columns if col not in user_stats.columns]
            logger.warning(f"Отсутствуют необходимые колонки для графика ({key_suffix}): {missing_cols}")
            return None

        # Создаем копию для безопасности
        df = user_stats.copy()
        
        # Проверяем, есть ли данные после фильтрации
        if df.empty:
            logger.warning(f"После фильтрации данных для графика не осталось записей ({key_suffix})")
            return None
        
        # Убеждаемся, что числовые колонки имеют числовой тип
        for col in ['buy_count', 'sell_count']:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0).astype(int)
        
        # Сортировка и ограничение количества записей
        df['total'] = df['buy_count'] + df['sell_count']
        df = df.sort_values(by='total', ascending=False).head(MAX_USERS_IN_TABLE)
        
        # Если после всех преобразований данных нет, возвращаем None
        if df.empty:
            logger.warning(f"После сортировки и ограничения не осталось данных для графика ({key_suffix})")
            return None
        
        # Создание графика
        fig = go.Figure()
        
        # Добавляем столбцы покупок
        fig.add_trace(go.Bar(
            y=df['user'],
            x=df['buy_count'],
            name='Покупки',
            orientation='h',
            marker=dict(color='rgba(0, 128, 0, 0.7)')
        ))
        
        # Добавляем столбцы продаж
        fig.add_trace(go.Bar(
            y=df['user'],
            x=df['sell_count'],
            name='Продажи',
            orientation='h',
            marker=dict(color='rgba(255, 0, 0, 0.7)')
        ))
        
        # Настройка макета графика
        fig.update_layout(
            title=title,
            xaxis_title='Количество сделок',
            barmode='group',
            height=400,
            margin=dict(l=0, r=0, t=40, b=0)
        )
        
        return fig
        
    except Exception as e:
        logger.error(f"Ошибка при создании графика пользователей ({key_suffix}): {e}")
        return None

def format_user_stats(user_stats, title_prefix=""):
    """Форматирование статистики пользователей для отображения"""
    if user_stats is None or user_stats.empty:
        return pd.DataFrame()
    
    # Создаем копию и добавляем производные метрики
    stats = user_stats.copy()
    stats['net_volume'] = stats['buy_volume'] - stats['sell_volume']
    stats['total_trades'] = stats['buy_count'] + stats['sell_count']
    
    # Сортировка по общему количеству сделок
    stats = stats.sort_values('total_trades', ascending=False).head(MAX_USERS_IN_TABLE)
    
    # Форматирование чисел
    stats['buy_volume'] = stats['buy_volume'].round(2)
    stats['sell_volume'] = stats['sell_volume'].round(2)
    stats['net_volume'] = stats['net_volume'].round(2)
    
    # Переименование колонок для отображения
    stats = stats.rename(columns={
        'user': 'Пользователь',
        'buy_count': 'Покупки',
        'sell_count': 'Продажи',
        'buy_volume': 'Объем покупок',
        'sell_volume': 'Объем продаж',
        'net_volume': 'Чистый объем',
        'total_trades': 'Всего сделок'
    })
    
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
            col1, col2, col3 = st.columns([1, 2, 1])
            
            with col1:
                st.metric("Статус", "Подключено" if st.session_state.connected else "Отключено")
            
            with col2:
                st.write(f"Последнее обновление: {st.session_state.last_update.strftime('%H:%M:%S')}")
                st.write(f"Монета: {coin}, Интервал: {time_interval} мин")
            
            with col3:
                if st.session_state.last_error:
                    st.error(f"Ошибка: {st.session_state.last_error}")
        
        # Процесс обработки с индикатором загрузки
        with st.spinner(f"Обработка данных для {coin}..."):
            try:
                data, volume_by_time, sentiment = process_trade_data(time_interval, coin)
            except Exception as e:
                logger.error(f"Ошибка при обработке данных: {e}")
                st.error(f"Ошибка при обработке данных: {e}")
                data, volume_by_time, sentiment = None, None, None
        
        if data is None:
            st.warning(f"Недостаточно данных для {coin} в выбранном интервале.")
            return
        
        # Отображение настроения рынка
        try:
            st.metric(
                "Настроение рынка (% объема покупок)", 
                f"{sentiment:.1f}%",
                delta=f"{sentiment - 50:.1f}" if sentiment != 50 else None
            )
        except Exception as e:
            logger.error(f"Ошибка при отображении настроения рынка: {e}")
            st.error(f"Ошибка при отображении настроения рынка: {e}")
            
        # Графики и таблицы
        col1, col2 = st.columns(2)
        
        # График дельты объемов
        with col1:
            st.subheader("Дельта объемов")
            try:
                volume_chart = generate_volume_delta_chart(volume_by_time, coin)
                if volume_chart:
                    # Добавляем timestamp к ключу для уникальности
                    timestamp_str = datetime.now().strftime("%H%M%S%f")
                    st.plotly_chart(volume_chart, use_container_width=True, key=f"volume_delta_{coin}_{timestamp_str}")
                else:
                    st.info("Недостаточно данных для построения графика.")
            except Exception as e:
                logger.error(f"Ошибка при создании графика объемов: {e}")
                st.error(f"Ошибка при создании графика объемов: {e}")
        
        # Статистика пользователей (текущая)
        with col2:
            st.subheader("Статистика пользователей (текущий интервал)")
            try:
                user_stats = get_user_statistics(data)
                
                # Добавляем лог для отладки
                logger.info(f"Получены данные статистики пользователей: {len(user_stats) if user_stats is not None else 'None'} записей")
                
                if user_stats is not None and not user_stats.empty:
                    # Проверим содержимое user_stats
                    logger.info(f"Колонки user_stats: {list(user_stats.columns)}")
                    logger.info(f"Пример данных user_stats: {user_stats.head(1).to_dict('records')}")
                    
                    # Удостоверимся, что нужные колонки существуют перед сортировкой
                    required_columns = ['user', 'buy_count', 'sell_count']
                    if all(col in user_stats.columns for col in required_columns):
                        user_chart = generate_trade_count_chart(user_stats, "Сделки за текущий интервал", "current")
                        if user_chart:
                            # Добавляем timestamp к ключу для уникальности
                            timestamp_str = datetime.now().strftime("%H%M%S%f")
                            st.plotly_chart(user_chart, use_container_width=True, key=f"user_chart_current_{coin}_{timestamp_str}")
                        else:
                            st.info("Не удалось создать график статистики пользователей.")
                    else:
                        missing_cols = [col for col in required_columns if col not in user_stats.columns]
                        st.warning(f"Отсутствуют необходимые колонки: {missing_cols}")
                else:
                    st.info("Нет данных по пользователям за текущий интервал.")
            except Exception as e:
                logger.error(f"Ошибка при создании графика пользователей: {e}")
                st.error(f"Ошибка при создании графика пользователей: {e}")
        
        # Накопительная статистика пользователей
        st.subheader("Накопительная статистика пользователей")
        try:
            cumulative_stats = get_cumulative_user_statistics(coin)
            if cumulative_stats is not None and not cumulative_stats.empty:
                # Показываем график накопительной статистики
                col3, col4 = st.columns(2)
                
                with col3:
                    cumulative_chart = generate_trade_count_chart(cumulative_stats, "Сделки (всего)", "cumulative")
                    if cumulative_chart:
                        timestamp_str = datetime.now().strftime("%H%M%S%f")
                        st.plotly_chart(cumulative_chart, use_container_width=True, key=f"user_chart_cumulative_{coin}_{timestamp_str}")
                    else:
                        st.info("Не удалось создать график накопительной статистики.")
                
                with col4:
                    # Преобразуем накопительную статистику для таблицы
                    formatted_cumulative_stats = format_user_stats(cumulative_stats, "Накопительная ")
                    if not formatted_cumulative_stats.empty:
                        # Добавляем timestamp к ключу для уникальности
                        timestamp_str = datetime.now().strftime("%H%M%S%f")
                        st.dataframe(formatted_cumulative_stats, use_container_width=True, key=f"cumulative_stats_table_{coin}_{timestamp_str}")
                    else:
                        st.info("Нет данных для таблицы накопительной статистики.")
            else:
                st.info("Нет накопительных данных по пользователям.")
        except Exception as e:
            logger.error(f"Ошибка при отображении накопительной статистики: {e}")
            st.error(f"Ошибка при отображении накопительной статистики: {e}")
        
        # Таблица статистики пользователей (текущий интервал)
        st.subheader("Детальная статистика пользователей (текущий интервал)")
        try:
            formatted_stats = format_user_stats(user_stats)
            if not formatted_stats.empty:
                # Добавляем timestamp к ключу для уникальности
                timestamp_str = datetime.now().strftime("%H%M%S%f")
                st.dataframe(formatted_stats, use_container_width=True, key=f"user_stats_table_{coin}_{timestamp_str}")
            else:
                st.info("Нет данных для таблицы.")
        except Exception as e:
            logger.error(f"Ошибка при создании таблицы статистики: {e}")
            st.error(f"Ошибка при создании таблицы статистики: {e}")
            
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
            if st.button("Подключиться к WebSocket", key="connect_button"):
                try:
                    connect_websocket()
                    st.session_state.connected = True
                    st.session_state.status_message = "Подключение к WebSocket установлено"
                except Exception as e:
                    st.session_state.last_error = str(e)
                    logger.error(f"Ошибка при подключении к WebSocket: {e}")
                    st.error(f"Ошибка при подключении: {e}")
        else:
            if st.button("Отключиться", key="disconnect_button"):
                st.session_state.connected = False
                st.session_state.status_message = "Отключено от WebSocket"
                
        # Кнопка обновления
        if st.button("Обновить данные", key="refresh_button"):
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
