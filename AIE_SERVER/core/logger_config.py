#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全局日志配置管理器
提供统一的日志级别控制和格式化，支持自定义级别和消息过滤
"""

import logging
import os
import sys
from typing import Optional, List, Set

class LoggerConfig:
    """全局日志配置管理器"""
    
    _instance = None
    _initialized = False
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self._initialized = True
        self._log_level = logging.ERROR
        
        # 自定义日志级别
        self._setup_custom_levels()
        
        # 从环境变量读取配置
        self._load_from_env()
        
        # 设置默认日志级别
        self._setup_logging()
        
        print(f"日志配置初始化完成 - 日志级别: {logging.getLevelName(self._log_level)}")
    
    def _setup_custom_levels(self):
        """设置自定义日志级别"""
        # 添加自定义级别
        logging.VERBOSE = 5
        logging.NOTICE = 15
        logging.SUCCESS = 25
        logging.CUSTOM = 35
        
        logging.addLevelName(logging.VERBOSE, "VERBOSE")      # 比DEBUG更详细
        logging.addLevelName(logging.NOTICE, "NOTICE")        # 通知信息
        logging.addLevelName(logging.SUCCESS, "SUCCESS")      # 成功信息
        logging.addLevelName(logging.CUSTOM, "CUSTOM")    # 自定义信息
        
        # 为Logger类添加自定义方法
        logging.Logger.verbose = lambda self, msg, *args, **kwargs: self.log(logging.VERBOSE, msg, *args, **kwargs)
        logging.Logger.notice = lambda self, msg, *args, **kwargs: self.log(logging.NOTICE, msg, *args, **kwargs)
        logging.Logger.success = lambda self, msg, *args, **kwargs: self.log(logging.SUCCESS, msg, *args, **kwargs)
        logging.Logger.custom = lambda self, msg, *args, **kwargs: self.log(logging.CUSTOM, msg, *args, **kwargs)
    
    def _load_from_env(self):
        """从环境变量加载配置"""
        # 检查日志级别环境变量
        log_level_env = os.getenv('LOG_LEVEL', 'INFO').upper()
        
        # 支持的标准和自定义级别
        level_mapping = {
            'VERBOSE': 5,      # 最详细
            'DEBUG': 10,       # 调试信息
            'NOTICE': 15,      # 通知信息
            'INFO': 20,        # 一般信息
            'SUCCESS': 25,     # 成功信息
            'WARNING': 30,     # 警告
            'CUSTOM': 35,      # 自定义信息
            'ERROR': 40,       # 错误
            'CRITICAL': 50     # 严重错误
        }
        
        if log_level_env in level_mapping:
            self._log_level = level_mapping[log_level_env]
        
        # 检查是否启用自定义消息过滤
        self._custom_filter = os.getenv('LOG_FILTER', '').strip()
        if self._custom_filter:
            print(f"启用自定义消息过滤: {self._custom_filter}")
    
    def _setup_logging(self):
        """设置日志配置"""
        # 设置根日志器级别
        logging.getLogger().setLevel(self._log_level)
        
        # 创建控制台处理器
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(self._log_level)
        
        # 添加自定义过滤器
        if self._custom_filter:
            console_handler.addFilter(self._create_custom_filter())
        
        # 创建格式化器
        formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s'
        )
        
        formatter_error = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s'
        )

        #console_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter_error)
        
        # 添加到根日志器
        root_logger = logging.getLogger()
        root_logger.addHandler(console_handler)
    
    def _create_custom_filter(self):
        """创建自定义消息过滤器"""
        class CustomFilter(logging.Filter):
            def __init__(self, filter_text):
                super().__init__()
                self.filter_text = filter_text.lower()
            
            def filter(self, record):
                # 只显示包含指定关键词的消息
                return self.filter_text in record.getMessage().lower()
        
        return CustomFilter(self._custom_filter)
    
    def get_logger(self, name: str) -> logging.Logger:
        """获取配置好的日志器"""
        logger = logging.getLogger(name)
        logger.setLevel(self._log_level)
        return logger
    
    def get_log_level(self) -> int:
        """获取当前日志级别"""
        return self._log_level
    
    def set_log_level(self, level: int):
        """设置日志级别"""
        self._log_level = level
        
        # 更新根日志器
        logging.getLogger().setLevel(level)
        
        print(f"日志级别已设置为: {logging.getLevelName(level)}")
    
    def get_available_levels(self) -> dict:
        """获取所有可用的日志级别"""
        return {
            'VERBOSE': 5,      # 最详细
            'DEBUG': 10,       # 调试信息
            'NOTICE': 15,      # 通知信息
            'INFO': 20,        # 一般信息
            'SUCCESS': 25,     # 成功信息
            #'PROGRESS': 35,    # 进度信息
            'CUSTOM': 35,     # 自定义信息
            'WARNING': 30,     # 警告
            'ERROR': 40,       # 错误
            'CRITICAL': 50     # 严重错误
        }

# 全局实例
logger_config = LoggerConfig()

def get_logger(name: str) -> logging.Logger:
    """获取配置好的日志器（便捷函数）"""
    return logger_config.get_logger(name)

def set_log_level(level: int):
    """设置日志级别（便捷函数）"""
    logger_config.set_log_level(level)

def get_available_levels() -> dict:
    """获取所有可用的日志级别（便捷函数）"""
    return logger_config.get_available_levels()

# 环境变量配置说明
"""
使用环境变量控制日志级别：

# 标准日志级别
export LOG_LEVEL=DEBUG      # 显示调试及以上级别
export LOG_LEVEL=INFO       # 显示信息及以上级别
export LOG_LEVEL=WARNING    # 显示警告及以上级别
export LOG_LEVEL=ERROR      # 仅显示错误（默认）
export LOG_LEVEL=CRITICAL   # 仅显示严重错误

# 自定义日志级别
export LOG_LEVEL=VERBOSE    # 显示最详细信息
export LOG_LEVEL=NOTICE     # 显示通知及以上级别
export LOG_LEVEL=SUCCESS    # 显示成功及以上级别
export LOG_LEVEL=custom     # 显示自定义信息

# 自定义消息过滤（只显示包含特定关键词的消息）
export LOG_FILTER=heart     # 只显示包含"heart"的消息
export LOG_FILTER=error     # 只显示包含"error"的消息
export LOG_FILTER=fps       # 只显示包含"fps"的消息

# 组合使用示例
export LOG_LEVEL=INFO       # 设置日志级别
export LOG_FILTER=success   # 只显示包含"success"的INFO及以上级别消息

# 在Windows上使用：
set LOG_LEVEL='CUSTOM'
set LOG_FILTER='emotion'
""" 
