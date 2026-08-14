#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
摄像头线程 - 负责图像采集和分发
"""

import time
import cv2
import numpy as np
from .camera_subscriber import EnhancedImageSubscriber
from datetime import datetime
from typing import Optional, Tuple
from .logger_config import get_logger
from models.face_landmark_pose import FaceLandmarkPose
from .base_thread import BaseThread

# 获取日志器
logger = get_logger(__name__)

class CameraThread(BaseThread):
    """摄像头线程 - 负责图像采集和分发"""
    
    def __init__(self, camera_id: int, data_manager,server_address: str):
        """
        初始化摄像头线程
        
        Args:
            camera_id: 摄像头ID
            data_manager: 数据管理器实例
            device: 计算设备
        """
        super().__init__(name=f"CameraThread_{camera_id}", data_manager=data_manager)

        self.camera_id = camera_id
        self.cap = None
        self.frame_width = 0
        self.frame_height = 0
        self.fps = 0.0
        # 人脸检测模型
        self.face_model = None
        # 检测结果缓存
        self._last_face_result = None
        self._last_landmark_result = None
        self.server_address = server_address
        # 摄像头配置
        #self.target_fps = 30.0
        #self.frame_interval = 1.0 / self.target_fps
        
        # 性能监控
        #self._frame_count = 0
        #self._last_fps_time = time.time()
        
        logger.info(f"摄像头线程 {camera_id} 已初始化")
    
    def initialize_camera(self) -> bool:
        """初始化摄像头"""
        try:
            self.cap = cv2.VideoCapture(self.camera_id)
            self.face_model = FaceLandmarkPose()
            if not self.cap.isOpened():
                logger.error(f"无法打开摄像头 {self.camera_id}")
                return False
            
            # 获取实际参数
            #self.frame_width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            #self.frame_height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            actual_fps = self.cap.get(cv2.CAP_PROP_FPS)


            # 获取实际分辨率
            self.width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            self.height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            #self.cap.set(cv2.CAP_PROP_FRAME_WIDTH,  640)
            #self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

            logger.info(f"摄像头 {self.camera_id} 初始化成功 - "
                       f"分辨率: {self.frame_width}x{self.frame_height}, "
                       f"FPS: {actual_fps:.1f}")
            return True
            

        except Exception as e:
            logger.error(f"初始化摄像头 {self.camera_id} 失败: {e}")
            return False
    
    def get_current_time_string(self) -> str:
        """获取当前时间的字符串格式"""
        return datetime.now().strftime('%Y-%m-%d_%H:%M:%S.%f')[:-3]  # 精确到毫秒
    
    def run(self):
        """摄像头线程主循环"""
        #if not self.initialize_camera():
        #     logger.error(f"摄像头 {self.camera_id} 初始化失败，线程退出")
        #     return
        
        logger.info(f"摄像头线程 {self.camera_id} 开始运行")
        sequence = 0
        # 使用 Python 实现的 ZeroMQ 订阅者替代 .so 文件
        try:
            print(self.server_address)
            # subscriber = EnhancedImageSubscriber(server_address="tcp://127.0.0.1:5556")
            subscriber = EnhancedImageSubscriber(server_address=self.server_address)
            logger.info(f"ZeroMQ 订阅者初始化成功，连接到 tcp://192.168.8.100:5556")
            # 在线程环境中，给 ZeroMQ 连接额外的时间稳定
            time.sleep(0.5)
        except Exception as e:
            logger.error(f"初始化 ZeroMQ 订阅者失败: {e}")
            import traceback
            traceback.print_exc()
            return
        
        self.face_model = FaceLandmarkPose()
        
        # 统计信息，用于调试
        consecutive_failures = 0
        max_consecutive_failures = 100  # 连续失败100次后打印警告
        
        while self.running:
            try:
                # 检查是否被暂停
                if not self.wait_if_paused():
                    continue
                # 获取当前时间（可读格式）
                current_time = self.get_current_time_string()
                
                # 读取摄像头帧 - 使用 EnhancedImageSubscriber 接收
                # 增加超时时间到 100ms，给 ZeroMQ 足够的时间接收数据
                try:
                    frame, header = subscriber.receive_enhanced_frame(timeout_ms=100)
                
                    if frame is None or header is None:
                        consecutive_failures += 1
                        # 只在连续失败很多次后打印，避免刷屏
                        if consecutive_failures == 1:
                            logger.debug("等待接收图像数据...")
                        elif consecutive_failures % 100 == 0:
                            logger.warning(f"连续 {consecutive_failures} 次未接收到图像，请检查发布端是否运行")
                        continue
                    
                    # 成功接收到数据，重置失败计数
                    if consecutive_failures > 0:
                        logger.info(f"成功接收到图像数据（之前连续失败 {consecutive_failures} 次）")
                    consecutive_failures = 0
                    
                    # 第一次成功接收时打印确认信息
                    if sequence == 0:
                        logger.info(f"成功建立 ZeroMQ 连接并接收到第一帧图像: {header.cols}x{header.rows}, frame_id={header.frame_id}")
                    
                except Exception as e:
                    consecutive_failures += 1
                    if consecutive_failures <= 3 or consecutive_failures % 100 == 0:
                        logger.error(f"接收过程中出错: {e}")
                        import traceback
                        if consecutive_failures <= 3:
                            traceback.print_exc()
                    time.sleep(0.01)  # 出错后稍作等待
                    continue
                
                # 处理帧 - 使用当前时间字符串
                self.data_manager.broadcast_image_to_all_threads(frame, current_time)

                '''
                ===========================人脸检测===========================
                '''
                # 人脸检测
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
                    # 获取当前图像序列号（在broadcast_image_to_all_threads中已更新）
                    current_sequence = self.data_manager.get_current_sequence()
                    # 获取当前图像序列号（在broadcast_image_to_all_threads中已更新）
                    current_sequence = self.data_manager.get_current_sequence()
                    result = {
                        'face_detected': True,
                        'face_bbox': [x, y, w, h],
                        'landmarks': landmarks,
                        'timestamp': current_time,
                        'image_sequence': current_sequence,  # 添加序列号
                    }
                    sequence += 1
                    #cv2.imwrite(f"images/face_{sequence}.jpg", frame[y:y+h, x:x+w])
                    #cv2.imwrite(f"images/face_{sequence}_whole.jpg", frame)
                    self.data_manager.update_result(result, 'face')

                else:
                    # 获取当前图像序列号
                    current_sequence = self.data_manager.get_current_sequence()
                    result = {
                        'face_detected': False,
                        'face_bbox': None,
                        'landmarks': None,
                        'timestamp': current_time,
                        'image_sequence': current_sequence,  # 添加序列号
                    }
                
                    # 重要：更新数据管理器中的结果
                    self.data_manager.update_result(result, 'face')

                # 设置人脸检测完成事件
                self.data_manager.set_face_detection_completed()

                '''
                ===========================人脸检测结束===========================
                '''
                #last_frame_time = current_time
                
            except KeyboardInterrupt:
                logger.info(f"摄像头线程 {self.camera_id} 收到退出信号")
                break
            except Exception as e:
                logger.error(f"摄像头线程 {self.camera_id} 运行出错: {e}")
                print(e)
                time.sleep(0.1)
        
        # 清理资源
        try:
            subscriber.cleanup()
        except:
            pass
        logger.info(f"摄像头线程 {self.camera_id} 已停止")
    
    def cleanup(self):
        """清理摄像头资源"""
        try:
            if self.cap is not None:
                self.cap.release()
                self.cap = None
                logger.info(f"摄像头 {self.camera_id} 资源已释放")
        except Exception as e:
            logger.error(f"清理摄像头资源失败: {e}")

    #def _process_camera_frame(self, frame: np.ndarray, timestamp: float):
        #"""处理摄像头帧"""
        #try:
            ## 更新统计信息
            ##self._frame_count += 1
            #current_time = time.time()
            
            ### 计算FPS
            ##if current_time - self._last_fps_time >= 1.0:
                ##self.fps = self._frame_count / (current_time - self._last_fps_time)
                ##self._frame_count = 0
                ##self._last_fps_time = current_time
                
                ### 更新数据管理器中的FPS
                ##self.data_manager.set_camera_fps(self.fps)
            
            ## 广播图像到所有线程
            ##self.data_manager.broadcast_image_to_all_threads(frame, timestamp)
            
            ## 更新性能统计
            ##self._update_stats(0.001)  # 假设处理时间很短
            
        #except Exception as e:
            #logger.error(f"处理摄像头帧失败: {e}")
    

    #def get_camera_info(self) -> dict:
        #"""获取摄像头信息"""
        #if self.cap is None:
            #return {}
        
        #try:
            #return {
                #'camera_id': self.camera_id,
                #'frame_width': self.frame_width,
                #'frame_height': self.frame_height,
                #'fps': self.fps,
                #'is_opened': self.cap.isOpened()
            #}
        #except Exception as e:
            #logger.error(f"获取摄像头信息失败: {e}")
            #return {}
    
    def get_camera_info(self) -> dict:
        """获取摄像头信息"""
        if self.cap is None:
            return {}
        
        try:
            return {
                'camera_id': self.camera_id,
                'frame_width': self.frame_width,
                'frame_height': self.frame_height,
                'fps': self.fps,
                'is_opened': self.cap.isOpened(),
                'current_time': self.get_current_time_string()  # 添加当前时间信息
            }
        except Exception as e:
            logger.error(f"获取摄像头信息失败: {e}")
            return {}
    
    def get_current_time(self) -> str:
        """获取当前时间字符串"""
        return self.get_current_time_string()
    
    def get_stats(self) -> dict:
        """获取线程统计信息"""
        base_stats = super().get_stats()
        camera_stats = {
            'camera_id': self.camera_id,
            'frame_width': self.frame_width,
            'frame_height': self.frame_height,
            'fps': self.fps,
            'is_opened': self.cap.isOpened() if self.cap else False,
            'current_time': self.get_current_time_string()
        }
        base_stats.update(camera_stats)
        return base_stats 
