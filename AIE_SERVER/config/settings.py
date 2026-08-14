# -*- coding: utf-8 -*-
"""
系统配置文件
"""

import os
from typing import Dict, Any, List

# 基础配置
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 模型配置
MODEL_CONFIG = {
    # 面部检测配置
    'face_detection': {
        'backend': 'mediapipe',  # mediapipe, opencv
        'min_detection_confidence': 0.7,
        'model_selection': 0,
    },
    
    # AU检测配置
    'au_detection': {
        'disfa_model_path': os.path.join(BASE_DIR, 'models', 'weights', 'disfa_model.pt'),
        'bp4d_model_path': os.path.join(BASE_DIR, 'models', 'weights', 'bp4d_model.pt'),
        'num_class_disfa': 12,
        'num_class_bp4d': 5,
        'backbone': 'resnet34',
        'pooling': True,
        'normalize': True,
        'activation': '',
    },
    
    # 情绪识别配置
    'emotion_recognition': {
        'model_path': os.path.join(BASE_DIR, 'models', 'weights', 'emotion_model.pt'),
        'labels': ['angry', 'disgust', 'fear', 'happy', 'sad', 'surprise', 'neutral'],
    },
}

# AU配置
AU_CONFIG = {
    # AU列表
    'au_list': [
        "AU1", "AU2", "AU4", "AU5", "AU6", "AU9",
        "AU10", "AU12", "AU14", "AU15", "AU17", "AU20",
        "AU25", "AU26", "AU43"
    ],
    
    # AU阈值
    'thresholds': [0.4] * 15,
    
    # 时间窗口配置
    'window_size': 150,
    'ratio': 0.7,
    
    # 数据平滑配置
    'smoothing_window_size': 5,
}

# 数据处理配置
DATA_CONFIG = {
    'input_size': (224, 224),
    'normalize_mean': [0.485, 0.456, 0.406],
    'normalize_std': [0.229, 0.224, 0.225],
    'batch_size': 1,
}

# 日志配置
LOG_CONFIG = {
    'level': 'INFO',
    'format': '{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}',
    'file': os.path.join(BASE_DIR, 'logs', 'system.log'),
}

# 输出配置
OUTPUT_CONFIG = {
    'save_results': True,
    'output_dir': os.path.join(BASE_DIR, 'outputs'),
    'save_format': 'json',  # json, csv, pickle
}

# 设备配置
DEVICE_CONFIG = {
    'use_gpu': True,
    'gpu_id': 0,
    'num_workers': 4,
}

def get_config() -> Dict[str, Any]:
    """获取完整配置"""
    return {
        'model': MODEL_CONFIG,
        'au': AU_CONFIG,
        'data': DATA_CONFIG,
        'log': LOG_CONFIG,
        'output': OUTPUT_CONFIG,
        'device': DEVICE_CONFIG,
    }

def update_config(updates: Dict[str, Any]):
    """更新配置"""
    global MODEL_CONFIG, AU_CONFIG, DATA_CONFIG, LOG_CONFIG, OUTPUT_CONFIG, DEVICE_CONFIG
    
    if 'model' in updates:
        MODEL_CONFIG.update(updates['model'])
    if 'au' in updates:
        AU_CONFIG.update(updates['au'])
    if 'data' in updates:
        DATA_CONFIG.update(updates['data'])
    if 'log' in updates:
        LOG_CONFIG.update(updates['log'])
    if 'output' in updates:
        OUTPUT_CONFIG.update(updates['output'])
    if 'device' in updates:
        DEVICE_CONFIG.update(updates['device'])