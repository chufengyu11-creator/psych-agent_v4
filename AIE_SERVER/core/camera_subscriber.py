#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
摄像头 ZeroMQ 订阅者
从 CameraSUB.cpp 转换的 Python 版本
用于接收和显示通过 ZeroMQ 发布的摄像头视频流
"""

import time
import cv2
import zmq
import numpy as np
from collections import deque
import argparse
import struct
from datetime import datetime


class EnhancedImageHeader:
    """增强的图像头部（用于订阅时解析头部信息，匹配发布端格式）"""
    
    # 魔数：'MIMG' (Match Image) - 与发布端一致
    MAGIC = 0x4D494D47  # 'MIMG' in ASCII
    
    # 协议版本
    VERSION = 2
    
    # 结构体格式：与发布端完全一致
    # uint32_t magic, uint16_t version, uint32_t rows, uint32_t cols,
    # int32_t type, uint64_t data_size, uint32_t frame_id,
    # uint64_t send_timestamp_us, uint64_t capture_timestamp_us,
    # uint32_t expected_fps, uint32_t actual_fps
    STRUCT_FORMAT = '<I H I I i Q I Q Q I I'  # 小端序，与发布端一致
    
    # 头部大小（字节）
    SIZE = struct.calcsize(STRUCT_FORMAT)
    
    def __init__(self):
        """初始化头部"""
        self.magic = self.MAGIC
        self.version = self.VERSION
        self.rows = 0
        self.cols = 0
        self.type = 0  # OpenCV 类型，如 cv2.CV_8UC3
        self.data_size = 0
        self.frame_id = 0
        self.send_timestamp_us = 0  # 微秒时间戳
        self.capture_timestamp_us = 0  # 捕获时间戳
        self.expected_fps = 0
        self.actual_fps = 0
    
    @staticmethod
    def deserialize(data: bytes) -> 'EnhancedImageHeader':
        """
        从字节数据反序列化头部（匹配发布端格式）
        
        Args:
            data: 字节数据
            
        Returns:
            EnhancedImageHeader 对象
        """
        if len(data) < EnhancedImageHeader.SIZE:
            raise ValueError(f"数据长度不足: {len(data)} < {EnhancedImageHeader.SIZE}")
        
        # 解析二进制数据 - 使用与发布端相同的格式（小端序）
        unpacked = struct.unpack(EnhancedImageHeader.STRUCT_FORMAT, data[:EnhancedImageHeader.SIZE])
        
        header = EnhancedImageHeader()
        header.magic = unpacked[0]
        header.version = unpacked[1]
        header.rows = unpacked[2]
        header.cols = unpacked[3]
        header.type = unpacked[4]
        header.data_size = unpacked[5]
        header.frame_id = unpacked[6]
        header.send_timestamp_us = unpacked[7]
        header.capture_timestamp_us = unpacked[8]
        header.expected_fps = unpacked[9]
        header.actual_fps = unpacked[10]
        
        return header
    
    def get_send_time_string(self) -> str:
        """
        获取发送时间的字符串格式
        
        Returns:
            时间字符串
        """
        if self.send_timestamp_us == 0:
            return "N/A"
        
        # 将微秒时间戳转换为秒
        timestamp_sec = self.send_timestamp_us / 1000000.0
        dt = datetime.fromtimestamp(timestamp_sec)
        return dt.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]  # 精确到毫秒


class EnhancedImageSubscriber:
    """增强的图像订阅者（对应 C++ 的 EnhancedImageSubscriber）"""
    
    def __init__(self, server_address: str = "tcp://localhost:5556"):
        """
        初始化图像订阅者
        
        Args:
            server_address: ZeroMQ 服务器地址
        """
        self.server_address = server_address
        self.running = True
        
        # ZeroMQ 设置
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.SUB)
        self.socket.setsockopt(zmq.RCVHWM, 100)  # 接收高水位标记
        self.socket.setsockopt(zmq.LINGER, 0)    # 关闭时不等待
        
        # 重要：先订阅主题，再连接
        # 这样可以确保订阅过滤器在连接建立前就设置好
        self.socket.setsockopt_string(zmq.SUBSCRIBE, "enhanced_camera")  # 订阅增强版主题
        
        print(f"正在连接到 {server_address}...")
        try:
            self.socket.connect(server_address)
            print(f"成功连接到 {server_address}")
        except Exception as e:
            print(f"连接失败: {e}")
            raise
        
        # ZeroMQ PUB/SUB 需要等待连接建立
        # 订阅者需要先连接，发布者后发送才能收到消息
        # 在线程环境中，可能需要更长的等待时间
        print("等待连接建立...")
        time.sleep(1.0)  # 增加等待时间，确保连接建立
        
        # 尝试接收一次，检查连接是否正常（使用非阻塞模式）
        # 这不会阻塞，只是检查连接状态
        try:
            self.socket.setsockopt(zmq.RCVTIMEO, 100)  # 短暂超时
            # 不实际接收，只是检查 socket 状态
        except:
            pass
        
        # 统计信息
        self.frame_counter = 0
        self.frame_timestamps = deque()
        self.received_frames = 0
        self.total_bytes_received = 0
        self.start_time = time.time()
        
        print("=== 增强版图像订阅者 ===")
        print(f"服务器地址: {server_address}")
        print("使用协议版本: 2")
        print("控制键: S-切换统计显示, R-重置统计, ESC-退出")
        print("==========================")
    
    def receive_enhanced_frame(self, timeout_ms: int = 1000):
        """
        接收增强帧
        
        Args:
            timeout_ms: 接收超时时间（毫秒）
        
        Returns:
            tuple: (frame, header) 或 (None, None) 如果接收失败
        """
        self.socket.setsockopt(zmq.RCVTIMEO, timeout_ms)
        
        try:
            # 接收多部分消息
            # ZeroMQ PUB/SUB 模式发送的是多部分消息
            # 使用阻塞模式接收，直到收到完整的多部分消息
            parts = []
            more = True
            while more:
                try:
                    # 使用阻塞接收（不使用 NOBLOCK）
                    part = self.socket.recv()
                    parts.append(part)
                    # 检查是否还有更多部分
                    more = self.socket.getsockopt(zmq.RCVMORE)
                except zmq.Again:
                    # 超时
                    return None, None
                except zmq.ZMQError as e:
                    print(f"接收错误: {e}")
                    return None, None
            
            # 可能的消息格式：
            # 1. 三部分：主题、头部、数据（增强版）
            # 2. 两部分：主题、数据（简单版）
            # 3. 单部分：直接是数据
            
            if len(parts) == 3:
                # 增强版格式：主题、头部、数据
                topic_msg = parts[0]
                header_msg = parts[1]
                data_msg = parts[2]
                
                # 验证主题（允许不同的主题名称）
                try:
                    topic = topic_msg.decode('utf-8')
                except:
                    topic = None
                
                # 如果主题不匹配，尝试跳过主题检查（某些发布者可能使用不同的主题）
                if topic and topic not in ["enhanced_camera", "camera", "image"]:
                    # 仍然尝试处理，但记录警告
                    pass
            elif len(parts) == 2:
                # 简单版格式：主题、数据（无头部）
                topic_msg = parts[0]
                data_msg = parts[1]
                header_msg = None
                print(f"收到简单格式消息（无头部），主题: {topic_msg[:50] if len(topic_msg) < 50 else topic_msg[:50] + '...'}")
                # 对于简单格式，无法解析头部，返回 None
                return None, None
            elif len(parts) == 1:
                # 单部分：可能是直接的数据
                data_msg = parts[0]
                header_msg = None
                print(f"收到单部分消息，大小: {len(data_msg)} 字节")
                return None, None
            else:
                print(f"警告: 收到 {len(parts)} 部分消息，无法处理")
                return None, None
            
            if header_msg is None:
                return None, None
            
            # 验证头部大小
            if len(header_msg) != EnhancedImageHeader.SIZE:
                # 只在第一次或每100次打印一次，避免刷屏
                if not hasattr(self, '_header_size_warn_count'):
                    self._header_size_warn_count = 0
                self._header_size_warn_count += 1
                if self._header_size_warn_count <= 3 or self._header_size_warn_count % 100 == 0:
                    print(f"头部大小错误: {len(header_msg)} (期望 {EnhancedImageHeader.SIZE})")
                    # 尝试打印头部的前几个字节用于调试
                    if len(header_msg) > 0:
                        print(f"头部前16字节（十六进制）: {header_msg[:16].hex()}")
                return None, None
            
            # 解析增强头部
            try:
                header = EnhancedImageHeader.deserialize(header_msg)
            except Exception as e:
                # 只在第一次或每100次打印一次
                if not hasattr(self, '_deserialize_error_count'):
                    self._deserialize_error_count = 0
                self._deserialize_error_count += 1
                if self._deserialize_error_count <= 3 or self._deserialize_error_count % 100 == 0:
                    print(f"解析头部失败: {e}")
                    print(f"头部数据（十六进制）: {header_msg.hex()[:64]}...")
                return None, None
            
            # 验证头部
            if not self._is_valid_header(header):
                # 只在第一次或每100次打印一次
                if not hasattr(self, '_invalid_header_count'):
                    self._invalid_header_count = 0
                self._invalid_header_count += 1
                if self._invalid_header_count <= 3 or self._invalid_header_count % 100 == 0:
                    print(f"无效的头部数据 - magic: 0x{header.magic:X} (期望 0x{EnhancedImageHeader.MAGIC:X}), version: {header.version}, size: {header.cols}x{header.rows}")
                return None, None
            
            # 验证数据大小
            if len(data_msg) != header.data_size:
                print(f"数据大小不匹配: {len(data_msg)} (期望 {header.data_size})")
                return None, None
            
            # 重建图像
            # 将字节数据转换为 numpy 数组
            # 注意：frombuffer 创建的是只读数组，需要复制为可写数组
            image_data = np.frombuffer(bytes(data_msg), dtype=np.uint8)
            
            # 根据图像类型重建图像，并复制为可写数组
            if header.type == cv2.CV_8UC1:  # 灰度图
                image = image_data.reshape((header.rows, header.cols)).copy()
            elif header.type == cv2.CV_8UC3:  # BGR
                image = image_data.reshape((header.rows, header.cols, 3)).copy()
            elif header.type == cv2.CV_8UC4:  # BGRA
                image = image_data.reshape((header.rows, header.cols, 4)).copy()
            else:
                print(f"不支持的图像类型: {header.type}")
                return None, None
            
            # 更新统计
            self.received_frames += 1
            self.total_bytes_received += header.data_size
            self.frame_timestamps.append(time.time())
            
            # 保留最近5秒的时间戳
            while self.frame_timestamps and (time.time() - self.frame_timestamps[0]) > 5.0:
                self.frame_timestamps.popleft()
            
            return image, header
            
        except zmq.Again:
            # 接收超时（正常情况）
            return None, None
        except zmq.ZMQError as e:
            print(f"接收错误: {e}")
            return None, None
        except Exception as e:
            print(f"接收异常: {e}")
            return None, None
    
    def _is_valid_header(self, header: EnhancedImageHeader) -> bool:
        """验证头部有效性"""
        # 验证魔数
        if header.magic != EnhancedImageHeader.MAGIC:
            print(f"无效魔数: 0x{header.magic:X}")
            return False
        
        # 验证版本
        if header.version < 1 or header.version > 2:
            print(f"无效版本: {header.version}")
            return False
        
        # 验证图像尺寸
        if header.rows == 0 or header.rows > 10000 or header.cols == 0 or header.cols > 10000:
            print(f"无效图像尺寸: {header.cols}x{header.rows}")
            return False
        
        # 验证数据大小
        if header.data_size > 100 * 1024 * 1024:  # 不超过100MB
            print(f"数据大小过大: {header.data_size} bytes")
            return False
        
        return True
    
    def get_current_fps(self) -> float:
        """计算当前 FPS"""
        if len(self.frame_timestamps) < 2:
            return 0.0
        
        time_span = self.frame_timestamps[-1] - self.frame_timestamps[0]
        if time_span > 0:
            return len(self.frame_timestamps) / time_span
        return 0.0
    
    def get_avg_fps(self) -> float:
        """计算平均 FPS"""
        total_time = time.time() - self.start_time
        if total_time > 0:
            return self.received_frames / total_time
        return 0.0
    
    def overlay_frame_info(self, frame, header: EnhancedImageHeader):
        """在图像上叠加帧信息"""
        if frame is None or header is None:
            return
        
        current_fps = self.get_current_fps()
        
        # 计算延迟（毫秒）
        receive_time_us = int(time.time() * 1000000)
        latency_ms = (receive_time_us - header.send_timestamp_us) / 1000.0
        
        # 叠加信息
        info_y = 30
        line_height = 30
        
        # FPS 信息
        fps_text = f"FPS: {current_fps:.1f}"
        cv2.putText(frame, fps_text, (10, info_y),
                   cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        
        # 延迟信息
        info_y += line_height
        latency_color = (0, 0, 255) if latency_ms > 100 else (0, 255, 0)
        latency_text = f"延迟: {latency_ms:.1f} ms"
        cv2.putText(frame, latency_text, (10, info_y),
                   cv2.FONT_HERSHEY_SIMPLEX, 0.7, latency_color, 2)
        
        # 帧ID信息
        info_y += line_height
        frame_text = f"Frame: {header.frame_id}"
        cv2.putText(frame, frame_text, (10, info_y),
                   cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        
        # 分辨率信息
        info_y += line_height
        size_text = f"Size: {header.cols}x{header.rows}"
        cv2.putText(frame, size_text, (10, info_y),
                   cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
    
    def print_stats(self):
        """打印统计信息"""
        current_fps = self.get_current_fps()
        avg_fps = self.get_avg_fps()
        total_mb = self.total_bytes_received / (1024 * 1024)
        
        print(f"\r[订阅者] 已接收: {self.received_frames} 帧 | "
              f"数据: {total_mb:.2f} MB | "
              f"当前FPS: {current_fps:.1f} | "
              f"平均FPS: {avg_fps:.1f}", end='', flush=True)
    
    def handle_keyboard_input(self):
        """处理键盘输入"""
        #key = cv2.waitKey(1) & 0xFF
        key = 0
        
        if key == 27:  # ESC
            print("\n用户请求退出...")
            self.running = False
        elif key == ord('s') or key == ord('S'):
            # 切换统计显示（这里可以扩展）
            print("\n统计显示切换功能（待实现）")
        elif key == ord('r') or key == ord('R'):
            # 重置统计
            self.received_frames = 0
            self.total_bytes_received = 0
            self.frame_timestamps.clear()
            self.start_time = time.time()
            print("\n统计已重置")
    
    def run(self, show_window: bool = True):
        """运行订阅循环"""
        if show_window:
           # cv2.namedWindow("Enhanced Video Stream", cv2.WINDOW_AUTOSIZE)
           pass
        print("开始接收增强视频流...")
        print("提示: 如果长时间显示'等待数据...'，请检查：")
        print("  1. 发布者是否正在运行")
        print("  2. 发布者和订阅者的地址是否匹配")
        print("  3. 网络连接是否正常")
        print()
        
        display_counter = 0
        timeout_count = 0
        
        try:
            while self.running:
                # 接收图像
                frame, header = self.receive_enhanced_frame(timeout_ms=100)
                
                if frame is not None and header is not None:
                    timeout_count = 0  # 重置超时计数
                    
                    # 在图像上叠加帧信息
                    self.overlay_frame_info(frame, header)
                    
                    # 显示图像
                    if show_window:
                       # cv2.imshow("Enhanced Video Stream", frame)
                       pass
                    
                    # 每30帧显示一次头部信息
                    display_counter += 1
                    if display_counter % 30 == 0:
                        print(f"\n[接收] {header.get_send_time_string()} - "
                              f"帧 {header.frame_id} ({header.cols}x{header.rows}) - "
                              f"实际FPS: {header.actual_fps}/{header.expected_fps}")
                    
                    # 每10帧打印一次统计
                    if self.received_frames % 10 == 0:
                        self.print_stats()
                else:
                    # 接收超时
                    timeout_count += 1
                    if timeout_count % 50 == 0:  # 每5秒提示一次（50 * 100ms）
                        print(f"\r[状态] 等待数据... (已等待 {timeout_count * 0.1:.1f} 秒)", end='', flush=True)
                    else:
                        print("\r[状态] 等待数据...", end='', flush=True)
                
                # 处理键盘输入
                self.handle_keyboard_input()
                
        except KeyboardInterrupt:
            print("\n收到退出信号")
        finally:
            self.cleanup()
    
    def cleanup(self):
        """清理资源"""
        self.running = False
        
        if hasattr(self, 'socket'):
            self.socket.close()
        if hasattr(self, 'context'):
            self.context.term()
        
        #cv2.destroyAllWindows()
        
        # 打印最终统计
        print("\n\n=== 最终接收统计 ===")
        print(f"总接收帧数: {self.received_frames}")
        print(f"总接收数据: {self.total_bytes_received / (1024 * 1024):.2f} MB")
        if self.received_frames > 0:
            avg_fps = self.get_avg_fps()
            print(f"平均FPS: {avg_fps:.2f}")
        print("====================")


def main():
    """图像订阅者主函数"""
    parser = argparse.ArgumentParser(description='摄像头 ZeroMQ 订阅者')
    parser.add_argument('--address', type=str, default='tcp://192.168.8.109:5556',
                       help='ZeroMQ 服务器地址 (默认: tcp://localhost:5556)')
    parser.add_argument('--no-window', action='store_true',
                       help='不显示图像窗口')
    
    args = parser.parse_args()
    
    try:
        subscriber = EnhancedImageSubscriber(server_address=args.address)
        subscriber.run(show_window=not args.no_window)
    except Exception as e:
        print(f"程序错误: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    return 0


if __name__ == "__main__":
    main()
