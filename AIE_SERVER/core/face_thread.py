#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
人脸检测线程 - 负责人脸检测和关键点提取
"""

import time
import cv2
import numpy as np
from typing import Optional, Dict, Any, Tuple
from .base_thread import BaseThread
from .logger_config import get_logger

# 导入模型
from models.face_landmark_pose import FaceLandmarkPose

# 获取日志器
logger = get_logger(__name__)

class FaceDetectionThread(BaseThread):
    """人脸检测线程 - 负责人脸检测和关键点提取"""
    
    def __init__(self, data_manager, device: str = 'cpu'):
        """
        初始化人脸检测线程
        
        Args:
            data_manager: 数据管理器实例
            device: 计算设备
        """
        super().__init__(name="FaceDetectionThread", data_manager=data_manager, device=device)
        
        # 人脸检测模型
        self.face_model = None
        
        # 检测结果缓存
        self._last_face_result = None
        self._last_landmark_result = None
        
        # 检测统计
        #self._face_detected_count = 0
        #self._face_not_detected_count = 0
        
        logger.info("人脸检测线程已初始化")
    
    def initialize_model(self) -> bool:
        """初始化人脸检测模型"""
        try:
            self.face_model = FaceLandmarkPose()
            logger.info(f"人脸检测模型初始化成功，设备: {self.device}")
            return True
        except Exception as e:
            logger.error(f"初始化人脸检测模型失败: {e}")
            return False
    
    def run(self):
        """人脸检测线程主循环"""
        if not self.initialize_model():
            logger.error("人脸检测模型初始化失败，线程退出")
            return
        
        logger.info("人脸检测线程开始运行")
        
        while self.running:
            try:
                # 检查是否被暂停
                if not self.wait_if_paused():
                    continue
                
                # 获取图像
                image_data = self.data_manager.get_image_for_face_detection(timeout=1.0)
                if image_data is None:
                    continue
                
                frame, timestamp, sequence = image_data
                
                # 处理帧
                landmarks = self.face_model.get_landmarks(frame)
                if landmarks is not None:
                    # 处理不同类型的landmarks对象
                    if hasattr(landmarks, 'xmin') and hasattr(landmarks, 'ymin'):
                        # 对象类型，有xmin, ymin等属性
                        x = int(landmarks.xmin * frame.shape[1])
                        w = int(landmarks.width * frame.shape[1])
                        y = int(landmarks.ymin * frame.shape[0])
                        h = int(landmarks.height * frame.shape[0])
                    else:
                        # NumPy数组类型，需要计算边界框
                        if isinstance(landmarks, np.ndarray) and len(landmarks) > 0:
                            # 计算所有关键点的边界框
                            x_coords = landmarks[:, 0]
                            y_coords = landmarks[:, 1]
                            x = int(np.min(x_coords))
                            y = int(np.min(y_coords))
                            w = int(np.max(x_coords) - x)
                            h = int(np.max(y_coords) - y)
                        else:
                            # 默认值
                            x, y, w, h = 0, 0, frame.shape[1], frame.shape[0]
                    
                    # 确保边界框在图像范围内
                    x = max(0, min(x, frame.shape[1] - 1))
                    y = max(0, min(y, frame.shape[0] - 1))
                    w = min(w, frame.shape[1] - x)
                    h = min(h, frame.shape[0] - y)
                    result = {
                        'face_detected': True,
                        'face_bbox': [x, y, w, h],
                        'landmarks': landmarks,
                        'timestamp': timestamp,
                    }
                    #cv2.imwrite(f"images/face_{sequence}.jpg", frame[y:y+h, x:x+w])
                    #cv2.imwrite(f"images/face_{sequence}_whole.jpg", frame)
                    self.data_manager.update_result(result, 'face')

                else:
                    result = {
                        'face_detected': False,
                        'face_bbox': None,
                        'landmarks': None,
                        'timestamp': timestamp,
                    }
                    
                    # 重要：更新数据管理器中的结果
                    self.data_manager.update_result(result, 'face')

                # 设置人脸检测完成事件
                self.data_manager.set_face_detection_completed()
                    
            except Exception as e:
                logger.error(f"人脸检测线程运行出错: {e}")
                time.sleep(0.1)
        
        logger.info("人脸检测线程已停止")
    
    
    def get_last_face_result(self) -> Optional[Dict[str, Any]]:
        """获取最后一次人脸检测结果"""
        return self._last_face_result
    
    def cleanup(self):
        """清理资源"""
        try:
            if self.face_model is not None:
                # 清理模型资源
                del self.face_model
                self.face_model = None
            
            logger.info("人脸检测线程资源已清理")
        except Exception as e:
            logger.error(f"清理人脸检测线程资源失败: {e}")
    
