import streamlit as st
from streamlit_autorefresh import st_autorefresh
import plotly.graph_objects as go
import threading
from core.websocket_client import start_websocket
from core.data_processor import process_trade_data
import pandas as pd
import pyperclip

def show_dashboard():
    st.set_page_config(page_title="HyperPulse", layout="wide")
    st.title("HyperPulse: Анализ сделок на HyperLiquid")

    # Автоматическое обновление каждые 2 секунды
    st_autorefresh(interval=2000, key="auto_refresh")

    # Инициализация состояния
    if 'connection_status' not in st.session_state:
        st.session_state.connection_status = "Ожидание подключения..."
    if 'websocket_thread' not in st.session_state:
        st.session_state.websocket_thread = None

    # Выбор параметров
    col1, col2 = st.columns(2)
    with col1:
        coin = st.selectbox("Выберите криптовалюту", ["BTC", "ETH", "SOL"], key="coin")
    with col2:
        time_interval = st.selectbox("Интервал (мин)", [1, 2, 3, 5], key="interval")

    st.write(f"Статус: {st.session_state.connection_status}")

    # Запуск WebSocket-потока
    if st.session_state.websocket_thread is None or not st.session_state.websocket_thread.is_alive():
        try:
            st.session_state.websocket_thread = threading.Thread(
                target=start_websocket,
                args=(coin,),
                daemon=True
            )
            st.session_state.websocket_thread.start()
            st.session_state.connection_status = "Подключено"
        except Exception as e:
            st.session_state.connection_status = f"Ошибка подключения: {e}"
            st.error(f"Не удалось подключиться к вебсокету: {e}")

    # Получение и отображение данных
    df, volume_by_time, sentiment = process_trade_data(time_interval, coin)

    if df is not None and not df.empty and volume_by_time is not None and sentiment is not None:
        buy_volume = volume_by_time['B'] if 'B' in volume_by_time else []
        sell_volume = volume_by_time['A'] if 'A' in volume_by_time else []
        delta_volume = buy_volume - sell_volume

        # Общие метрики
        st.metric("Настроение рынка", f"{sentiment:.1f}%", delta=f"{sentiment - 50:.1f}%")
        st.write(f"Объем покупок: ${df[df['side'] == 'B']['total'].sum():,.2f}")
        st.write(f"Объем продаж: ${df[df['side'] == 'A']['total'].sum():,.2f}")
        st.write(f"Объем данных: {len(df)} записей")

        # 📑 Tabs: Графики и Таблица
        tab1, tab2 = st.tabs(["📈 Графики", "📋 Таблица кошельков"])

        with tab1:
            # 📊 Дельта объема
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=volume_by_time.index,
                y=delta_volume,
                mode='lines',
                name='Покупки',
                line=dict(color='yellowgreen')
            ))
            fig.update_layout(title="Дельта объема сделок в USD", xaxis_title="Время", yaxis_title="Дельта (USD)")
            st.plotly_chart(fig, use_container_width=True)

            # 📊 Кол-во сделок
            count_by_time = df.groupby(['time_bin', 'side']).size().unstack().fillna(0)

            fig_count = go.Figure()
            fig_count.add_trace(go.Scatter(
                x=count_by_time.index,
                y=count_by_time.get('B', []),
                mode='lines',
                name='Покупки (кол-во)',
                line=dict(color='yellowgreen')
            ))
            fig_count.add_trace(go.Scatter(
                x=count_by_time.index,
                y=count_by_time.get('A', []),
                mode='lines',
                name='Продажи (кол-во)',
                line=dict(color='lightblue')
            ))
            fig_count.update_layout(title="Количество сделок", xaxis_title="Время", yaxis_title="Сделки (шт)")
            st.plotly_chart(fig_count, use_container_width=True)

        with tab2:
            st.subheader("📋 Активность пользователей")

            # Группировка и агрегация
            buyers = df[df['side'] == 'B'].groupby('buyer').agg(
                buy_count=('buyer', 'count'),
                buy_volume=('total', 'sum')
            ).reset_index().rename(columns={'buyer': 'user'})

            sellers = df[df['side'] == 'A'].groupby('seller').agg(
                sell_count=('seller', 'count'),
                sell_volume=('total', 'sum')
            ).reset_index().rename(columns={'seller': 'user'})

            user_stats = pd.merge(buyers, sellers, on='user', how='outer').fillna(0)
            user_stats[['buy_count', 'sell_count']] = user_stats[['buy_count', 'sell_count']].astype(int)
            user_stats['buy_volume'] = user_stats['buy_volume'].apply(lambda x: f"${x:,.0f}")
            user_stats['sell_volume'] = user_stats['sell_volume'].apply(lambda x: f"${x:,.0f}")

            # Для сортировки используем копию с float значениями
            user_stats_sorted = user_stats.copy()
            user_stats_sorted['buy_volume_raw'] = user_stats_sorted['buy_volume'].replace('[\\$,]', '', regex=True).astype(float)
            user_stats_sorted['sell_volume_raw'] = user_stats_sorted['sell_volume'].replace('[\\$,]', '', regex=True).astype(float)

            user_stats_sorted = user_stats_sorted.sort_values(
                by=['buy_count', 'sell_count', 'buy_volume_raw', 'sell_volume_raw'],
                ascending=[False, False, False, False]
            )

            # Отображение заголовков таблицы
            header_col1, header_col2, header_col3, header_col4, header_col5, header_col6, header_col7 = st.columns([2, 2, 2, 2, 2, 1, 1])
            header_col1.text("Wallet")
            header_col2.text("Buy Count")
            header_col3.text("Buy Total")
            header_col4.text("Sell Count")
            header_col5.text("Sell Total")
            header_col6.text("Copy wallet")  # Для кнопки "Копировать"
            header_col7.text("Go to HypurrScan")  # Для кнопки "Перейти"

            # Отображение таблицы с кнопками для копирования и перехода
            for index, row in user_stats_sorted.iterrows():
                col1, col2, col3, col4, col5, col6, col7 = st.columns([2, 2, 2, 2, 2, 1, 1])
                col1.text(row['user'])
                col2.text(row['buy_count'])
                col3.text(row['buy_volume'])
                col4.text(row['sell_count'])
                col5.text(row['sell_volume'])
                
                # Кнопка для копирования
                if col6.button("Копировать", key=f"copy_{index}"):
                    pyperclip.copy(row['user'])
                    st.success(f"Скопировано: {row['user']}")
                
                # Кнопка для перехода
                address_url = f"https://hypurrscan.io/address/{row['user']}"
                col7.markdown(f"[Перейти]({address_url})", unsafe_allow_html=True)
    else:
        st.warning("Нет данных для отображения.")
