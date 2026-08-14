#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
注意力分析API - 简化版本
提供系统初始化和注意力数据获取功能
"""


import sys
import os
import threading
import time
import json
import base64
import cv2
import numpy as np
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import paho.mqtt.client as mqtt

from core.logger_config import get_logger
from core.thread_manager import release_core_thread_manager
from api.base_api import BaseAPI

logger = get_logger(__name__)

class apiEmo(BaseAPI):
    """注意力分析API类"""
    
    def __init__(self, camera_id: int = 0, device: str = 'cuda', 
                 mqtt_broker: str = "192.168.8.132", mqtt_port: int = 1883, mqtt_enable: bool = True,
                 gaze_only: bool = False,
                 au_only: bool = True,
                 face_classify_only : bool = True,
                 heart_rate_only : bool = False,
                 server_address :  str = "tcp://localhost:5554" ):
        """
        初始化注意力分析API
        
        Args:
            camera_id: 摄像头ID
            device: 计算设备
            mqtt_broker: MQTT Broker 地址 (默认 localhost)
            mqtt_port: MQTT 端口 (默认 1883)
            mqtt_enable: 是否启用 MQTT
            gaze_only: 兼容旧参数，AU-only 版本不再启动视线线程
        """
        super().__init__(server_address, camera_id, device)
        self._gaze_only = gaze_only
        self._au_only = au_only
        self._heart_rate_only = heart_rate_only
        # AU/emotion pipeline requires image frames, face landmarks, then AU inference.
        self._required_threads.clear()
        self._required_threads.add('face_detection')
        if au_only:
            self._required_threads.add('au')
            
        
        # 后台保持线程相关属性
        self._keepalive_thread = None  # 后台保持线程对象
        
        # MQTT 相关属性
        self._mqtt_client = None
        self._mqtt_broker = mqtt_broker or "localhost"
        self._mqtt_port = mqtt_port
        self._mqtt_base_topic = "emotion"  # 基础主题路径
        self._mqtt_enable = mqtt_enable
        self._mqtt_connected = False
        self._mqtt_publisher_thread = None
        self._mqtt_stop_event = threading.Event()
        
        # MQTT 数据去重：缓存上次发布的序列号，避免重复发送相同数据
        self._last_published_gaze_sequence = -1
        self._last_published_au_sequence = -1
        self._last_published_heartrate_sequence = -1
        self._last_published_emotion_sequence = -1
        self._last_published_image_sequence = -1
        self._last_face_detected_state = None  # 上次发布的人脸检测状态
        
        # 图像压缩质量（1-100，数值越大质量越高但文件越大）
        self._image_jpeg_quality = 85
        
        if self._mqtt_enable:
            logger.info(f"MQTT 已启用 - Broker: {self._mqtt_broker}:{self._mqtt_port}, Base Topic: {self._mqtt_base_topic}")
        logger.info("AU/情绪分析API 初始化完成")
    
    def wait(self):
        super().wait()
    
    def _daemon_thread(self):
        """
        启动后台保持线程，保持程序运行
        这个线程会调用 wait() 来保持程序运行，但不阻塞主线程
        """
        if self._keepalive_thread is not None and self._keepalive_thread.is_alive():
            logger.warning("保持线程已在运行，跳过启动")
            return
        
        def daemon_worker():
            """后台线程工作函数"""
            try:
                # 等待系统稳定
                self.wait()
            except Exception as e:
                logger.error(f"保持线程异常: {e}")
                import traceback
                logger.error(traceback.format_exc())
        
        # 创建并启动 daemon 线程
        self._keepalive_thread = threading.Thread(
            target=daemon_worker,
            name=f"{self.__class__.__name__}_keepalive_{self._api_id}",
            daemon=True  # 设置为 daemon，主程序退出时自动退出
        )
        self._keepalive_thread.start()
        logger.info("后台保持线程已启动，主线程可继续执行")
    
    def init(self) -> bool:
        """
        Returns:
            bool: 是否启动成功
        """
        # 调用父类的 start() 方法
        success = super().start()
        
        # 如果启动成功，启动后台保持线程
        if success:
            self._daemon_thread()
            # 如果启用 MQTT，启动 MQTT 客户端
            if self._mqtt_enable:
                self.start_mqtt_client()
        return success
    
    #def get_attention(self) -> dict:
    #    """
    #    获取注意力数据
    #    返回pitch、yaw和focusing
    #    
    #    Returns:
    #        dict: {
    #            'pitch': float,  # 俯仰角（度）
    #            'yaw': float,     # 偏航角（度）
    #            'focusing': int   # 是否专注 (0或1)
    #        }
    #        如果获取失败，返回None
    #    """
    #    try:
    #        
    #        # 从数据管理器获取注意力结果
    #        attention_result = self.data_manager.get_attention_result()
    #        if attention_result:
    #            return {
    #                'pitch': float(attention_result.get('attention_pitch', 0.0)),
    #                'yaw': float(attention_result.get('attention_yaw', 0.0)),
    #                'focusing': int(attention_result.get('focusing', 0))
    #            }
    #        return None
    #        
    #    except Exception as e:
    #        import traceback
    #        error_info = traceback.format_exc()
    #        logger.error(f"获取注意力数据失败: {e}\n错误详情:\n{error_info}")
    #        return None

    def getPesonID(self):
        """
        获取Emotion数据
        """    
        person_id_result = self.data_manager.get_person_id_result()
        if person_id_result is not None:
            return {
                'person_id': person_id_result.get('person_id'),
                }
        return 'Unknown'
    def getEmoGaze(self):
        """
        获取Gaze数据
        """    
        # 从数据管理器获取注意力结果
        attention_result = self.data_manager.get_attention_result()
        person_id = self.getPesonID()
        if attention_result:
            # 处理时间戳：如果是字符串则保持，如果是数字则转换为浮点数
            timestamp = attention_result.get('timestamp', 0.0)
            if isinstance(timestamp, str):
                # 时间戳是字符串格式，保持为字符串
                timestamp_value = timestamp
            else:
                # 时间戳是数字，转换为浮点数
                timestamp_value = float(timestamp) if timestamp else 0.0
            
            return {
                'person_id':person_id,
                'pitch': float(attention_result.get('attention_pitch', 0.0)),
                'yaw': float(attention_result.get('attention_yaw', 0.0)),
                'focusing': int(attention_result.get('focusing', 0)),
                'timestamp': timestamp_value,
                'image_sequence': int(attention_result.get('image_sequence', 0))
            }
        return None

    def getEmoAU(self):
        """
        获取AU数据（包含情绪映射结果）
        """    
        au_result = self.data_manager.get_au_result()
        person_id = self.getPesonID()
        if au_result:
            # 处理时间戳：如果是字符串则保持，如果是数字则转换为浮点数
            timestamp = au_result.get('timestamp', 0.0)
            if isinstance(timestamp, str):
                # 时间戳是字符串格式，保持为字符串
                timestamp_value = timestamp
            else:
                # 时间戳是数字，转换为浮点数
                timestamp_value = float(timestamp) if timestamp else 0.0
            
            # 获取情绪映射结果
            emotion_result = self.data_manager.get_emotion_result()
            emotion_data = {}
            if emotion_result:
                # 处理情绪时间戳
                emotion_timestamp = emotion_result.get('timestamp', 0.0)
                if isinstance(emotion_timestamp, str):
                    emotion_timestamp_value = emotion_timestamp
                else:
                    emotion_timestamp_value = float(emotion_timestamp) if emotion_timestamp else 0.0
                
                emotion_data = {
                    'person_id':person_id,
                    'emotion_label': emotion_result.get('emotion_label', 'neutral'),
                    'emotion_confidence': float(emotion_result.get('emotion_confidence', 0.0)),
                    'emotion_probabilities': emotion_result.get('emotion_probabilities', {}),
                    'emotion_timestamp': emotion_timestamp_value,
                    'emotion_image_sequence': int(emotion_result.get('image_sequence', 0))
                }
            
            result = {
                'au_units': au_result.get('au_units', {}),
                'timestamp': timestamp_value,
                'image_sequence': int(au_result.get('image_sequence', 0)),
                'inference_time': float(au_result.get('inference_time', 0.0))
            }
            
            # 添加情绪映射结果
            if emotion_data:
                result.update(emotion_data)
            
            return result
        return None

    def getEmoBlink(self):
        """
        TODO: 实现获取Blink数据
        """    
        pass

    def getEmoHeartRate(self):
        """
        获取HeartRate数据
        """    
        heart_rate_result = self.data_manager.get_heart_rate_result()
        person_id = self.getPesonID()
        if heart_rate_result:
            # 处理时间戳：如果是字符串则保持，如果是数字则转换为浮点数
            timestamp = heart_rate_result.get('timestamp', 0.0)
            if isinstance(timestamp, str):
                # 时间戳是字符串格式，保持为字符串
                timestamp_value = timestamp
            else:
                # 时间戳是数字，转换为浮点数
                timestamp_value = float(timestamp) if timestamp else 0.0
            
            return {
                'person_id':person_id,
                'heart_rate': float(heart_rate_result.get('heart_rate', 9999)),
                'hrv': float(heart_rate_result.get('hrv', 9999)),
                'timestamp': timestamp_value,
                'image_sequence': int(heart_rate_result.get('image_sequence', 0))
            }
        return None

    def getEmoEmotion(self):
        """
        获取Emotion数据
        """    
        emotion_result = self.data_manager.get_emotion_result()
        person_id = self.getPesonID()
        if emotion_result:
            # 处理时间戳：如果是字符串则保持，如果是数字则转换为浮点数
            timestamp = emotion_result.get('timestamp', 0.0)
            if isinstance(timestamp, str):
                # 时间戳是字符串格式，保持为字符串
                timestamp_value = timestamp
            else:
                # 时间戳是数字，转换为浮点数
                timestamp_value = float(timestamp) if timestamp else 0.0
            
            return {
                'person_id':person_id,
                'emotion_label': emotion_result.get('emotion_label', 'neutral'),
                'emotion_confidence': float(emotion_result.get('emotion_confidence', 0.0)),
                'emotion_probabilities': emotion_result.get('emotion_probabilities', {}),
                'timestamp': timestamp_value,
                'image_sequence': int(emotion_result.get('image_sequence', 0))
            }
        return None

    
    # ==================== MQTT 相关方法 ====================
    
    def _init_mqtt_client(self):
        """初始化 MQTT 客户端"""
        try:
            # 创建客户端
            client_id = "apiEmo"
            self._mqtt_client = mqtt.Client(client_id=client_id)
            
            # 设置回调函数
            self._mqtt_client.on_connect = self._on_mqtt_connect
            self._mqtt_client.on_disconnect = self._on_mqtt_disconnect
            
            logger.info(f"MQTT 客户端已初始化: {client_id}")
            return True
        except Exception as e:
            logger.error(f"初始化 MQTT 客户端失败: {e}")
            return False
    
    def _on_mqtt_connect(self, client, userdata, flags, rc):
        """MQTT 连接回调"""
        if rc == 0:
            self._mqtt_connected = True
            logger.info("MQTT 连接成功")
        else:
            self._mqtt_connected = False
            logger.error(f"MQTT 连接失败，错误码: {rc}")
    
    def _on_mqtt_disconnect(self, client, userdata, rc):
        """MQTT 断开连接回调"""
        self._mqtt_connected = False
        logger.info("MQTT 连接断开")
    
    def _connect_mqtt(self) -> bool:
        """连接 MQTT Broker"""
        if self._mqtt_client is None:
            if not self._init_mqtt_client():
                return False
        
        try:
            # 连接 Broker
            self._mqtt_client.connect(self._mqtt_broker, self._mqtt_port, keepalive=60)
            # 启动网络循环（非阻塞）
            self._mqtt_client.loop_start()
            
            # 等待连接建立（最多等待3秒）
            for _ in range(30):
                if self._mqtt_connected:
                    return True
                time.sleep(0.1)
            
            logger.warning("MQTT 连接超时")
            return False
        except Exception as e:
            logger.error(f"连接 MQTT Broker 失败: {e}")
            return False
    
    def _get_mqtt_topic(self, data_type: str) -> str:
        """
        生成 MQTT 主题
        
        Args:
            data_type: 数据类型 ('gaze', 'heart_rate', 'au', 'blink', 'image' 等)
        
        Returns:
            str: 完整的主题路径
        """
        return f"{self._mqtt_base_topic}/{data_type}"
    
    def _publish_data(self, data_type: str, data: dict):
        """
        发布数据到 MQTT
        
        Args:
            data_type: 数据类型 ('gaze', 'heart_rate', 'au', 'blink', 'image', 'face_status')
            data: 要发布的数据字典
        """
        try:
            if not self._mqtt_connected or not data:
                return
            
            topic = self._get_mqtt_topic(data_type)
            payload = json.dumps(data)
            result = self._mqtt_client.publish(topic, payload, qos=0)
            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                logger.warning(f"MQTT 发布失败 ({data_type})，错误码: {result.rc}")
        except Exception as e:
            logger.error(f"发布 {data_type} 数据失败: {e}")
    
    def _publish_gaze_data(self):
        """发布 gaze 数据到 MQTT（仅当数据更新时发布）"""
        gaze_data = self.getEmoGaze()
        if gaze_data:
            current_sequence = gaze_data.get('image_sequence', -1)
            # 只有当序列号变化时才发布（避免重复发送相同数据）
            if current_sequence != self._last_published_gaze_sequence:
                self._publish_data('gaze', gaze_data)
                self._last_published_gaze_sequence = current_sequence
        # 注意：如果 gaze_data 为 None，说明 gaze_attention 线程可能未运行或没有检测到人脸
    
    def _publish_heart_rate_data(self):
        """发布心率数据到 MQTT（仅当数据更新时发布）"""
        heart_rate_data = self.getEmoHeartRate()
        if heart_rate_data:
            current_sequence = heart_rate_data.get('image_sequence', -1)
            # 只有当序列号变化时才发布（避免重复发送相同数据）
            if current_sequence != self._last_published_heartrate_sequence:
                self._publish_data('heartrate', heart_rate_data)
                self._last_published_heartrate_sequence = current_sequence
    
    def _publish_au_data(self):
        """发布 AU 数据到 MQTT（仅当数据更新时发布）"""
        au_data = self.getEmoAU()
        if au_data:
            current_sequence = au_data.get('image_sequence', -1)
            # 只有当序列号变化时才发布（避免重复发送相同数据）
            if current_sequence != self._last_published_au_sequence:
                # 调试：检查发送的AU数据
                au_units = au_data.get('au_units', {})
                au_keys = sorted(au_units.keys())
                au_count = len(au_keys)
                if 'AU25' not in au_units or 'AU26' not in au_units:
                    logger.warning(f"[MQTT发送] 序列号{current_sequence}: AU数量={au_count}, 缺少AU25或AU26, 键: {au_keys}")
                self._publish_data('au', au_data)
                self._last_published_au_sequence = current_sequence
    
    def _publish_emotion_data(self):
        """发布情绪数据到 MQTT（仅当数据更新时发布，独立于AU数据）"""
        emotion_data = self.getEmoEmotion()
        if emotion_data:
            current_sequence = emotion_data.get('image_sequence', -1)
            # 只有当序列号变化时才发布（避免重复发送相同数据）
            if current_sequence != self._last_published_emotion_sequence:
                self._publish_data('emotion', emotion_data)
                self._last_published_emotion_sequence = current_sequence
    
    def _publish_face_status(self):
        """发布人脸检测状态到 MQTT（仅当状态变化时发布）"""
        try:
            # 获取人脸检测结果
            face_result = self.data_manager.get_face_result()
            current_face_detected = False
            if face_result:
                current_face_detected = face_result.get('face_detected', False)
            # 只有当状态变化时才发布（避免重复发送相同状态）
            if current_face_detected != self._last_face_detected_state:
                # 获取时间戳和序列号
                timestamp = time.time()
                if face_result:
                    timestamp = face_result.get('timestamp', timestamp)
                    if isinstance(timestamp, str):
                        timestamp_value = timestamp
                    else:
                        timestamp_value = float(timestamp) if timestamp else time.time()
                else:
                    timestamp_value = timestamp
                
                current_sequence = self.data_manager.get_current_sequence()
                
                face_detected_data = {
                        'face_detected': current_face_detected,
                        'timestamp': timestamp_value,
                        'image_sequence': current_sequence,
                }
                self._publish_data('face_status', face_detected_data)
                print("===============================> face_detected_data: ", face_detected_data)

                # 更新状态
                self._last_face_detected_state = current_face_detected
        except Exception as e:
            logger.error(f"发布人脸状态异常: {e}")
    
    def _publish_person_id(self):
        """发布personID数据到 MQTT（仅当心率数据更新时发布）"""
        person_id = self.getPesonID()
        heart_rate_data = self.getEmoHeartRate()
        if person_id:   
            self._publish_data('person_id', person_id)
            logger.info("MQTT PERSON")
                
       







    def _encode_image_to_base64(self, frame: np.ndarray) -> str:
        """
        将图像编码为 base64 字符串
        
        Args:
            frame: OpenCV 图像 (BGR 格式)
        
        Returns:
            str: base64 编码的 JPEG 图像字符串
        """
        try:
            # 将图像编码为 JPEG 格式（压缩）
            encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), self._image_jpeg_quality]
            success, encoded_image = cv2.imencode('.jpg', frame, encode_param)
            if not success:
                logger.error("图像编码失败")
                return None
            
            # 转换为 base64 字符串
            image_base64 = base64.b64encode(encoded_image.tobytes()).decode('utf-8')
            return image_base64
        except Exception as e:
            logger.error(f"图像编码异常: {e}")
            return None
    
    def _publish_image_data(self):
        """发布图像数据到 MQTT（仅当数据更新时发布）"""
        try:
            # 获取当前帧
            current_frame = self.data_manager.get_current_frame()
            if current_frame is None:
                return
            
            # 获取当前序列号
            current_sequence = self.data_manager.get_current_sequence()
            
            # 只有当序列号变化时才发布（避免重复发送相同帧）
            if current_sequence != self._last_published_image_sequence:
                # 编码图像为 base64
                image_base64 = self._encode_image_to_base64(current_frame)
                if image_base64:
                    # 获取时间戳
                    timestamp = self.data_manager.get_current_timestamp()
                    if timestamp is None:
                        timestamp = time.time()
                    
                    # 构建图像数据
                    image_data = {
                        'image': image_base64,  # base64 编码的 JPEG 图像
                        'image_sequence': current_sequence,
                        'timestamp': timestamp,
                        'width': int(current_frame.shape[1]),
                        'height': int(current_frame.shape[0]),
                        'format': 'jpeg'  # 图像格式
                    }
                    
                    # 发布图像数据
                    self._publish_data('image', image_data)
                    self._last_published_image_sequence = current_sequence
        except Exception as e:
            logger.error(f"发布图像数据异常: {e}")
    
    def _mqtt_publisher_loop(self):
        """MQTT 发布线程循环"""
        logger.info("MQTT 发布线程已启动")
        
        while not self._mqtt_stop_event.is_set():
            try:
                if self._mqtt_connected:
                    # 发布人脸检测状态（仅当状态变化时发布）
                    self._publish_face_status()
                    
                    if self._au_only:
                        self._publish_au_data()
                        self._publish_emotion_data()
                
                time.sleep(0.005)  # 30ms 间隔，约 33.3Hz 发布频率
            except Exception as e:
                logger.error(f"MQTT 发布线程异常: {e}")
                time.sleep(0.03)
        logger.info("MQTT===============================DFADFASDFADFASFADAF")
        logger.info("MQTT 发布线程已停止")
    
    def start_mqtt_client(self, broker: str = None, port: int = None) -> bool:
        """
        启动 MQTT 客户端
        Args:
            broker: MQTT Broker 地址（覆盖初始化时的设置）
            port: MQTT 端口（覆盖初始化时的设置）
        
        Returns:
            bool: 是否启动成功
        """
        if not self._mqtt_enable:
            logger.warning("MQTT 未启用")
            return False
        
        # 更新配置（如果提供了参数）
        if broker:
            self._mqtt_broker = broker
        if port:
            self._mqtt_port = port
        
        # 初始化客户端
        if not self._init_mqtt_client():
            return False
        
        # 连接 Broker
        if not self._connect_mqtt():
            return False
        
        # 启动发布线程
        if self._mqtt_publisher_thread is None or not self._mqtt_publisher_thread.is_alive():
            self._mqtt_stop_event.clear()
            self._mqtt_publisher_thread = threading.Thread(
                target=self._mqtt_publisher_loop,
                name=f"{self.__class__.__name__}_mqtt_publisher",
                daemon=True
            )
            self._mqtt_publisher_thread.start()
            logger.info("MQTT 发布线程已启动")
        
        return True
    
    def stop_mqtt_client(self):
        """停止 MQTT 客户端"""
        try:
            # 停止发布线程
            if self._mqtt_publisher_thread is not None and self._mqtt_publisher_thread.is_alive():
                self._mqtt_stop_event.set()
                self._mqtt_publisher_thread.join(timeout=2.0)
                self._mqtt_publisher_thread = None
                logger.info("MQTT===============================")
                logger.info("MQTT 发布线程已停止")
            
            # 断开连接
            if self._mqtt_client:
                self._mqtt_client.loop_stop()
                self._mqtt_client.disconnect()
                self._mqtt_connected = False
                logger.info("MQTT 客户端已断开")
        except Exception as e:
            logger.error(f"停止 MQTT 客户端失败: {e}")

    def cleanup(self):
        """
        清理资源 - 停止后台保持线程和 MQTT 客户端
        """
        try:
            # 先停止 MQTT 客户端
            if self._mqtt_client:
                self.stop_mqtt_client()
            
            # 停止后台保持线程
            if self._keepalive_thread is not None and self._keepalive_thread.is_alive():
                logger.info("正在停止后台线程...")
                # 停止 wait() 方法（唤醒阻塞的 wait()）
                self.stop()  # 这会设置 _stop_event，唤醒 wait() 方法
                # 等待线程结束（设置超时，避免无限等待）
                self._keepalive_thread.join(timeout=2.0)
                if self._keepalive_thread.is_alive():
                    logger.warning("保持线程未在超时时间内结束")
                else:
                    logger.info("后台保持线程已停止")
                self._keepalive_thread = None
            
            # 然后执行原有的清理逻辑
            if self.thread_manager and self._api_id:
                # 先注销本API的线程需求
                self.thread_manager.unregister_thread_requirement(self._api_id)
                
                # 停止本API需要的处理线程
                for thread_name in self._required_threads:
                    if thread_name in self.thread_manager.threads:
                        thread = self.thread_manager.threads[thread_name]
                        if thread.is_running():
                            thread.stop()
                
                # 释放全局线程管理器引用
                release_core_thread_manager()
                self.thread_manager = None
                self.data_manager = None
                logger.info("注意力分析API资源已清理")

        except Exception as e:
            import traceback
            error_info = traceback.format_exc()
            logger.error(f"清理注意力分析API资源失败: {e}\n错误详情:\n{error_info}")


def create_attention_api(camera_id: int = 0, device: str = 'cuda', gaze_only: bool = True):
    """创建注意力分析API实例；gaze_only=True 时仅视线，便于标定。"""
    return apiEmo(camera_id=camera_id, device=device, gaze_only=gaze_only)


# def main():
#     """
#     主函数 - 直接运行此脚本时，启动推理程序在后台运行
#     """
#     import signal
    
#     # 创建 API 实例（默认仅视线模式，便于标定；若要完整管线则 gaze_only=False）
#     api = apiEmo(
#         camera_id=0,
#         device='cuda',
#         mqtt_broker="0.0.0.0",  # 根据实际情况修改
#         mqtt_port=1883,
#         mqtt_enable=True,
#         gaze_only=True
#     )
    
#     # 信号处理函数
#     def signal_handler(sig, frame):
#         logger.info("\n收到退出信号，正在清理资源...")
#         try:
#             api.cleanup()
#             # 等待一下让线程完全退出
#             time.sleep(2.0)
#         except Exception as e:
#             logger.error(f"清理资源时出错: {e}")
#         logger.info("程序已退出")
#         import sys
#         sys.exit(0)
    
#     # 注册信号处理
#     signal.signal(signal.SIGINT, signal_handler)
#     signal.signal(signal.SIGTERM, signal_handler)
    
#     try:
#         # 初始化系统
#         logger.info("正在初始化系统...")
#         if not api.init():
#             logger.error("系统初始化失败")
#             return
        
#         logger.info("系统初始化成功，推理程序已在后台运行")
#         if api._gaze_only:
#             logger.info("MQTT 数据正在发布到: emotion/gaze, emotion/face_status, emotion/image")
#         else:
#             logger.info("MQTT 数据正在发布到: emotion/gaze, emotion/heartrate, emotion/au, emotion/emotion, emotion/face_status, emotion/image")
#         logger.info("按 Ctrl+C 退出程序")
        
#         # 主线程保持运行（后台线程会自动处理所有工作）
#         # 使用 wait() 方法保持程序运行，不消耗 CPU
#         api.wait()
        
#     except KeyboardInterrupt:
#         logger.info("收到中断信号")
#     except Exception as e:
#         logger.error(f"程序运行出错: {e}")
#         import traceback
#         logger.error(traceback.format_exc())
#     finally:
#         # 清理资源
#         logger.info("正在清理资源...")
#         api.cleanup()
#         time.sleep(2.0)  # 等待线程完全退出
#         logger.info("程序已退出")


# if __name__ == "__main__":
#     main()


