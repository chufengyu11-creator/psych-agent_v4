#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
摄像头 ZeroMQ 发布器
从 CameraPUB.cpp 转换的 Python 版本
用于通过 ZeroMQ 发布摄像头视频流
"""

import time
import cv2
import struct
import zmq
from collections import deque
from datetime import datetime
import argparse


class EnhancedImageHeader:
    """增强的图像头部结构（对应 C++ 的 EnhancedImageHeader）"""
    MAGIC = 0x4D494D47  # "MIMG"
    VERSION = 2
    
    # 结构体格式：对应 C++ 的 #pragma pack(push, 1)
    # uint32_t magic, uint16_t version, uint32_t rows, uint32_t cols,
    # int32_t type, uint64_t data_size, uint32_t frame_id,
    # uint64_t send_timestamp_us, uint64_t capture_timestamp_us,
    # uint32_t expected_fps, uint32_t actual_fps
    STRUCT_FORMAT = '<I H I I i Q I Q Q I I'  # 小端序，对应 1 字节对齐
    
    SIZE = struct.calcsize(STRUCT_FORMAT)
    
    def __init__(self, rows=0, cols=0, img_type=0, data_size=0, 
                 frame_id=0, expected_fps=30, actual_fps=0):
        self.magic = self.MAGIC
        self.version = self.VERSION
        self.rows = rows
        self.cols = cols
        self.type = img_type
        self.data_size = data_size
        self.frame_id = frame_id
        self.expected_fps = expected_fps
        self.actual_fps = actual_fps
        
        # 获取当前时间戳（微秒）
        now_us = int(time.time() * 1000000)
        self.send_timestamp_us = now_us
        self.capture_timestamp_us = now_us
    
    def serialize(self) -> bytes:
        """序列化为字节流"""
        return struct.pack(self.STRUCT_FORMAT,
                          self.magic,
                          self.version,
                          self.rows,
                          self.cols,
                          self.type,
                          self.data_size,
                          self.frame_id,
                          self.send_timestamp_us,
                          self.capture_timestamp_us,
                          self.expected_fps,
                          self.actual_fps)
    
    @classmethod
    def deserialize(cls, data: bytes):
        """从字节流反序列化"""
        values = struct.unpack(cls.STRUCT_FORMAT, data)
        header = cls()
        header.magic = values[0]
        header.version = values[1]
        header.rows = values[2]
        header.cols = values[3]
        header.type = values[4]
        header.data_size = values[5]
        header.frame_id = values[6]
        header.send_timestamp_us = values[7]
        header.capture_timestamp_us = values[8]
        header.expected_fps = values[9]
        header.actual_fps = values[10]
        return header
    
    def get_send_time_string(self) -> str:
        """获取发送时间字符串"""
        seconds = self.send_timestamp_us // 1000000
        microseconds = self.send_timestamp_us % 1000000
        dt = datetime.fromtimestamp(seconds)
        return f"{dt.strftime('%H:%M:%S')}.{microseconds:06d}"
    
    def print(self):
        """打印头部信息"""
        print(f"Frame {self.frame_id}: {self.cols}x{self.rows}, "
              f"type={self.type}, size={self.data_size} bytes, "
              f"sent at {self.get_send_time_string()}, "
              f"FPS: {self.actual_fps}/{self.expected_fps}")


class EnhancedCameraPublisher:
    """增强的摄像头发布器（对应 C++ 的 EnhancedCameraPublisher）"""
    
    def __init__(self, address: str = "tcp://*:5556", camera_id: int = 0):
        """
        初始化摄像头发布器
        
        Args:
            address: ZeroMQ 绑定地址
            camera_id: 摄像头 ID
        """
        self.address = address
        self.camera_id = camera_id
        self.start_time = time.time()
        
        # ZeroMQ 设置
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.PUB)
        self.socket.setsockopt(zmq.SNDHWM, 100)  # 发送高水位标记
        self.socket.setsockopt(zmq.LINGER, 0)    # 关闭时不等待
        
        print(f"正在绑定到 {address}...")
        try:
            self.socket.bind(address)
            print(f"成功绑定到 {address}")
        except zmq.ZMQError as e:
            print(f"绑定失败: {e}")
            raise
        
        # 重要：ZeroMQ PUB/SUB 需要等待订阅者连接
        # 发布者绑定后，需要等待一段时间让订阅者连接
        print("等待订阅者连接（建议先启动订阅者）...")
        time.sleep(1.0)  # 增加等待时间，确保订阅者有时间连接
        
        # 打开摄像头
        if not self.open_camera(camera_id):
            raise RuntimeError("无法打开摄像头")
        
        # 统计信息
        self.frame_counter = 0
        self.frame_timestamps = deque()
        self.stats = {
            'total_frames_sent': 0,
            'total_bytes_sent': 0,
            'current_fps': 0.0,
            'avg_fps': 0.0
        }
        
        print("=== 增强版摄像头发布者 ===")
        print(f"地址: {address}")
        print(f"摄像头ID: {camera_id}")
        print("==========================")
    
    def open_camera(self, camera_id: int) -> bool:
        """打开摄像头"""
        self.cap = cv2.VideoCapture(camera_id)
        if not self.cap.isOpened():
            print(f"错误：无法打开摄像头 ID {camera_id}")
            return False
        
        # 设置摄像头参数
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        self.cap.set(cv2.CAP_PROP_FPS, 30)
        
        # 显示摄像头信息
        fps = self.cap.get(cv2.CAP_PROP_FPS)
        width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        
        print("摄像头信息:")
        print(f"  分辨率: {width}x{height}")
        print(f"  帧率: {fps} FPS")
        
        return True
    
    def update_fps(self):
        """更新 FPS 统计"""
        now = time.time()
        self.frame_timestamps.append(now)
        
        # 保留最近5秒的时间戳
        while self.frame_timestamps and (now - self.frame_timestamps[0]) > 5.0:
            self.frame_timestamps.popleft()
        
        # 计算当前FPS
        if len(self.frame_timestamps) >= 2:
            time_span = self.frame_timestamps[-1] - self.frame_timestamps[0]
            if time_span > 0:
                self.stats['current_fps'] = len(self.frame_timestamps) / time_span
        
        # 计算平均FPS
        total_time = now - self.start_time
        if total_time > 0:
            self.stats['avg_fps'] = self.stats['total_frames_sent'] / total_time
    
    def send_frame(self, frame) -> bool:
        """发送帧"""
        if frame is None or frame.size == 0:
            print("错误：试图发送空帧")
            return False
        
        try:
            self.frame_counter += 1
            frame_id = self.frame_counter
            frame_size = frame.nbytes
            
            # 确定 OpenCV 图像类型
            # Python OpenCV 使用 numpy，需要转换为 C++ OpenCV 类型
            if len(frame.shape) == 2:  # 灰度图
                img_type = cv2.CV_8UC1
            elif len(frame.shape) == 3:
                if frame.shape[2] == 3:  # BGR
                    img_type = cv2.CV_8UC3
                elif frame.shape[2] == 4:  # BGRA
                    img_type = cv2.CV_8UC4
                else:
                    img_type = cv2.CV_8UC1
            else:
                img_type = cv2.CV_8UC1
            
            # 创建增强的头部
            header = EnhancedImageHeader(
                rows=frame.shape[0],
                cols=frame.shape[1],
                img_type=img_type,
                data_size=frame_size,
                frame_id=frame_id,
                expected_fps=60
            )
            
            # 更新实际帧率
            header.actual_fps = int(self.stats['current_fps'])
            
            # 发送三部分消息
            # 1. 主题
            topic = b"enhanced_camera"
            self.socket.send(topic, zmq.SNDMORE)
            
            # 2. 增强的头部信息
            header_bytes = header.serialize()
            self.socket.send(header_bytes, zmq.SNDMORE)
            
            # 3. 图像数据（最后一部分，不使用 SNDMORE）
            frame_bytes = frame.tobytes()
            self.socket.send(frame_bytes, 0)
            
            # 更新统计
            self.stats['total_frames_sent'] += 1
            self.stats['total_bytes_sent'] += frame_size
            self.update_fps()
            
            # 每60帧显示一次头部信息
            if frame_id % 60 == 0:
                header.print()
            
            return True
            
        except zmq.ZMQError as e:
            print(f"发送错误: {e}")
            return False
        except Exception as e:
            print(f"发送异常: {e}")
            return False
    
    def print_stats(self):
        """打印统计信息"""
        print(f"\r[发布者] 已发送: {self.stats['total_frames_sent']} 帧 | "
              f"字节: {self.stats['total_bytes_sent'] // 1024} KB | "
              f"FPS: {self.stats['current_fps']:.1f}", end='', flush=True)
    
    def run(self, target_fps: int = 30):
        """运行发布循环"""
        frame_interval = 1.0 / target_fps
        
        print(f"开始视频传输 ({target_fps} FPS)...")
        print("按 ESC 键退出")
        
        try:
            while True:
                frame_start = time.time()
                
                # 捕获帧
                ret, frame = self.cap.read()
                if not ret or frame is None:
                    print("捕获帧失败")
                    time.sleep(0.1)
                    continue
                
                # 发送帧
                if not self.send_frame(frame):
                    print("发送帧失败")
                
                # 显示统计信息
                if self.frame_counter % 10 == 0:
                    self.print_stats()
                
                # 控制帧率
                frame_end = time.time()
                elapsed = frame_end - frame_start
                
                if elapsed < frame_interval:
                    time.sleep(frame_interval - elapsed)
                
                # 检查退出键
                #key = cv2.waitKey(1) & 0xFF
                key = 0
                if key == 27:  # ESC键
                    print("\n用户停止传输")
                    break
                    
        except KeyboardInterrupt:
            print("\n收到退出信号")
        finally:
            self.cleanup()
    
    def cleanup(self):
        """清理资源"""
        if hasattr(self, 'cap'):
            self.cap.release()
        if hasattr(self, 'socket'):
            self.socket.close()
        if hasattr(self, 'context'):
            self.context.term()
        #cv2.destroyAllWindows()
        print("资源已清理")


def main():
    """摄像头发布器主函数"""
    parser = argparse.ArgumentParser(description='摄像头 ZeroMQ 发布器')
    parser.add_argument('--address', type=str, default='tcp://localhost:5556',
                       help='ZeroMQ 绑定地址 (默认: tcp://*:5556)')
    parser.add_argument('--camera', type=int, default=0,
                       help='摄像头 ID (默认: 0)')
    parser.add_argument('--fps', type=int, default=30,
                       help='目标帧率 (默认: 30)')
    
    args = parser.parse_args()
    
    try:
        publisher = EnhancedCameraPublisher(
            address=args.address,
            camera_id=args.camera
        )
        publisher.run(target_fps=args.fps)
    except Exception as e:
        print(f"程序错误: {e}")
        return 1
    
    return 0


if __name__ == "__main__":
    main()
