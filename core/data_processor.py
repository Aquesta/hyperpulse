import pandas as pd
from datetime import datetime, timedelta
from core.websocket_client import trade_data

# глобальная переменная для хранения накопленных данных
historical_df = pd.DataFrame()

def process_trade_data(time_interval, coin):
    global historical_df

    # Все новые данные за последнюю минуту
    new_data = [t for t in trade_data if t['coin'] == coin]
    if not new_data:
        print(f"No trades found for coin {coin}.")
        return None, None, None

    # Преобразуем в DataFrame
    new_df = pd.DataFrame(new_data)

    # Объединяем с историей
    if not historical_df.empty:
        historical_df = pd.concat([historical_df, new_df])
        historical_df.drop_duplicates(inplace=True)
        historical_df.reset_index(drop=True, inplace=True)
    else:
        historical_df = new_df

    # Оставляем только записи из нужного временного окна
    now = datetime.now()
    time_threshold = now - timedelta(minutes=time_interval)
    df = historical_df[historical_df['timestamp'] >= time_threshold]

    if df.empty:
        return None, None, None
    
    # print(df)

    buy_volume = df[df['side'] == 'B']['total'].sum()
    sell_volume = df[df['side'] == 'A']['total'].sum()
    total = buy_volume + sell_volume
    sentiment = (buy_volume / total * 100) if total > 0 else 50

    df['time_bin'] = df['timestamp'].dt.floor('1s')
    volume_by_time = df.groupby(['time_bin', 'side'])['total'].sum().unstack().fillna(0)

    return df, volume_by_time, sentiment