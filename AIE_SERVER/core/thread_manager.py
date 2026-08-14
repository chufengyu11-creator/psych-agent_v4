#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
核心线程管理器 - 负责多线程协调和管理
"""

import time
import threading
from typing import Dict, Any, Optional, List, Set
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 导入新的日志配置
from core.logger_config import get_logger
# 导入数据管理器和线程类
from core.data_manager import CoreDataManager
from core.camera_thread import CameraThread
from core.face_thread import FaceDetectionThread
from core.au_thread import AUDetectionThread
# 获取日志器
logger = get_logger(__name__)

class CoreThreadManager:
    """核心线程管理器 - 负责多线程协调和管理"""
    
    def __init__(self, server_address:  str="tcp://localhost:5554", camera_id: int = 0, device: str = 'cuda' ):
        """
        初始化核心线程管理器
        
        Args:
            camera_id: 摄像头ID
            device: 计算设备
        """
        self.camera_id = camera_id
        self.device = device
        self.server_address = server_address
        

        # 数据管理器
        self.data_manager = CoreDataManager()
        
        # 线程字典
        self.threads = {}
        self.thread_status = {}
        
        # 线程管理锁
        self._thread_lock = threading.Lock()
        
        # 运行状态
        self.running = False
        self.initialized = False

        # 数据收集器
        self.data_collector = None
        
        # 线程需求注册 (记录线程)
        self._thread_requirements: Dict[str, Set[str]] = {}  # {api_id: {thread_names}}
        self._thread_requirements_lock = threading.Lock()
        
        logger.info(f"核心线程管理器已初始化 - 摄像头: {camera_id}, 设备: {device}")
    
    # 创建线程函数
    def initialize(self) -> bool:
        """初始化所有线程"""
        try:
            with self._thread_lock:
                if self.initialized:
                    logger.warning("-----任务管理器已经初始化-----")
                    return True
                
                # 创建摄像头线程
                # self.threads['camera'] = CameraThread(
                #     server_address=self.server_address,
                #     camera_id=self.camera_id,
                #     data_manager=self.data_manager
                    
                # )
                
                # 创建AU检测线程
                self.threads['au'] = AUDetectionThread(
                    data_manager=self.data_manager,
                    device=self.device
                )

                self.threads['face_detection'] = FaceDetectionThread(
                    data_manager=self.data_manager,
                    device=self.device
                )

                
                # 初始化线程状态
                for thread_name in self.threads:
                    self.thread_status[thread_name] = {
                        'created': True,
                        'started': False,
                        'running': False,
                        'error': None
                    }
                
                #初始化成功标志位
                self.initialized = True
                logger.info("所有线程已创建")
                # 创建并启动数据收集器（当检测到人脸时自动收集数据）
                # try:
                #     self.data_collector = create_data_collector(
                #         data_manager=self.data_manager,
                #         collection_duration=30.0,  # 收集30秒数据
                #         sample_interval=0.01      # 检查间隔（实际只要有数据就收集）
                #     )
                #     self.data_collector.start_collection_when_face_detected()
                #     logger.info("数据收集器已启动，等待人脸出现...")
                #     print("[线程管理器] 数据收集器已启动，等待人脸出现...")
                # except Exception as e:
                #     logger.warning(f"启动数据收集器失败: {e}，将继续运行但不收集数据")
                #     print(f"[线程管理器] ✗ 启动数据收集器失败: {e}，将继续运行但不收集数据")
                #     import traceback
                #     traceback.print_exc()
                return True
                
        except Exception as e:
            logger.error(f"初始化线程失败: {e}")
            return False

    # 单独启动摄像头线程
    def start_camera_thread(self) -> bool:
        """
        单独启动摄像头线程
        这是其他线程启动的前提条件
        
        Returns:
            bool: 是否启动成功
        """
        try:
            if not self.initialized:
                if not self.initialize():
                    logger.error("-----任务管理器初始化未成功-----")
                    return False
            
            with self._thread_lock:
                # 检查摄像头线程是否已启动
                if 'camera' in self.threads:
                    camera_thread = self.threads['camera']
                    if camera_thread.is_running():
                        logger.info("------摄像头正常运行中------")
                        return True
                
                # 如果没有启动摄像头线程
                logger.info("正在启动摄像头线程...")
                if not self._start_thread('camera'):
                    logger.error("-----摄像头线程启动失败-----")
                    return False
                
                # 等待摄像头线程稳定
                time.sleep(1.0)
                logger.info("------摄像头正常运行中------")
                return True
        except Exception as e:
            logger.error(f"启动摄像头线程失败: {e}")
            return False
    
    def register_thread_requirement(self, api_id: str, thread_names: Set[str]):
        """
        注册线程需求（某个API需要哪些处理线程）
        
        Args:
            api_id: API标识符（用于跟踪）
            thread_names: 需要的线程名称集合，如 {'gaze_attention', 'au'}
        """
        with self._thread_requirements_lock:
            self._thread_requirements[api_id] = thread_names.copy()
            logger.info(f"API {api_id} 注册线程需求: {thread_names}")
    
    def unregister_thread_requirement(self, api_id: str):
        """
        注销线程需求
        
        Args:
            api_id: API标识符
        """
        with self._thread_requirements_lock:
            if api_id in self._thread_requirements:
                del self._thread_requirements[api_id]
                logger.info(f"API {api_id} 注销线程需求")
    
    def get_required_threads(self) -> Set[str]:
        """
        获取所有需要的线程集合（所有API需求的并集）
        
        Returns:
            Set[str]: 需要的线程名称集合
        """
        with self._thread_requirements_lock:
            required = set()
            for thread_names in self._thread_requirements.values():
                required.update(thread_names)
            return required
    
    def start_processing_threads(self, thread_names: Optional[Set[str]] = None) -> bool:
        """
        启动处理线程（人脸检测、AU、情绪）
        必须在摄像头线程启动之后调用
        
        Args:
            thread_names: 要启动的线程名称集合，如果为None则启动所有需要的线程
        
        Returns:
            bool: 是否启动成功
        """
        try:
            # 检查摄像头线程是否已启动
            # if 'camera' not in self.threads or not self.threads['camera'].is_running():
            #     logger.error("摄像头线程未启动，无法启动处理线程")
            #     return False
            
            # 如果没有指定线程名称，则启动所有需要的线程
            if thread_names is None:
                thread_names = self.get_required_threads()
            
            if not thread_names:
                logger.info("没有需要启动的处理线程")
                return True
            
            with self._thread_lock:
                success_count = 0
              
                if 'face_detection' in thread_names:
                    logger.info("正在启动人脸检测线程...")
                    if self._start_thread('face_detection'):
                        success_count += 1
                    else:
                        logger.warning("人脸检测线程启动失败")

                # 启动AU检测线程
                if 'au' in thread_names:
                    logger.info("正在启动AU检测线程...")
                    if self._start_thread('au'):
                        success_count += 1
                    else:
                        logger.warning("AU检测线程启动失败")
                
                # 等待一段时间，让前面的线程稳定
                if success_count > 0:
                    time.sleep(0.5)
                
                logger.info(f"处理线程启动完成: {success_count}/{len(thread_names)} 成功")
                return success_count > 0
                
        except Exception as e:
            logger.error(f"启动处理线程失败: {e}")
            return False
    
    def start_all_threads(self) -> bool:
        """
        启动所有线程（先启动摄像头，再启动其他线程）
        
        Returns:
            bool: 是否启动成功
        """
        try:
            with self._thread_lock:
                if self.running:
                    logger.warning("线程管理器已在运行")
                    return True
                
                # # 第一步：启动摄像头线程
                # if not self.start_camera_thread():
                #     logger.error("摄像头线程启动失败，无法继续启动其他线程")
                #     return False
                
                # # 第二步：等待摄像头线程稳定并开始产生数据
                # logger.info("等待摄像头线程稳定并开始产生数据...")
                # time.sleep(2.0)  # 给摄像头更多时间稳定
                
                # 第三步：启动处理线程
                if not self.start_processing_threads():
                    logger.warning("部分处理线程启动失败，但系统继续运行")
                
                self.running = True
                logger.info("所有线程启动流程完成")
                return True
                
        except Exception as e:
            logger.error(f"启动线程失败: {e}")
            return False

    # 启动指定线程， 设置线程状态, 注册线程到数据管理器
    def _start_thread(self, thread_name: str) -> bool:
        """启动指定线程"""
        try:
            if thread_name not in self.threads:
                logger.error(f"线程 {thread_name} 不存在")
                return False
            
            thread = self.threads[thread_name]
            
            # 检查线程是否已经在运行，避免重复启动
            if thread.is_running():
                logger.info(f"线程 {thread_name} 已在运行，跳过启动")
                # 更新状态（确保状态一致）
                self.thread_status[thread_name]['started'] = True
                self.thread_status[thread_name]['running'] = True
                self.thread_status[thread_name]['error'] = None
                return True
            
            # 启动线程
            thread.start()
            
            # 等待线程启动
            time.sleep(0.1)
            
            # 检查线程状态
            if thread.is_running():
                self.thread_status[thread_name]['started'] = True
                self.thread_status[thread_name]['running'] = True
                self.thread_status[thread_name]['error'] = None
                
                logger.info(f"线程 {thread_name} 启动成功")
                return True
            else:
                self.thread_status[thread_name]['error'] = "线程启动失败"
                logger.error(f"线程 {thread_name} 启动失败")
                return False
                
        except Exception as e:
            self.thread_status[thread_name]['error'] = str(e)
            logger.error(f"启动线程 {thread_name} 失败: {e}")
            return False
    
    def stop_all_threads(self):
        """停止所有线程"""
        try:
            with self._thread_lock:
                if not self.running:
                    logger.warning("线程管理器未在运行")
                    return
                
                self.running = False
                
                # 停止所有线程
                for thread_name, thread in self.threads.items():
                    try:
                        if thread.is_running():
                            thread.stop()
                            logger.info(f"线程 {thread_name} 已停止")
                        
                        # 更新状态
                        self.thread_status[thread_name]['running'] = False
                        self.thread_status[thread_name]['started'] = False
                        
                    except Exception as e:
                        logger.error(f"停止线程 {thread_name} 失败: {e}")
                
                logger.info("所有线程已停止")
                
        except Exception as e:
            logger.error(f"停止线程失败: {e}")
    
    def pause_thread(self, thread_name: str):
        """暂停指定线程"""
        try:
            if thread_name in self.threads:
                thread = self.threads[thread_name]
                thread.pause()
                logger.info(f"线程 {thread_name} 已暂停")
            else:
                logger.warning(f"线程 {thread_name} 不存在")
        except Exception as e:
            logger.error(f"暂停线程 {thread_name} 失败: {e}")
    
    def resume_thread(self, thread_name: str):
        """恢复指定线程"""
        try:
            if thread_name in self.threads:
                thread = self.threads[thread_name]
                thread.resume()
                logger.info(f"线程 {thread_name} 已恢复")
            else:
                logger.warning(f"线程 {thread_name} 不存在")
        except Exception as e:
            logger.error(f"恢复线程 {thread_name} 失败: {e}")
    
    def get_thread_status(self, thread_name: str) -> Optional[Dict[str, Any]]:
        """获取指定线程状态"""
        if thread_name in self.thread_status:
            status = self.thread_status[thread_name].copy()
            
            # 添加线程统计信息
            if thread_name in self.threads:
                thread = self.threads[thread_name]
                status['stats'] = thread.get_stats()
                status['is_alive'] = thread.is_alive()
            
            return status
        return None
    
    def get_all_threads_status(self) -> Dict[str, Dict[str, Any]]:
        """获取所有线程状态"""
        status = {}
        for thread_name in self.threads:
            status[thread_name] = self.get_thread_status(thread_name)
        return status
    
    def get_thread_stats(self, thread_name: str) -> Optional[Dict[str, Any]]:
        """获取指定线程统计信息"""
        if thread_name in self.threads:
            return self.threads[thread_name].get_stats()
        return None
    
    def get_data_manager(self) -> CoreDataManager:
        """获取数据管理器"""
        return self.data_manager
    
    def is_running(self) -> bool:
        """检查线程管理器是否正在运行"""
        return self.running
    
    def is_initialized(self) -> bool:
        """检查线程管理器是否已初始化"""
        return self.initialized
    
    def get_camera_info(self) -> Dict[str, Any]:
        """获取摄像头信息"""
        if 'camera' in self.threads:
            return self.threads['camera'].get_camera_info()
        return {}
    
    def get_attention_statistics(self) -> Optional[Dict[str, Any]]:
        """注意力模块已从 AU-only 版本移除。"""
        return None
    
    def reset_thread_stats(self, thread_name: str = None):
        """重置线程统计信息"""
        try:
            if thread_name is None:
                # 重置所有线程统计
                for thread in self.threads.values():
                    thread.reset_stats()
                logger.info("所有线程统计已重置")
            elif thread_name in self.threads:
                # 重置指定线程统计
                self.threads[thread_name].reset_stats()
                logger.info(f"线程 {thread_name} 统计已重置")
            else:
                logger.warning(f"线程 {thread_name} 不存在")
        except Exception as e:
            logger.error(f"重置线程统计失败: {e}")
    
    def cleanup(self):
        """清理所有资源"""
        try:
            # 停止数据收集器
            if hasattr(self, 'data_collector') and self.data_collector is not None:
                try:
                    self.data_collector.stop_collection()
                    logger.info("数据收集器已停止")
                except Exception as e:
                    logger.error(f"停止数据收集器失败: {e}")
            # 停止所有线程
            self.stop_all_threads()
            
            # 清理线程资源
            for thread_name, thread in self.threads.items():
                try:
                    thread.cleanup()
                except Exception as e:
                    logger.error(f"清理线程 {thread_name} 资源失败: {e}")
            
            # 清空线程字典
            self.threads.clear()
            self.thread_status.clear()
            
            self.initialized = False
            self.running = False
            
            logger.info("线程管理器资源已清理")
            
        except Exception as e:
            logger.error(f"清理线程管理器资源失败: {e}")
    
    def __enter__(self):
        """上下文管理器入口"""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器出口"""
        self.cleanup()


# 全局单例线程管理器
_global_thread_manager: Optional[CoreThreadManager] = None
_global_thread_manager_lock = threading.Lock()
_global_thread_manager_ref_count = 0  # 引用计数


def get_core_thread_manager( server_address : str, camera_id: int = 0, device: str = 'cuda', auto_start_camera: bool = True ) -> CoreThreadManager:
    """
    获取全局单例线程管理器
    确保多个API共享同一个线程管理器，摄像头线程只启动一次
    
    Args:
        camera_id: 摄像头ID（仅在首次创建时使用）
        device: 计算设备（仅在首次创建时使用）
        auto_start_camera: 是否自动启动摄像头线程（默认True）
        
    Returns:
        CoreThreadManager: 全局线程管理器实例
    """
    global _global_thread_manager, _global_thread_manager_ref_count

    with _global_thread_manager_lock:
        if _global_thread_manager is None:
            logger.info("创建全局线程管理器实例")
            _global_thread_manager = CoreThreadManager(
                server_address = server_address,
                camera_id=camera_id,
                device=device,
                
            )
            _global_thread_manager_ref_count = 0
        
        # 自动启动摄像头线程（如果未启动且auto_start_camera为True）
        # if auto_start_camera:
        #     # 初始化线程（如果尚未初始化）
        #     if not _global_thread_manager.is_initialized():
        #         if not _global_thread_manager.initialize():
        #             logger.error("线程管理器初始化失败")
        #         else:
        #             logger.info("线程管理器初始化成功")
            
        #     # 检查摄像头线程是否已启动
        #     camera_running = False
        #     if 'camera' in _global_thread_manager.threads:
        #         camera_thread = _global_thread_manager.threads['camera']
        #         camera_running = camera_thread.is_running()
            
        #     # 如果摄像头线程未启动，自动启动
        #     if not camera_running:
        #         logger.info("自动启动摄像头线程...")
        #         if _global_thread_manager.start_camera_thread():
        #             logger.info("摄像头线程自动启动成功")
        #         else:
        #             logger.error("摄像头线程自动启动失败")
        
        _global_thread_manager_ref_count += 1
        logger.info(f"线程管理器引用计数: {_global_thread_manager_ref_count}")
        return _global_thread_manager


def release_core_thread_manager():
    """
    释放线程管理器引用
    当引用计数为0时，可以选择是否清理资源
    """
    global _global_thread_manager, _global_thread_manager_ref_count
    
    with _global_thread_manager_lock:
        if _global_thread_manager_ref_count > 0:
            _global_thread_manager_ref_count -= 1
            logger.info(f"线程管理器引用计数: {_global_thread_manager_ref_count}")
        
        # 注意：这里不自动清理，因为其他API可能还在使用
        # 如果需要清理，应该显式调用 cleanup_global_thread_manager()


def cleanup_global_thread_manager():
    """
    清理全局线程管理器
    只有在确定没有其他API使用时才调用
    """
    global _global_thread_manager, _global_thread_manager_ref_count
    
    with _global_thread_manager_lock:
        if _global_thread_manager is not None:
            logger.info("清理全局线程管理器")
            _global_thread_manager.cleanup()
            _global_thread_manager = None
            _global_thread_manager_ref_count = 0


def get_core_data_manager() -> Optional[CoreDataManager]:
    """
    获取全局数据管理器
    
    Returns:
        CoreDataManager: 数据管理器实例，如果线程管理器未初始化则返回None
    """
    global _global_thread_manager
    
    if _global_thread_manager is not None:
        return _global_thread_manager.get_data_manager()
    return None


if __name__ == "__main__":
    thread_manager = CoreThreadManager()
    thread_manager.initialize()
    thread_manager.start_all_threads() 
    
    while True:
        time.sleep(1)
