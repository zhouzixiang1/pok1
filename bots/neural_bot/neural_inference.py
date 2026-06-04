"""共享 NumPy 神经网络推理引擎。

纯 NumPy 实现，运行时无 PyTorch 依赖。
前向传播就是矩阵乘法 + ReLU + sigmoid/softmax。
"""

import numpy as np
import os


def relu(x):
    return np.maximum(0, x)


def sigmoid(x):
    x = np.clip(x, -20, 20)
    return 1.0 / (1.0 + np.exp(-x))


def softmax(x):
    x = x - np.max(x, axis=-1, keepdims=True)
    e = np.exp(x)
    return e / np.sum(e, axis=-1, keepdims=True)


class FCNet:
    """全连接网络，纯 NumPy 推理。"""

    def __init__(self, layer_sizes, output_activation='sigmoid'):
        """
        Args:
            layer_sizes: 如 [123, 128, 64, 32, 1]
            output_activation: 'sigmoid', 'softmax', 'none'
        """
        self.layer_sizes = layer_sizes
        self.output_activation = output_activation
        self.weights = []
        self.biases = []

    def load_weights(self, path):
        """从 .npz 文件加载权重。"""
        data = np.load(path)
        self.weights = []
        self.biases = []
        i = 0
        while f'w{i}' in data:
            self.weights.append(data[f'w{i}'].astype(np.float32))
            self.biases.append(data[f'b{i}'].astype(np.float32))
            i += 1
        return len(self.weights) > 0

    def forward(self, x):
        """前向传播。x 可以是 (features,) 或 (batch, features)。"""
        if x.ndim == 1:
            x = x.reshape(1, -1)

        for i, (w, b) in enumerate(zip(self.weights, self.biases)):
            x = x @ w + b
            if i < len(self.weights) - 1:
                x = relu(x)
            elif self.output_activation == 'sigmoid':
                x = sigmoid(x)
            elif self.output_activation == 'softmax':
                x = softmax(x)
        return x

    def predict(self, features):
        """单样本推理，返回标量或向量。"""
        result = self.forward(features)[0]
        if result.ndim == 0:
            return float(result)
        if result.ndim == 1 and len(result) == 1:
            return float(result[0])
        return result


def load_network(name, data_dir=None):
    """加载命名网络的便捷函数。

    查找路径: {data_dir}/{name}_weights.npz
    """
    if data_dir is None:
        data_dir = os.path.join(os.path.dirname(__file__), 'data')

    # 网络架构定义
    architectures = {
        'equity': {
            'layers': [123, 128, 64, 32, 1],
            'activation': 'sigmoid',
        },
        'policy_action': {
            'layers': [200, 256, 128, 64, 3],
            'activation': 'softmax',
        },
        'policy_discrete': {
            'layers': [200, 256, 128, 64, 6],
            'activation': 'softmax',
        },
        'policy_raise': {
            'layers': [200, 128, 64, 1],
            'activation': 'sigmoid',
        },
    }

    if name not in architectures:
        raise ValueError(f"未知网络: {name}")

    config = architectures[name]
    net = FCNet(config['layers'], config['activation'])

    path = os.path.join(data_dir, f'{name}_weights.npz')
    if not os.path.exists(path):
        return None

    if net.load_weights(path):
        return net
    return None
