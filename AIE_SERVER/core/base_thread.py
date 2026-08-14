#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
基础线程类 - 所有处理线程的基类
"""

import time
import threading
from typing import Optional, Dict, Any
from .logger_config import get_logger

# 获取日志器
logger = get_logger(__name__)

class BaseThread(threading.Thread):
    """基础线程类 - 所有处理线程的基类"""
    
    def __init__(self, name: str, data_manager, device: str = 'cpu'):
        """
        初始化基础线程
        
        Args:
            name: 线程名称
            data_manager: 数据管理器实例
            device: 计算设备 ('cpu', 'cuda', 'cuda:0' 等)
        """
        super().__init__(name=name, daemon=True)
        
        self.data_manager = data_manager
        self.device = device
        self.running = False
        self.paused = False
        
        # 性能统计
        self._process_count = 0
        self._total_process_time = 0.0
        self._start_time = None
        self._last_stats_time = time.time()
        
        # 线程同步
        self._pause_event = threading.Event()
        self._pause_event.set()  # 初始状态为运行
        
        logger.info(f"基础线程 {name} 已初始化，设备: {device}")
    
    def start(self):
        """启动线程"""
        if not self.running:
            self.running = True
            self._start_time = time.time()
            super().start()
            logger.info(f"线程 {self.name} 已启动")
        else:
            logger.warning(f"线程 {self.name} 已在运行")
    
    def stop(self):
        """停止线程"""
        if self.running:
            self.running = False
            self._pause_event.set()  # 确保线程不被暂停
            logger.info(f"线程 {self.name} 已停止")
        else:
            logger.warning(f"线程 {self.name} 未在运行")
    
    def pause(self):
        """暂停线程"""
        if self.running and not self.paused:
            self.paused = True
            self._pause_event.clear()
            logger.info(f"线程 {self.name} 已暂停")
        else:
            logger.warning(f"线程 {self.name} 无法暂停")
    
    def resume(self):
        """恢复线程"""
        if self.running and self.paused:
            self.paused = False
            self._pause_event.set()
            logger.info(f"线程 {self.name} 已恢复")
        else:
            logger.warning(f"线程 {self.name} 无法恢复")
    
    def wait_if_paused(self, timeout: float = 1.0) -> bool:
        """如果线程被暂停，则等待恢复"""
        if self.paused:
            return self._pause_event.wait(timeout=timeout)
        return True
    
    def is_running(self) -> bool:
        """检查线程是否正在运行"""
        return self.running and self.is_alive()
    
    def is_paused(self) -> bool:
        """检查线程是否被暂停"""
        return self.paused
    
    def get_device(self) -> str:
        """获取计算设备"""
        return self.device
    
    def set_device(self, device: str):
        """设置计算设备"""
        self.device = device
        logger.info(f"线程 {self.name} 设备已设置为: {device}")
    
    
    def _log_stats(self):
        """记录性能统计信息"""
        if self._process_count > 0:
            avg_process_time = self._total_process_time / self._process_count
            fps = self._process_count / (time.time() - self._start_time) if self._start_time else 0
            
            logger.debug(f"线程 {self.name} 统计 - "
                        f"处理帧数: {self._process_count}, "
                        f"平均处理时间: {avg_process_time*1000:.2f}ms, "
                        f"FPS: {fps:.1f}")
    
    def get_stats(self) -> Dict[str, Any]:
        """获取性能统计信息"""
        if self._process_count > 0:
            avg_process_time = self._total_process_time / self._process_count
            fps = self._process_count / (time.time() - self._start_time) if self._start_time else 0
        else:
            avg_process_time = 0.0
            fps = 0.0
        
        return {
            'name': self.name,
            'running': self.running,
            'paused': self.paused,
            'process_count': self._process_count,
            'avg_process_time_ms': avg_process_time * 1000,
            'fps': fps,
            'device': self.device,
            'uptime': time.time() - self._start_time if self._start_time else 0
        }
    
    def reset_stats(self):
        """重置性能统计"""
        self._process_count = 0
        self._total_process_time = 0.0
        self._start_time = time.time()
        self._last_stats_time = time.time()
        logger.info(f"线程 {self.name} 统计已重置")
    
    # 抽象方法 - 子类必须实现
    def run(self):
        """线程主循环 - 子类必须实现"""
        raise NotImplementedError("子类必须实现run方法")
    
    def process_frame(self, frame, timestamp: float, sequence: int) -> Optional[Dict[str, Any]]:
        """处理单帧图像 - 子类必须实现"""
        raise NotImplementedError("子类必须实现process_frame方法")
    
    def cleanup(self):
        """清理资源 - 子类可以重写"""
        logger.info(f"线程 {self.name} 正在清理资源")
    
    def __enter__(self):
        """上下文管理器入口"""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器出口"""
        self.stop()
        self.cleanup() 