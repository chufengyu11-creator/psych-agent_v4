#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AU检测线程 - 负责面部动作单元检测
"""

import time
import cv2
import numpy as np
from typing import Optional, Dict, Any, Tuple, List
from .base_thread import BaseThread
from .logger_config import get_logger
import pycuda.driver as cuda  # 添加这行
import pycuda.autoinit
# 导入模型
from models.au_detector import IntegratedAUDetector
from models.emotion_model import EmotionModel
import json 
# 获取日志器
logger = get_logger(__name__)

class AUDetectionThread(BaseThread):
    """AU检测线程 - 负责面部动作单元检测"""
    
    def __init__(self, data_manager, device: str = 'cpu',
                 auto_calibrate_baseline: bool = True,
                 baseline_samples_required: int = 8,
                 baseline_calibration_timeout: float = 3000.0,
                 enable_auto_recalibration: bool = True,
                 no_face_threshold: float = 1.0,
                 calibration_cooldown: float = 0.5):
        """
        初始化AU检测线程
        
        Args:
            data_manager: 数据管理器实例
            device: 计算设备
            auto_calibrate_baseline: 是否自动校准基线（启动时采集）
            baseline_samples_required: 基线校准所需样本数量
            baseline_calibration_timeout: 基线校准超时时间（秒）
        """
        super().__init__(name="AUDetectionThread", data_manager=data_manager, device=device)
        
        # AU检测模型
        self.au_model = None
        
        # 情绪识别模型
        self.emotion_model = None
        #是否需要清理模型
        self._need_cleanup = False
        # 检测结果缓存
        self._last_au_result = None
        self._last_emotion_result = None

        # AU配置
        self.target_aus = [1, 2, 4, 6, 12, 15, 20, 25]  # 常用AU单元
        self.confidence_threshold = 0.0
        
        # 基线校准相关
        self.auto_calibrate_baseline = auto_calibrate_baseline
        self.baseline_samples_required = baseline_samples_required
        self.baseline_calibration_timeout = baseline_calibration_timeout
        self._baseline_samples = []  # 存储中性表情样本
        self._baseline_calibration_start_time = None
        self._baseline_calibration_completed = False
        self.enable_auto_recalibration = enable_auto_recalibration
        self.no_face_threshold = no_face_threshold
        self.calibration_cooldown = calibration_cooldown
        self._last_calibration_time = 0.0
        self._no_face_start_time = None
        self._consecutive_no_face_duration = 0.0
        
        logger.info(f"AU检测线程已初始化 - 自动基线校准: {auto_calibrate_baseline}")
    
    def initialize_model(self) -> bool:
        """初始化AU检测模型和情绪识别模型"""
        try:
            self.au_model = IntegratedAUDetector()
            self.emotion_model = EmotionModel()
            logger.info(f"AU检测模型和情绪识别模型初始化成功")
            return True
        except Exception as e:
            logger.error(f"初始化模型失败: {e}")
            return False
    
    def run(self):
        """AU检测线程主循环"""
        if not self.initialize_model():
            logger.error("AU检测模型初始化失败，线程退出")
            return
        
        logger.info("AU检测线程开始运行")
        
        # 如果启用自动基线校准且未校准，先进行基线校准
        if self.auto_calibrate_baseline and not self._baseline_calibration_completed:
            logger.info("开始自动基线校准...")
            if self._perform_baseline_calibration():
                self._last_calibration_time = time.time()
                logger.info("基线校准完成，开始正常AU检测流程")
            else:
                logger.warning("基线校准失败或超时，继续使用未校准模式")
        try:
            while self.running:
                try:
                    # 检查是否被暂停
                    if not self.wait_if_paused():
                        continue
                    if self._need_cleanup:
                        print(f"au 线程需要清理，线程退出")
                        break  # 直接退出循环，结束线程
                
                    # 获取图像
                    image_data = self.data_manager.get_image_for_au_detection(1.0)
                    if image_data is None:
                        continue
                
                    frame, timestamp, sequence = image_data
                
                    # 等待人脸检测完成
                    if not self.data_manager.wait_for_face_detection_completed(1.0):
                        continue

                    # 获取人脸检测结果
                    face_result = self.data_manager.get_face_result()
                    if face_result is None or not face_result.get('face_detected', False):
                        if self.enable_auto_recalibration:
                            if self._no_face_start_time is None:
                                self._no_face_start_time = time.time()
                                logger.debug("检测到无人脸状态开始(AU线程)")
                            else:
                                self._consecutive_no_face_duration = time.time() - self._no_face_start_time
                        time.sleep(0.05)
                        continue
                    else:
                        if self._no_face_start_time is not None:
                            no_face_duration = time.time() - self._no_face_start_time
                            logger.info(f"人脸重新出现，无人脸持续时间: {no_face_duration:.1f} 秒")
                            if (self.enable_auto_recalibration and 
                                self._baseline_calibration_completed and 
                                no_face_duration >= self.no_face_threshold):
                                logger.info("检测到新用户，触发AU线程基线重新校准")
                                if self.trigger_recalibration():
                                    continue
                            self._no_face_start_time = None
                            self._consecutive_no_face_duration = 0

                    # 获取人脸关键点
                    landmarks = face_result.get('landmarks', [])
                    if landmarks.shape[0] < 10:
                        return None
                    face_bbox = face_result.get('face_bbox', None)
       
                    x, y, w, h = face_bbox
                    # 确保边界框在图像范围内
                    x = max(0, min(x, frame.shape[1] - 1))
                    y = max(0, min(y, frame.shape[0] - 1))
                    w = min(w, frame.shape[1] - x)
                    h = min(h, frame.shape[0] - y)
            
                    # 裁剪人脸区域
                    face_crop = frame[y:y+h, x:x+w]
            
                    # AU检测
                    au_start_time = time.time()
                    # TODO: 模型推理只需要图像
                    au_result = self.au_model.forward(face_crop, landmarks)
                    au_inference_time = (time.time() - au_start_time) * 1000  # 转换为毫秒
                    if au_result is None:
                        self._au_not_detected_count += 1
                        return self._create_no_au_result(timestamp, sequence)

                    # 处理AU结果 - 只保留AU编号和数值
                    processed_au_result = self._process_au_result(au_result)
                    # 创建简化的结果字典
                    result_au = {
                        'au_units': processed_au_result,  # AU编号和对应数值
                        'timestamp': timestamp,
                        'image_sequence': sequence,
                        'inference_time': au_inference_time
                    }
                    # 缓存结果
                    self._last_au_result = result_au
            
                    logger.custom(f"AU检测完成 序列号: {sequence}, "         
                                f"检测到AU: {processed_au_result}, "
                                f"推理时间: {au_inference_time:.2f}ms")
                    # 设置AU检测完成事件
                    self.data_manager.set_au_detection_completed()
                    # 更新结果到数据管理器
                    self.data_manager.update_result(result_au, 'au')

                    # ==================== 情绪识别（基于AU结果，必须完成基线校准）====================
                    if self.emotion_model is not None:
                        try:
                            # 如果启用了自动基线校准但校准未完成，跳过情绪识别（等待校准完成）
                            if self.auto_calibrate_baseline and not self._baseline_calibration_completed:
                                continue  # 跳过情绪识别，等待校准完成
                        
                            emotion_start_time = time.time()
                            emotion_result = self.emotion_model.forward(processed_au_result)
                            emotion_inference_time = (time.time() - emotion_start_time) * 1000  # 转换为毫秒
                        
                            if emotion_result:
                                result_emotion = {
                                    'emotion_label': emotion_result.get('label', 'neutral'),
                                    'emotion_confidence': emotion_result.get('confidence', 0.0),
                                    'emotion_probabilities': emotion_result.get('probabilities', {}),
                                    'timestamp': timestamp,
                                    'image_sequence': sequence,
                                }
                            
                                # 缓存结果
                                self._last_emotion_result = result_emotion
                            
                                logger.custom(f"emotion 识别完成 序列号: {sequence}, "
                                            f"情绪: {result_emotion['emotion_label']}, "
                                            f"置信度: {result_emotion['emotion_confidence']:.3f}, "
                                            f"推理时间: {emotion_inference_time:.2f}ms")
                            
                                # 更新结果到数据管理器
                                self.data_manager.update_result(result_emotion, 'emotion')
                            else:
                                logger.warning(f"情绪识别返回空结果，序列号: {sequence}")
                        except Exception as e:
                            logger.error(f"情绪识别异常: {e}")
                            import traceback
                            logger.error(traceback.format_exc())
                    else:
                        logger.warning("情绪模型未初始化，跳过情绪识别")
                    # ==================== 情绪识别结束 ====================
                    
                except Exception as e:
                    logger.error(f"AU检测线程运行出错: {e}")
        finally:
            # 无论什么原因退出，都要清理资源
            print(f"线程退出，执行最终清理")
            self.cleanup()
            self.running  = False
        
        logger.info("AU检测线程已停止")
    
    def process_frame(self, frame: np.ndarray, timestamp, sequence: int, face_result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """处理单帧图像"""
        try:
            if self.au_model is None:
                return None
            
            
        except Exception as e:
            logger.error(f"处理AU检测帧失败: {e}")
            return None
    
    def _process_au_result(self, au_result: Dict[str, Any]) -> Dict[str, Any]:
        """处理AU检测结果 - 只返回AU编号和对应数值"""
        try:
            processed_result = {}  
            # au_result 直接就是 {'AU1': 0.32526761293411255, 'AU2': 0.123456789} 格式
            for au_name, au_value in au_result.items():
                # 应用置信度阈值，只保留数值
                if au_value >= self.confidence_threshold:
                    processed_result[au_name] = au_value  # 直接使用AU名称和数值
            
            return processed_result
            
        except Exception as e:
            logger.error(f"处理AU结果失败: {e}")
            return {}
    
    def _create_no_au_result(self, timestamp, sequence: int) -> Dict[str, Any]:
        """创建无AU检测结果"""
        return {
            'au_units': {},  # 空的AU单元字典
            'timestamp': timestamp,
            'image_sequence': sequence,
            'inference_time': 0.0
        }
    
    
    def reset_au_stats(self):
        """重置AU检测统计"""
        logger.info("AU检测统计已重置")
    
    def get_last_au_result(self) -> Optional[Dict[str, Any]]:
        """获取最后一次AU检测结果"""
        return self._last_au_result
    
    def set_target_aus(self, au_ids: List[int]):
        """设置目标AU单元"""
        self.target_aus = au_ids
        logger.info(f"目标AU单元已设置为: {au_ids}")
    
    def set_confidence_threshold(self, threshold: float):
        """设置置信度阈值"""
        if 0.0 <= threshold <= 1.0:
            self.confidence_threshold = threshold
            logger.info(f"置信度阈值已设置为: {threshold}")
        else:
            logger.error("置信度阈值必须在0.0到1.0之间")
    
    def get_active_aus(self) -> List[str]:
        """获取当前活跃的AU单元"""
        if self._last_au_result is None:
            return []
        
        active_aus = []
        au_units = self._last_au_result.get('au_units', {})
        
        for au_name, au_data in au_units.items():
            if au_data.get('active', False):
                active_aus.append(au_name)
        
        return active_aus
    
    def get_au_intensity(self, au_id: int) -> float:
        """获取指定AU单元的强度"""
        if self._last_au_result is None:
            return 0.0
        
        au_units = self._last_au_result.get('au_units', {})
        au_name = f"AU{au_id}"
        
        if au_name in au_units:
            return au_units[au_name].get('intensity', 0.0)
        
        return 0.0
    
    def _perform_baseline_calibration(self) -> bool:
        """
        执行基线校准 - 自动采集中性表情样本（使用AU检测结果）
        注意：不依赖情绪模型判断，直接采集样本作为基线（相信用户在提示下能够做出中性表情）
        
        Returns:
            bool: 校准是否成功
        """
        if self.emotion_model is None:
            logger.error("情绪模型未初始化，无法进行基线校准")
            return False
        
        logger.info(f"[AU线程基线校准] 开始自动基线校准 - 需要采集 {self.baseline_samples_required} 个中性表情样本")
        logger.info("[AU线程基线校准] 请保持中性表情（平静、无表情），系统将自动采集样本...")
        
        self._baseline_samples = []
        self._baseline_calibration_start_time = time.time()
        calibration_timeout = self.baseline_calibration_timeout
        
        frame_count = 0
        last_sample_time = 0
        sample_interval = 0.3  # 样本采集间隔（秒），避免采集过于相似的帧
        min_au_value = 0.0  # AU最小值阈值（用于过滤无效数据）
        max_au_value = 1.0  # AU最大值阈值（用于过滤异常数据）
        
        while self.running and len(self._baseline_samples) < self.baseline_samples_required:
            # 检查超时
            if time.time() - self._baseline_calibration_start_time > calibration_timeout:
                logger.warning(f"[AU线程基线校准] 基线校准超时（{calibration_timeout}秒），已采集 {len(self._baseline_samples)} 个样本")
                break
            
            try:
                # 检查是否被暂停
                if not self.wait_if_paused():
                    time.sleep(0.1)
                    continue
                
                # 获取图像
                image_data = self.data_manager.get_image_for_au_detection(None)
                if image_data is None:
                    time.sleep(0.05)
                    continue
                
                frame, timestamp, sequence = image_data
                
                # 等待人脸检测完成
                if not self.data_manager.wait_for_face_detection_completed(None):
                    time.sleep(0.05)
                    continue

                # 获取人脸检测结果
                face_result = self.data_manager.get_face_result()
                if face_result is None or not face_result.get('face_detected', False):
                    time.sleep(0.05)
                    continue

                # 获取人脸关键点
                landmarks = face_result.get('landmarks', [])
                if landmarks.shape[0] < 10:
                    time.sleep(0.05)
                    continue
                
                face_bbox = face_result.get('face_bbox', None)
                if face_bbox is None:
                    time.sleep(0.05)
                    continue
       
                x, y, w, h = face_bbox
                # 确保边界框在图像范围内
                x = max(0, min(x, frame.shape[1] - 1))
                y = max(0, min(y, frame.shape[0] - 1))
                w = min(w, frame.shape[1] - x)
                h = min(h, frame.shape[0] - y)
            
                # 裁剪人脸区域
                face_crop = frame[y:y+h, x:x+w]
            
                # AU检测（用于基线校准）
                au_result = self.au_model.forward(face_crop, landmarks)
                if au_result is None:
                    time.sleep(0.05)
                    continue

                # 处理AU结果
                processed_au_result = self._process_au_result(au_result)
                
                frame_count += 1
                current_time = time.time()
                
                # 检查样本采集间隔（避免采集过于相似的帧）
                if current_time - last_sample_time < sample_interval:
                    time.sleep(0.05)
                    continue
                
                # 简单的数据质量检查（不判断情绪，只检查AU值是否在合理范围内）
                au_values = list(processed_au_result.values())
                if not au_values:
                    time.sleep(0.05)
                    continue
                
                # 检查AU值是否在合理范围内（过滤异常数据）
                max_au = max(au_values)
                min_au = min(au_values)
                
                # 如果AU值在合理范围内，直接采集样本
                if min_au_value <= min_au <= max_au_value and min_au_value <= max_au <= max_au_value:
                    self._baseline_samples.append(processed_au_result.copy())
                    last_sample_time = current_time
                    
                    logger.info(f"[AU线程基线校准] 采集基线样本 {len(self._baseline_samples)}/{self.baseline_samples_required} "
                               f"(帧: {frame_count}, AU范围: [{min_au:.3f}, {max_au:.3f}])")
                    
                    # 每采集一个样本，稍作延迟
                    time.sleep(sample_interval * 0.5)
                
                # 每10帧显示一次进度
                if frame_count % 10 == 0:
                    logger.info(f"[AU线程基线校准] 基线校准中... 已采集 {len(self._baseline_samples)}/{self.baseline_samples_required} 样本")
                
                time.sleep(0.05)  # 避免CPU占用过高
                
            except Exception as e:
                logger.error(f"[AU线程基线校准] 基线校准过程中出错: {e}")
                time.sleep(0.1)
        
        # 如果采集到足够样本，进行校准
        if len(self._baseline_samples) >= 5:  # 至少5个样本
            logger.info(f"[AU线程基线校准] 开始校准，样本数: {len(self._baseline_samples)}, emotion_model实例: {id(self.emotion_model)}")
            logger.info(f"[AU线程基线校准] 校准前状态: baseline_calibrated={self.emotion_model.baseline_calibrated}, baseline_au is None={self.emotion_model.baseline_au is None}")
            
            success = self.emotion_model.calibrate_baseline(
                self._baseline_samples, 
                min_samples=5
            )
            
            logger.info(f"[AU线程基线校准] 校准后状态: success={success}, baseline_calibrated={self.emotion_model.baseline_calibrated}, baseline_au is None={self.emotion_model.baseline_au is None}")
            
            if success:
                self._baseline_calibration_completed = True
                elapsed_time = time.time() - self._baseline_calibration_start_time
                self._last_calibration_time = time.time()
                logger.info(f"[AU线程基线校准] 基线校准成功！共采集 {len(self._baseline_samples)} 个样本，耗时 {elapsed_time:.2f} 秒")
                logger.info(f"[AU线程基线校准] 最终状态确认: baseline_calibrated={self.emotion_model.baseline_calibrated}, baseline_au is None={self.emotion_model.baseline_au is None}")
                return True
            else:
                logger.error("[AU线程基线校准] 基线校准失败")
                return False
        else:
            logger.warning(f"[AU线程基线校准] 基线校准样本不足（仅 {len(self._baseline_samples)} 个），校准失败")
            return False
    
    def cleanup(self):
        """清理资源"""
        try:
            self.au_model.cleanup()
            if self.au_model is not None:
                # 清理模型资源
                del self.au_model
                self.au_model = None
            if self.emotion_model is not None:
                del self.emotion_model
                self.emotion_model = None
            self._baseline_samples.clear()
            logger.info("AU检测线程资源已清理")
        except Exception as e:
            logger.error(f"清理AU检测线程资源失败: {e}")

    def trigger_recalibration(self) -> bool:
        """手动触发基线重新校准（多用户场景）"""
        current_time = time.time()
        elapsed = current_time - self._last_calibration_time
        if elapsed < self.calibration_cooldown:
            remaining = self.calibration_cooldown - elapsed
            logger.warning(f"[AU线程] 基线校准冷却中，剩余 {remaining:.1f} 秒")
            return False

        logger.info("[AU线程] 开始重新校准基线...")
        self._baseline_calibration_completed = False
        self._baseline_samples = []

        success = self._perform_baseline_calibration()
        if success:
            self._last_calibration_time = time.time()
            logger.info("[AU线程] 基线重新校准成功")
        else:
            logger.warning("[AU线程] 基线重新校准失败，继续使用旧基线")
        return success
    
    def get_stats(self) -> dict:
        """获取线程统计信息"""
        base_stats = super().get_stats()
        au_stats = self.get_au_detection_stats()
        base_stats.update(au_stats)
        return base_stats 
    def request_cleanup(self):
        """请求清理（可从任何线程调用）"""
        self._need_cleanup = True