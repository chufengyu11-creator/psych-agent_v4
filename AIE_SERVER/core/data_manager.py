#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
核心数据管理器 - 管理所有线程间的数据共享
"""

import os
import time
import threading
import queue
import logging
from typing import Dict, Any, Optional, List, Set, Tuple
import cv2
import numpy as np
from queue import Queue, Empty

# 导入新的日志配置
from .logger_config import get_logger

# 获取日志器
logger = get_logger(__name__)

class CoreDataManager:
    """核心数据管理器 - 管理所有线程间的数据共享"""
    
    def __init__(self):
        self._lock = threading.Lock()
        self._sync_lock = threading.Lock()
        
        # 图像数据
        self._current_frame = None
        self._current_timestamp = None
        self._image_sequence = 0
        
        # 图像队列：存储 (frame, timestamp, sequence) 元组
        self._image_queue = Queue(maxsize=10)
        
        # 为每个线程创建独立的图像队列（广播模式）
        self._face_image_queue = Queue(maxsize=5)
        self._heart_rate_image_queue = Queue(maxsize=5)
        self._attention_image_queue = Queue(maxsize=5)
        self._au_image_queue = Queue(maxsize=5)  # AU检测图像队列
        self._blink_image_queue = Queue(maxsize=5)  # 眨眼检测图像队列
                #新队列用于人脸识别区分不同人的检测结果,这是一个图像队列
        self._face_recognition_queue = Queue(maxsize=5)
        
        # 结果队列
        self._heart_rate_queue = Queue()
        self._attention_queue = Queue()
        self._face_queue = Queue()
        self._landmark_queue = Queue()
        self._au_queue = Queue()  # AU检测结果队列
        self._blink_queue = Queue()  # 眨眼检测结果队列
        self._emotion_queue = Queue()  # 情绪识别结果队列
        
        # 当前结果缓存
        self._heart_rate_result = None
        self._attention_result = None
        self._face_result = None
        self._landmark_result = None
        self._gaze_result = None  # 视线追踪结果
        
        # 结果历史记录（按序列号存储）
        self._face_results_history = {}  # {sequence: result}
        self._landmark_results_history = {}  # {sequence: result}
        self._heart_rate_results_history = {}  # {sequence: result}
        self._attention_results_history = {}  # {sequence: result}
        self._gaze_results_history = {}  # {sequence: result}
        
        # 摄像头FPS
        self._camera_fps = 0.0
        self._fps_lock = threading.Lock()
        
        # 调试时间
        self._debug_time = 0
        
        # 线程同步事件
        self._image_ready = threading.Event()  # 新图像准备就绪
        self._face_detection_completed = threading.Event()  # 人脸+关键点检测完成
        self._au_detection_completed = threading.Event()  # AU检测完成
        
        # 当前处理的序列号
        self._current_processing_sequence = 0
        
        # 眨眼检测结果缓存
        self._blink_result = None
        self._blink_results_history = {} # {sequence: blink_result}
        
        # AU检测结果缓存
        self._au_result = None
        self._au_results_history = {} # {sequence: au_result}
        
        # 情绪识别结果缓存
        self._emotion_result = None
        self._emotion_results_history = {} # {sequence: emotion_result}
        
        self.person_id = None
        
        # MongoDB 集成
        self._mongodb_enabled = os.environ.get('MONGODB_ENABLED', 'false').lower() == 'true'
        if self._mongodb_enabled:
            try:
                from database.mongodb_client_sync import mongodb_client_sync
                from database.repositories_sync import detection_result_repo_sync
                self._mongodb_client = mongodb_client_sync
                self._mongodb_repo = detection_result_repo_sync
                # 尝试连接
                if self._mongodb_client.connect():
                    logger.info("MongoDB 已启用并连接成功")
                else:
                    logger.warning("MongoDB 连接失败，将禁用数据库保存功能")
                    self._mongodb_enabled = False
            except ImportError as e:
                logger.warning(f"MongoDB 模块导入失败: {e}，将禁用数据库保存功能")
                self._mongodb_enabled = False
            except Exception as e:
                logger.warning(f"MongoDB 初始化失败: {e}，将禁用数据库保存功能")
                self._mongodb_enabled = False
        else:
            self._mongodb_client = None
            self._mongodb_repo = None
        
        logger.info("核心数据管理器已初始化")
    
    def get_debug_time(self) -> int:
        """获取调试时间"""
        return self._debug_time
    
    def increment_debug_time(self):
        """增加调试时间"""
        self._debug_time += 1
    
    def reset_debug_time(self):
        """重置调试时间"""
        self._debug_time = 0
    
    # 图像队列操作
    def put_image_to_queue(self, frame: np.ndarray, timestamp: float = None):
        """将图像放入队列"""
        if timestamp is None:
            timestamp = time.time()
        
        with self._lock:
            self._image_sequence += 1
            image_data = (frame.copy(), timestamp, self._image_sequence)
            
            # 如果队列满了，移除最旧的图像
            if self._image_queue.full():
                try:
                    self._image_queue.get_nowait()
                    #logger.info("图像队列已满，移除最旧图像")
                except Empty:
                    pass
            
            self._image_queue.put(image_data)
            logger.debug(f"图像已放入队列，序列号: {self._image_sequence}")
    
    def broadcast_image_to_all_threads(self, frame: np.ndarray, timestamp: float = None):
        """将图像广播到所有线程的队列（广播模式）"""
        if timestamp is None:
            timestamp = time.time()
        
        with self._lock:
            # 先递增序列号，然后使用递增后的值
            self._image_sequence += 1
            image_data = (frame.copy(), timestamp, self._image_sequence)
            
            # 更新当前帧缓存和时间戳
            self._current_frame = frame.copy()
            self._current_timestamp = timestamp
            
            # 广播到所有线程队列
            self._put_image_to_queue_if_not_full(self._face_image_queue, image_data)
            self._put_image_to_queue_if_not_full(self._heart_rate_image_queue, image_data)
            self._put_image_to_queue_if_not_full(self._attention_image_queue, image_data)
            self._put_image_to_queue_if_not_full(self._au_image_queue, image_data)  # AU检测队列
            self._put_image_to_queue_if_not_full(self._blink_image_queue, image_data)  # 眨眼检测队列
            self._put_image_to_queue_if_not_full(self._face_recognition_queue ,image_data)#用于人脸识别的图像队列
            
            # 设置图像就绪事件，重置其他事件
            self._image_ready.set()
            self.reset_all_events()
            
    def _put_image_to_queue_if_not_full(self, queue: Queue, image_data: Tuple):
        """如果队列未满，则放入图像"""
        if not queue.full():
            queue.put(image_data)
        else:
            # 队列满了，移除最旧的图像
            try:
                queue.get_nowait()
                queue.put(image_data)
                # logger.info("队列已满，移除最旧图像")
            except Empty:
                pass
    
    def get_image_from_queue(self, timeout: float = 1.0) -> Optional[Tuple[np.ndarray, float, int]]:
        """从队列获取图像"""
        try:
            return self._image_queue.get(timeout=timeout)
        except Empty:
            return None
    
    def get_image_for_face_detection(self, timeout: float = 1.0) -> Optional[Tuple[np.ndarray, float, int]]:
        """为人脸检测线程获取图像"""
        try:
            return self._face_image_queue.get(timeout=timeout)
        except Empty:
            return None
    
    def get_image_for_heart_rate(self, timeout: float = 1.0) -> Optional[Tuple[np.ndarray, float, int]]:
        """为心率分析线程获取图像"""
        try:
            return self._heart_rate_image_queue.get(timeout=timeout)
        except Empty:
            return None
    
    def get_image_for_attention(self, timeout: float = 1.0) -> Optional[Tuple[np.ndarray, float, int]]:
        """为注意力分析线程获取图像"""
        try:
            return self._attention_image_queue.get(timeout=timeout)
        except Empty:
            return None
    
    
    def get_image_for_au_detection(self, timeout: float = 1.0) -> Optional[Tuple[np.ndarray, float, int]]:
        """为AU检测线程获取图像"""
        try:
            return self._au_image_queue.get(timeout=timeout)
        except Empty:
            return None
    
    def get_image_for_blink_detection(self, timeout: float = 1.0) -> Optional[Tuple[np.ndarray, float, int]]:
        """为眨眼检测线程获取图像"""
        try:
            return self._blink_image_queue.get(timeout=timeout)
        except Empty:
            return None
    def get_image_for_face_recognition(self,timeout:float =1.0)-> Optional[Tuple[np.ndarray, float, int]]:
        try:
            return self._face_recognition_queue.get(timeout=timeout)
        except Empty:
            return None
    
    def get_current_frame(self) -> Optional[np.ndarray]:
        """获取当前帧"""
        return self._current_frame
    
    def get_current_timestamp(self) -> Optional[float]:
        """获取当前时间戳"""
        return self._current_timestamp
    
    def get_current_sequence(self) -> int:
        """获取当前序列号"""
        return self._image_sequence
    
    def get_image_ready_event(self) -> threading.Event:
        """获取图像就绪事件"""
        return self._image_ready
    
    def get_face_detection_completed_event(self) -> threading.Event:
        """获取人脸检测完成事件"""
        return self._face_detection_completed
    def get_au_detection_completed_event(self) -> threading.Event:
        """获取AU检测完成事件"""
        return self._au_detection_completed
    
    def reset_all_events(self):
        """重置所有事件"""
        self._image_ready.clear()
        self._face_detection_completed.clear()
        self._au_detection_completed.clear()
    
    def set_face_detection_completed(self):
        """设置人脸检测完成事件"""
        self._face_detection_completed.set()
    
    def set_au_detection_completed(self):
        """设置AU检测完成事件"""
        self._au_detection_completed.set()
    
    def wait_for_image_ready(self, timeout: float = 1.0) -> bool:
        """等待图像就绪事件"""
        return self._image_ready.wait(timeout=timeout)
    
    def wait_for_face_detection_completed(self, timeout: float = 1.0) -> bool:
        """等待人脸检测完成事件"""
        return self._face_detection_completed.wait(timeout=timeout)
    def wait_for_au_detection_completed(self, timeout: float = 1.0) -> bool:
        """等待AU检测完成事件"""
        return self._au_detection_completed.wait(timeout=timeout)
    
    # 结果更新方法
    def update_result(self, result: dict, result_type: str):
        """
        更新结果数据
        
        Args:
            result: 结果数据字典
            result_type: 结果类型 ('face', 'landmark', 'gaze', 'heart_rate', 'attention', 'au', 'blink')
        """
        with self._lock:
            if result_type == 'attention':
                # 同时更新注意力和视线追踪结果（合并功能）
                self._attention_result = result.copy()
                self._attention_queue.put(result)
                self._attention_results_history[result.get('image_sequence', 0)] = result.copy()
                
                # 保持历史记录在合理范围内
                if len(self._attention_results_history) > 10:
                    oldest_sequence = min(self._attention_results_history.keys())
                    del self._attention_results_history[oldest_sequence]
                    
                #logger.info(f"注意力/视线追踪结果已更新: {result.get('attention_flag', 'Unknown')}")
                
            elif result_type == 'face':
                self._face_result = result.copy()
                self._face_queue.put(result)
                self._face_results_history[result.get('image_sequence', 0)] = result.copy()
                # 保持历史记录在合理范围内
                if len(self._face_results_history) > 20:
                    oldest_sequence = min(self._face_results_history.keys())
                    del self._face_results_history[oldest_sequence]
                #logger.info(f"人脸检测结果已更新: {result.get('face_detected', False)}")
                
            elif result_type == 'landmark':
                self._landmark_result = result.copy()
                self._landmark_queue.put(result)
                self._landmark_results_history[result.get('image_sequence', 0)] = result.copy()
                # 保持历史记录在合理范围内
                if len(self._landmark_results_history) > 20:
                    oldest_sequence = min(self._landmark_results_history.keys())
                    del self._landmark_results_history[oldest_sequence]
                #logger.info(f"关键点检测结果已更新: {result.get('landmarks_detected', False)}")
                
            elif result_type == 'heart_rate':
                self._heart_rate_result = result.copy()
                self._heart_rate_queue.put(result)
                self._heart_rate_results_history[result.get('image_sequence', 0)] = result.copy()
                # 保持历史记录在合理范围内
                if len(self._heart_rate_results_history) > 20:
                    oldest_sequence = min(self._heart_rate_results_history.keys())
                    del self._heart_rate_results_history[oldest_sequence]
                #logger.info(f"心率检测结果已更新: {result.get('heart_rate', 0)}")
                
            elif result_type == 'au':
                self._au_result = result.copy()
                self._au_queue.put(result)
                self._au_results_history[result.get('image_sequence', 0)] = result.copy()
                # 保持历史记录在合理范围内
                if len(self._au_results_history) > 20:
                    oldest_sequence = min(self._au_results_history.keys())
                    del self._au_results_history[oldest_sequence]
                #logger.info(f"AU检测结果已更新: {result.get('au_detected', False)}")
                
            elif result_type == 'blink':
                self._blink_result = result.copy()
                self._blink_queue.put(result)
                self._blink_results_history[result.get('image_sequence', 0)] = result.copy()
                # 保持历史记录在合理范围内
                if len(self._blink_results_history) > 20:
                    oldest_sequence = min(self._blink_results_history.keys())
                    del self._blink_results_history[oldest_sequence]
                #logger.info(f"眨眼检测结果已更新: {result.get('blink_detected', False)}")
                
            elif result_type == 'emotion':
                self._emotion_result = result.copy()
                self._emotion_queue.put(result)
                self._emotion_results_history[result.get('image_sequence', 0)] = result.copy()
                # 保持历史记录在合理范围内
                if len(self._emotion_results_history) > 20:
                    oldest_sequence = min(self._emotion_results_history.keys())
                    del self._emotion_results_history[oldest_sequence]
                #logger.info(f"情绪识别结果已更新: {result.get('emotion_label', 'neutral')}")
            elif result_type == 'person_id':
                self.person_id = result.copy()
                logger.info("人脸识别结果更新")
            else:
                #logger.warning(f"未知的结果类型: {result_type}")
                pass
            
            # MongoDB 保存（方案B：直接在update_result中保存）
            if self._mongodb_enabled and self._mongodb_repo:
                self._save_to_mongodb(result, result_type)
    
    # 结果获取方法
    def get_face_result(self) -> Optional[Dict[str, Any]]:
        """获取人脸检测结果"""
        return self._face_result
    
    def get_landmark_result(self) -> Optional[Dict[str, Any]]:
        """获取关键点检测结果"""
        return self._landmark_result
    
    def get_gaze_result(self) -> Optional[Dict[str, Any]]:
        """获取视线追踪结果"""
        return self._gaze_result
    
    def get_heart_rate_result(self) -> Optional[Dict[str, Any]]:
        """获取心率检测结果"""
        return self._heart_rate_result
    
    def get_attention_result(self) -> Optional[Dict[str, Any]]:
        """获取注意力分析结果"""
        return self._attention_result
    
    def get_au_result(self) -> Optional[Dict[str, Any]]:
        """获取AU检测结果"""
        return self._au_result
    
    def get_blink_result(self) -> Optional[Dict[str, Any]]:
        """获取眨眼检测结果"""
        return self._blink_result
    
    def get_emotion_result(self) -> Optional[Dict[str, Any]]:
        """获取情绪识别结果"""
        return self._emotion_result
    
    def get_person_id_result(self) -> Optional[Dict[str, Any]]:
        return self.person_id
    
    # 队列获取方法
    def get_face_queue(self) -> Queue:
        """获取人脸检测队列"""
        return self._face_queue
    
    def get_landmark_queue(self) -> Queue:
        """获取关键点检测队列"""
        return self._landmark_queue
    
    def get_gaze_queue(self) -> Queue:
        """获取视线追踪队列"""
        return self._gaze_queue
    
    def get_heart_rate_queue(self) -> Queue:
        """获取心率检测队列"""
        return self._heart_rate_queue
    
    def get_attention_queue(self) -> Queue:
        """获取注意力分析队列"""
        return self._attention_queue
    
    def get_au_queue(self) -> Queue:
        """获取AU检测队列"""
        return self._au_queue
    
    def get_blink_queue(self) -> Queue:
        """获取眨眼检测队列"""
        return self._blink_queue
    
    def get_emotion_queue(self) -> Queue:
        """获取情绪识别队列"""
        return self._emotion_queue
    
    # 历史记录获取方法
    def get_face_results_history(self) -> Dict[int, Dict[str, Any]]:
        """获取人脸检测结果历史"""
        return self._face_results_history.copy()
    
    def get_landmark_results_history(self) -> Dict[int, Dict[str, Any]]:
        """获取关键点检测结果历史"""
        return self._landmark_results_history.copy()
    
    def get_gaze_results_history(self) -> Dict[int, Dict[str, Any]]:
        """获取视线追踪结果历史"""
        return self._gaze_results_history.copy()
    
    def get_heart_rate_results_history(self) -> Dict[int, Dict[str, Any]]:
        """获取心率检测结果历史"""
        return self._heart_rate_results_history.copy()
    
    def get_attention_results_history(self) -> Dict[int, Dict[str, Any]]:
        """获取注意力分析结果历史"""
        return self._attention_results_history.copy()
    
    def get_au_results_history(self) -> Dict[int, Dict[str, Any]]:
        """获取AU检测结果历史"""
        return self._au_results_history.copy()
    
    def get_blink_results_history(self) -> Dict[int, Dict[str, Any]]:
        """获取眨眼检测结果历史"""
        return self._blink_results_history.copy()
    
    def get_emotion_results_history(self) -> Dict[int, Dict[str, Any]]:
        """获取情绪识别结果历史"""
        return self._emotion_results_history.copy()
    
    def _save_to_mongodb(self, result: dict, result_type: str):
        """
        保存结果到MongoDB（方案B：直接在update_result中保存）
        
        Args:
            result: 结果数据字典
            result_type: 结果类型
        """
        try:
            # 获取图像序列号
            image_sequence = result.get('image_sequence', 0)
            if image_sequence == 0:
                return  # 跳过无效序列号
            
            # 获取时间戳
            timestamp = result.get('timestamp', self._current_timestamp)
            
            # 获取当前帧（复制以避免线程安全问题）
            frame = self._current_frame.copy() if self._current_frame is not None else None
            
            # 获取所有相关结果（按序列号）
            # 优先使用当前传入的result（如果result_type匹配），否则从历史记录获取
            if result_type == 'face':
                face_result = result
            else:
                face_result = self._face_results_history.get(image_sequence)
            
            if result_type == 'landmark':
                landmark_result = result
            else:
                landmark_result = self._landmark_results_history.get(image_sequence)
            
            if result_type == 'attention':
                attention_result = result
            else:
                attention_result = self._attention_results_history.get(image_sequence)
            
            if result_type == 'au':
                au_result = result
            else:
                au_result = self._au_results_history.get(image_sequence)
            
            if result_type == 'heart_rate':
                heart_rate_result = result
            else:
                heart_rate_result = self._heart_rate_results_history.get(image_sequence)
            
            if result_type == 'emotion':
                emotion_result = result
            else:
                emotion_result = self._emotion_results_history.get(image_sequence)
            
            # 保存到MongoDB（使用upsert，按image_sequence聚合）
            success = self._mongodb_repo.save_result(
                image_sequence=image_sequence,
                timestamp=timestamp,
                frame=frame,
                face_result=face_result,
                landmark_result=landmark_result,
                attention_result=attention_result,
                au_result=au_result,
                heart_rate_result=heart_rate_result,
                emotion_result=emotion_result,
                upsert=True
            )
            
            if not success:
                logger.debug(f"MongoDB保存失败: sequence={image_sequence}, type={result_type}")
                
        except Exception as e:
            # 静默失败，不影响主流程
            logger.debug(f"MongoDB保存异常: {e}")
    
    def set_mongodb_enabled(self, enabled: bool):
        """启用或禁用MongoDB保存"""
        self._mongodb_enabled = enabled
        if enabled and not self._mongodb_client:
            try:
                from database.mongodb_client_sync import mongodb_client_sync
                from database.repositories_sync import detection_result_repo_sync
                self._mongodb_client = mongodb_client_sync
                self._mongodb_repo = detection_result_repo_sync
                self._mongodb_client.connect()
            except Exception as e:
                logger.error(f"启用MongoDB失败: {e}")
                self._mongodb_enabled = False
    
    # FPS管理
    def set_camera_fps(self, fps: float):
        """设置摄像头FPS"""
        with self._fps_lock:
            self._camera_fps = fps
    
    def get_camera_fps(self) -> float:
        """获取摄像头FPS"""
        with self._fps_lock:
            return self._camera_fps
    
    # 序列号管理
    def get_current_processing_sequence(self) -> int:
        """获取当前处理的序列号"""
        return self._current_processing_sequence
    
    def set_current_processing_sequence(self, sequence: int):
        """设置当前处理的序列号"""
        self._current_processing_sequence = sequence
    
    def increment_processing_sequence(self):
        """增加处理序列号"""
        self._current_processing_sequence += 1 