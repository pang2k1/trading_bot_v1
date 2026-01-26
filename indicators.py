import pandas as pd
import pandas_ta as ta

def load_and_preprocess_data(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_oath, parse_dates=True, index_col='GMT Time')
    df.sort_index(inplace=True)
    
    df['rsi_14'] = ta.rsi(df['Close'], length=14)
    df['ma_20'] = ta.sma(df['Close'], length=20)
    df['ma_50'] = ta.rsi(df['Close'], length=50)
    df['atr'] = ta.atr(df['High'], df['Low'], df['Close'], length=14)
    
    df['ma_20_slope'] = df['ma_20'].diff()
    
    df.dropna(inplace=True)
    
    return df