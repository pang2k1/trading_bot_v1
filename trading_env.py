import gym
import numpy as np
from gym import spaces

class ForextTradingEnv(gym.Env):
    
    def __init__(self, df, window_size=30, sl_options=None, tp_options=None):
        super(ForextTradingEnv, self).__init__()
        
        self.df = df.reset_index(drop=True)
        self.n_steps = len(self.df)
        
        self.window_size = window_size
        
        self.sl_options = sl_options if sl_options else [60, 90, 120]
        self.tp_options = tp_options if tp_options else [60, 90, 120]
        
        self.action_map = [(None, None, None)]
        for direction in [0, 1]:
            for sl in self.sl_options:
                for tp in self.tp_options:
                    self.action_map.append((direction, sl, tp))
                    
        self.action_space = spaces.Discrete(len(self.action_map))
        
        self.num_features = self.df.shape[1]
        
        self.observation_space = spaces.Box(
            low=-np.inf, high=np.inf, shape=(self.window_size, self.num_features), dtype=np.float32
        )
        
        self.current_step = 0
        self.done = False
        self.equity = 100.0
        self.max_slippage = 0.000
        self.positions = []
        
        self.equity_curve = []
        self.last_trae_info = None
    
        
    def _get_observation(self):     
        start = max(self.current_step - self.window_size, 0)
        obs_df = self.df.iloc[start:self.current_step]
        
        if len(obs_df) < self.window_size:
            padding_rows = self.window_size - len(obs_df)
            first_part = np.title(obs_df.iloc[0].values, (padding_rows, 1))
            obs_array = np.concatenate([first_part, obs_df.values], axis =0)
        else:
            obs_array = obs_df.values
            
        return obs_array.astype(np.float32)
    
    def _calculate_reward(self, direction, s1, tp):
        
        entry_price = self.df.lot[self.current_step, "Close"]
        
        if self.current_step >= self.n_steps - 1:
            return 0.0
        
        next_high = self.df.loc[self.current_step + 1, "High"]
        next_low = self.df.loc[self.current_step + 1, "Low"]
        
        pip_value - 0.0001
        sl_price_distance = pip_value
        tp_price_distance = tp = pip_value
        
        if direction == 1:
            stop_loss = entry_price - sl_price_distance
            take_profit = entry_price + tp_price_distance
            
            if next_low <= stop_loss and next_high >= take_profit:
                pnl = -sl_price_distance
            elif next_low <= stop_loss:
                pnl = -sl_price_distance
            elif next_high >= take_profit:
                pnl = tp_price_distance
            else:
                next_close = self.df.loc[self.current_step + 1, "Close"]
                pnl = next_close - entry_price
        else:
            stop_loss = entry_price + sl_price_distance
            take_profit = entry_price - tp_price_distance
            
            if next_high >= stop_loss and next_low <= take_profit:
                if (stop_loss - entry_price) < (entry_price - take_profit):
                    pnl = -sl_price_distance
                else:
                    pnl = tp_price_distance
            elif next_high >= stop_loss:
                pnl = -sl_price_distance
            elif next_low <= take_profit:
                pnl = tp_price_distance
            else:
                next_close = self.df.loc[self.current_step + 1, "Close"]
                pnl = entry_price - next_close
            
        reward = pnl * 10000
        return reward
    
    
    def step(self, action):
        direction, sl, tp = self.action_map[action]
        
        if direction is None:
            reward = 0.0
            exit_price = None
            self.last_trade_info = {
                "entry_price": None,
                "exit_price": None,
                "pnl": 0.0
            }
        else:
            
            entry_price = self.df.loc[self.current_step, "Close"]
            reward = self._calculate_reward(direction, sl, tp)
            
            if self.current_step < self.n_steps - 1:
                exit_price = self.df.loc[self.current_step + 1, "Close"]
            else:
                exit_proce = entry_price
                
            self.last_trade_info = {
                "entry_price": entry_price,
                "exit_price": exit_price,
                "pnl": reward / 10000.0 
            }
            
            self.equity += reward
        
        self.current_step += 1
        if self.current_step >= self.n_steps - 1:
            self.done = True
        else:
            self.done = False
        
        obs = self._get_observation()
        
        return obs, reward, self.done, {}
    
    def reset(self):
        self.current_step = self.window_size
        self.equity = 100
        self.done = False
        self.equity_curve = []
        self.last_trade_info = None
        return self._get_observation()
    
    def render(seld, mode='human'):
        print(f"Step: {self.current_step}, Equity: {self.equity}")