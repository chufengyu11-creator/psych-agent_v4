#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
基础API类 - 所有API的基类
提供线程管理器和数据管理器的统一接口
"""

import time
import threading
from typing import Optional, Dict, Any
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.thread_manager import CoreThreadManager, get_core_thread_manager, get_core_data_manager, release_core_thread_manager
from core.data_manager import CoreDataManager
from core.logger_config import get_logger

logger = get_logger(__name__)

class BaseAPI:
    """基础API类 - 所有API的基类"""
    
    def __init__(self, server_address: str,camera_id: int = 0, device: str = 'cuda'):
        """
        初始化基础API
        
        Args:
            camera_id: 摄像头ID（仅在首次创建全局线程管理器时使用）
            device: 计算设备 ('cpu', 'cuda', 'cuda:0' 等，仅在首次创建时使用)
        """
        self.camera_id = camera_id
        self.device = device
        self.server_address = server_address
        # 使用全局单例线程管理器，确保多个API共享同一个摄像头线程
        self.thread_manager: Optional[CoreThreadManager] = None
        self.data_manager: Optional[CoreDataManager] = None

        # API需要的线程名称集合
        self._required_threads: set = set()
        
        # 生成唯一API标识符
        import uuid
        self._api_id = f"{self.__class__.__name__}_{uuid.uuid4().hex[:8]}"
        
        # 用于保持主线程运行的Event（不消耗CPU）
        self._stop_event = threading.Event()
        
        # API就绪状态
        self._is_ready = False
        
        logger.info(f"基础API初始化完成 - 摄像头: {camera_id}, 设备: {device}")
    

    
    def start_processing_threads(self, thread_names: Optional[set] = None) -> bool:
        """
        启动处理线程（第二步）
        必须在摄像头线程启动之后调用
        
        Args:
            thread_names: 要启动的线程名称集合，如果为None则使用API注册的线程需求
        
        Returns:
            bool: 是否启动成功
        """
        try:
            if self.thread_manager is None:
                logger.error("线程管理器未初始化，请先启动摄像头线程")
                return False
            
            # # 检查摄像头线程是否已启动
            # if 'camera' not in self.thread_manager.threads:
            #     logger.error("摄像头线程不存在，请先启动摄像头线程")
            #     return False
            
            # camera_thread = self.thread_manager.threads['camera']
            # if not camera_thread.is_running():
            #     logger.error("摄像头线程未运行，请先启动摄像头线程")
            #     return False
            
            # 如果没有指定线程名称，使用API注册的线程需求
            if thread_names is None:
                thread_names = self._required_threads
            
            # 注册线程需求
            if thread_names:
                self.thread_manager.register_thread_requirement(self._api_id, thread_names)
            
            # 检查哪些线程需要启动（过滤掉已经在运行的线程）
            threads_to_start = set()
            for thread_name in thread_names:
                if thread_name in self.thread_manager.threads:
                    thread = self.thread_manager.threads[thread_name]
                    if not thread.is_running():
                        threads_to_start.add(thread_name)
                    else:
                        logger.info(f"线程 {thread_name} 已在运行，跳过启动")
                else:
                    threads_to_start.add(thread_name)
            
            # 只启动未运行的线程
            if threads_to_start:
                if not self.thread_manager.start_processing_threads(threads_to_start):
                    logger.warning("部分处理线程启动失败，但系统继续运行")
            else:
                logger.info("所有需要的处理线程已在运行")
            
            # 更新运行状态
            if not self.thread_manager.is_running():
                self.thread_manager.running = True
            
            logger.info(f"处理线程启动完成: {thread_names}")
            return True
            
        except Exception as e:
            import traceback
            error_info = traceback.format_exc()
            logger.error(f"启动处理线程失败: {e}\n错误详情:\n{error_info}")
            return False
    
    def start(self, server_address = None) -> bool:
        """
        启动系统（自动按顺序启动：先摄像头，后处理线程）
        摄像头线程会自动启动（如果未启动），处理线程按需启动
        
        Returns:
            bool: 是否启动成功
        
        """
        if server_address is None:
            server_address = self.server_address

        try:
            # 获取全局线程管理器（会自动启动摄像头线程）
            if self.thread_manager is None:
                self.thread_manager = get_core_thread_manager(
                    camera_id=self.camera_id,
                    device=self.device,
                    auto_start_camera=True,
                    server_address = self.server_address # 自动启动摄像头
                )
                self.data_manager = self.thread_manager.get_data_manager()
                self.thread_manager.initialize()
            # # 确保摄像头线程已启动
            # if 'camera' not in self.thread_manager.threads:
            #     logger.error("摄像头线程不存在")
            #     return False
            
            # camera_thread = self.thread_manager.threads['camera']
            # if not camera_thread.is_running():
            #     logger.info("启动摄像头线程...")
            #     if not self.thread_manager.start_camera_thread():
            #         logger.error("摄像头线程启动失败")
            #         return False
            #     # 等待摄像头线程稳定
            #     logger.info("等待摄像头线程稳定...")
            #     time.sleep(2.0)
            
            # 如果API有需要的处理线程，启动它们
            # if self._required_threads:
            #     # 注册线程需求
            #     self.thread_manager.register_thread_requirement(self._api_id, self._required_threads)
                
            #     # 检查哪些线程需要启动（过滤掉已经在运行的线程）
            #     threads_to_start = set()
            #     for thread_name in self._required_threads:
            #         if thread_name in self.thread_manager.threads:
            #             thread = self.thread_manager.threads[thread_name]
            #             if not thread.is_running():
            #                 threads_to_start.add(thread_name)
            #             else:
            #                 logger.info(f"线程 {thread_name} 已在运行，跳过启动")
            #         else:
            #             threads_to_start.add(thread_name)
                
            #     # 只启动未运行的线程
            #     if threads_to_start:
            #         if not self.thread_manager.start_processing_threads(threads_to_start):
            #             logger.warning("部分处理线程启动失败，但系统继续运行")
            #     else:
            #         logger.info("所有需要的处理线程已在运行")
            
            # # 更新运行状态
            # if not self.thread_manager.is_running():
            #     self.thread_manager.running = True
            
            self._is_ready = True
            logger.info("系统启动成功,仅仅初始化datamanager,其他线程不启动")
            return True
            
        except Exception as e:
            import traceback
            error_info = traceback.format_exc()
            logger.error(f"启动系统失败: {e}\n错误详情:\n{error_info}")
            return False
    
    def wait(self, timeout: Optional[float] = None):
        if not self._is_ready:
            logger.warning("API未初始化，无法等待")
            return
        
        logger.info("主线程进入等待状态，后台服务持续运行中...")
        logger.info("提示: 按 Ctrl+C 或调用 stop() 方法可以退出")
        
        try:
            # 使用 Event.wait() 阻塞，不消耗CPU
            # 可以通过 set() 来唤醒，或通过 timeout 来超时
            self._stop_event.wait(timeout=timeout)
        except KeyboardInterrupt:
            logger.info("收到中断信号，正在退出...")
            raise
    
    def stop(self):
        """
        停止等待，唤醒 wait() 方法
        注意：这不会停止后台线程，只是让 wait() 方法返回
        要停止后台线程，请调用 cleanup()
        """
        self._stop_event.set()
        logger.info("已发送停止信号")
    
    def cleanup(self):
        """
        清理资源
        释放对全局线程管理器的引用
        子类应该重写此方法来实现具体的线程停止逻辑
        """
        try:
            # 停止等待（如果正在等待）
            self.stop()
            
            # 注销线程需求
            if self.thread_manager and self._api_id:
                self.thread_manager.unregister_thread_requirement(self._api_id)
            
            # 释放全局线程管理器引用
            if self.thread_manager:
                release_core_thread_manager()
                self.thread_manager = None
                self.data_manager = None
            
            logger.info("API资源已清理")
        except Exception as e:
            import traceback
            error_info = traceback.format_exc()
            logger.error(f"清理API资源失败: {e}\n错误详情:\n{error_info}")
    
    def __enter__(self):
        """上下文管理器入口"""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器出口"""
        self.cleanup()

