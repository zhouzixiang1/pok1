"""神经网络策略模块。

结合策略网络和规则逻辑的混合决策系统:
1. 策略网络 → 动作概率 (fold/call/raise)
2. 胜率网络 → 快速胜率估计（如果可用）
3. 规则回退 → 验证和修正非法动作
"""

import os
import numpy as np

from neural_inference import load_network


DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')


class NNStrategy:
    def __init__(self):
        self.policy_action_net = load_network('policy_action', DATA_DIR)
        self.policy_raise_net = load_network('policy_raise', DATA_DIR)
        self.equity_net = load_network('equity', DATA_DIR)
        self.available = self.policy_action_net is not None

    def is_available(self):
        return self.available

    def get_action(self, features):
        """给定策略特征 (200 维)，返回 (action_label, confidence)。

        action_label: 0=fold, 1=call, 2=raise
        confidence: 最大概率值
        """
        if not self.available:
            return None, 0.0

        probs = self.policy_action_net.forward(features.reshape(1, -1))[0]
        label = int(np.argmax(probs))
        confidence = float(probs[label])
        return label, confidence

    def get_raise_fraction(self, features):
        """给定策略特征，返回加注占底池+下注的比例 [0, 1]。"""
        if self.policy_raise_net is None:
            return 0.5
        frac = float(self.policy_raise_net.predict(features))
        return frac

    def get_equity(self, equity_features):
        """给定胜率特征 (123 维)，返回胜率估计 [0, 1]。"""
        if self.equity_net is None:
            return None
        return float(self.equity_net.predict(equity_features))


# 全局单例
_strategy = None


def get_strategy():
    global _strategy
    if _strategy is None:
        _strategy = NNStrategy()
    return _strategy
