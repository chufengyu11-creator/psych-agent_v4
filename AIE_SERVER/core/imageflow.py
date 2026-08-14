# cv_subscriber.py
import ctypes
import numpy as np
import cv2
import time

class CVSubscriber:
    """
    用于接收C++传输的cv::Mat图像的Python类
    """
    
    # OpenCV数据类型到numpy数据类型的映射
    CV_TYPE_TO_DTYPE = {
        0: np.uint8,   # CV_8U
        1: np.int8,    # CV_8S
        2: np.uint16,  # CV_16U
        3: np.int16,   # CV_16S
        4: np.int32,   # CV_32S
        5: np.float32, # CV_32F
        6: np.float64  # CV_64F
    }
    
    # OpenCV类型到通道数的映射（不完全，但常见类型）
    CV_TYPE_TO_CHANNELS = {
        0: 1,   # CV_8UC1
        8: 1,   # CV_8UC1 的另一种表示
        16: 3,  # CV_8UC3
        24: 4   # CV_8UC4
    }
    
    def __init__(self, lib_path):
        """
        初始化
        
        Args:
            lib_path: C++动态库路径
        """
        # 加载库
        self.lib = ctypes.CDLL(lib_path)
        
        # 配置所有函数原型
        self._setup_function_prototypes()
        
        # 订阅者指针
        self._subscriber_ptr = None
        
        # 统计信息
        self.frame_count = 0
        self.start_time = None
    
    def _setup_function_prototypes(self):
        """设置所有C函数的参数和返回类型"""
        
        # create_subscriber
        self.lib.create_Subscriber.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        self.lib.create_Subscriber.restype = ctypes.c_void_p
        
        # destroy_subscriber
        self.lib.destroy_Subscriber.argtypes = [ctypes.c_void_p]
        self.lib.destroy_Subscriber.restype = None
        
        # receive_frame
        self.lib.receive_frame.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.lib.receive_frame.restype = ctypes.c_void_p
        
        # 获取帧信息的函数
        self.lib.get_frame_width.argtypes = [ctypes.c_void_p]
        self.lib.get_frame_width.restype = ctypes.c_int
        
        self.lib.get_frame_height.argtypes = [ctypes.c_void_p]
        self.lib.get_frame_height.restype = ctypes.c_int
        
        self.lib.get_frame_channels.argtypes = [ctypes.c_void_p]
        self.lib.get_frame_channels.restype = ctypes.c_int
        
        self.lib.get_frame_type.argtypes = [ctypes.c_void_p]
        self.lib.get_frame_type.restype = ctypes.c_int
        
        self.lib.get_frame_data.argtypes = [ctypes.c_void_p]
        self.lib.get_frame_data.restype = ctypes.POINTER(ctypes.c_ubyte)
        
        self.lib.get_frame_data_size.argtypes = [ctypes.c_void_p]
        self.lib.get_frame_data_size.restype = ctypes.c_size_t
        
        self.lib.get_frame_id.argtypes = [ctypes.c_void_p]
        self.lib.get_frame_id.restype = ctypes.c_int
        
        self.lib.get_timestamp.argtypes = [ctypes.c_void_p]
        self.lib.get_timestamp.restype = ctypes.c_longlong
        
        # free_frame_data
        self.lib.free_frame_data.argtypes = [ctypes.c_void_p]
        self.lib.free_frame_data.restype = None
    
    def connect(self, server_address="tcp://localhost:5556", 
                log_file="enhanced_stats.csv"):
        """
        连接到服务器
        
        Args:
            server_address: ZMQ服务器地址
            log_file: 日志文件路径
            
        Returns:
            bool: 是否连接成功
        """
        if self._subscriber_ptr:
            print("警告: 已经存在订阅者，先销毁")
            self.disconnect()
        
        try:
            # 转换为bytes
            server_bytes = server_address.encode('utf-8')
            log_bytes = log_file.encode('utf-8')
            
            # 调用C函数创建订阅者
            self._subscriber_ptr = self.lib.create_Subscriber(server_bytes, log_bytes)
            
            if self._subscriber_ptr:
                self.frame_count = 0
                self.start_time = time.time()
                return True
            else:
                print("❌ 连接失败")
                return False
                
        except Exception as e:
            print(f"❌ 连接时出错: {e}")
            return False
    
    def disconnect(self):
        """断开连接并清理资源"""
        if self._subscriber_ptr:
            self.lib.destroy_Subscriber(self._subscriber_ptr)
            self._subscriber_ptr = None
            print("✅ 已断开连接")
    
    def receive(self, timeout_ms=1000, show_info=True):
        """
        接收一帧图像
        
        Args:
            timeout_ms: 超时时间（毫秒）
            show_info: 是否显示帧信息
            
        Returns:
            numpy.ndarray or None: 图像数据，失败返回None
            dict: 帧的元信息
        """
        if not self._subscriber_ptr:
            print("❌ 错误: 未连接到服务器")
            return None, {}
        

     
            # 超时，继续尝试
  

        frame_data_ptr = None
        retry_count = 0
        max_retries = 100  # 防止无限循环

        while frame_data_ptr is None and retry_count < max_retries:
            # 尝试接收
            frame_data_ptr = self.lib.receive_frame(self._subscriber_ptr, timeout_ms)

            if frame_data_ptr is None:
                retry_count += 1
                #print(f"等待接收帧... (尝试 {retry_count}/{max_retries})")
        
        try:
            # 获取帧信息
            width = self.lib.get_frame_width(frame_data_ptr)
            height = self.lib.get_frame_height(frame_data_ptr)
            channels = self.lib.get_frame_channels(frame_data_ptr)
            cv_type = self.lib.get_frame_type(frame_data_ptr)
            frame_id = self.lib.get_frame_id(frame_data_ptr)
            timestamp = self.lib.get_timestamp(frame_data_ptr)
            
            # 获取数据指针和大小
            data_ptr = self.lib.get_frame_data(frame_data_ptr)
            data_size = self.lib.get_frame_data_size(frame_data_ptr)
            
            if width == 0 or height == 0 or data_size == 0:
                print("❌ 错误: 无效的帧数据")
                self.lib.free_frame_data(frame_data_ptr)
                return None, {}
            
            # 确定numpy数据类型
            # 从OpenCV类型中提取深度（数据类型）
            depth = cv_type & 7  # 取低3位
            
            if depth in self.CV_TYPE_TO_DTYPE:
                dtype = self.CV_TYPE_TO_DTYPE[depth]
            else:
                print(f"⚠️  未知的数据类型: {depth}, 使用uint8作为默认")
                dtype = np.uint8
            
            # 确定实际的通道数
            # 如果channels为0，尝试从cv_type推断
            if channels <= 0:
                # OpenCV类型格式: CV_{位数}{类型}C{通道数}
                # 例如: CV_8UC3 = 16
                if cv_type in self.CV_TYPE_TO_CHANNELS:
                    channels = self.CV_TYPE_TO_CHANNELS[cv_type]
                else:
                    channels = 1  # 默认单通道
            
            # 计算期望的数据大小
            expected_size = width * height * channels * np.dtype(dtype).itemsize
            
            if expected_size != data_size:
                print(f"⚠️  数据大小不匹配: 期望 {expected_size}, 实际 {data_size}")
                # 仍然尝试处理
            
            # 创建numpy数组
            # 方法1: 使用ctypes直接创建视图（更快，但更复杂）
            try:
                # 将ctypes指针转换为numpy数组
                buffer = ctypes.cast(data_ptr, ctypes.POINTER(ctypes.c_ubyte * data_size)).contents
                np_arr = np.frombuffer(buffer, dtype=dtype)
                
                # 重塑形状
                if channels == 1:
                    image = np_arr.reshape((height, width))
                elif channels == 3:
                    image = np_arr.reshape((height, width, channels))
                    # OpenCV使用BGR，转换为RGB用于显示（如果需要）
                    # image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
                elif channels == 4:
                    image = np_arr.reshape((height, width, channels))
                    # 移除alpha通道或保留
                else:
                    # 未知通道数，返回扁平数组
                    image = np_arr
                
            except Exception as e:
                print(f"❌ 创建numpy数组失败: {e}")
                self.lib.free_frame_data(frame_data_ptr)
                return None, {}
            
            # 统计信息
            self.frame_count += 1
            elapsed = time.time() - self.start_time
            fps = self.frame_count / elapsed if elapsed > 0 else 0
            
            # 帧信息
            frame_info = {
                'frame_id': frame_id,
                'width': width,
                'height': height,
                'channels': channels,
                'cv_type': cv_type,
                'data_size': data_size,
                'fps': fps,
                'total_frames': self.frame_count
            }
            
            if show_info:
                self._print_frame_info(frame_info)
            
            # 释放C++中的帧数据
            self.lib.free_frame_data(frame_data_ptr)
            
            return image, frame_info
            
        except Exception as e:
            print(f"❌ 处理帧数据时出错: {e}")
            import traceback
            traceback.print_exc()
            
            # 确保释放内存
            if frame_data_ptr:
                self.lib.free_frame_data(frame_data_ptr)
            
            return None, {}
    
    def _print_frame_info(self, info):
        """打印帧信息"""
        print(f"\n📊 帧 #{info['frame_id']}")
        print(f"  尺寸: {info['width']}x{info['height']}")
        print(f"  通道: {info['channels']}")
        print(f"  大小: {info['data_size']:,} 字节")
        print(f"  FPS: {info['fps']:.2f}")
        print(f"  总帧数: {info['total_frames']}")

    
    def receive_loop(self, callback=None, timeout_ms=1000, max_frames=None):
        """
        连续接收帧的循环
        
        Args:
            callback: 处理每帧的回调函数，接收(image, info)参数
            timeout_ms: 超时时间
            max_frames: 最大接收帧数，None表示无限
            
        Returns:
            int: 实际接收的帧数
        """
        print(f"🎬 开始接收视频流 (超时: {timeout_ms}ms)")
        

        
        try:
            while True:
                if max_frames and frame_count >= max_frames:
                    print(f"达到最大帧数限制: {max_frames}")
                    break
                
                # 接收帧
                image, info = self.receive(timeout_ms, show_info=False)
                
                if image is None:
                    print("没有图像")
                    continue

                
        except KeyboardInterrupt:
            print("\n 用户中断")
       
           
        
        return frame_count
    
    def __enter__(self):
        """支持with语句"""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """退出时自动清理"""
        self.disconnect()

# # 使用示例
# if __name__ == "__main__":
#     def example_high_performance():
#         print("\n=== 示例3: 高性能接收")
    
#         frame_count = 0
#         total_bytes = 0
#         start_time = time.time()
    
#         def count_frames(image, info):
#             nonlocal frame_count, total_bytes
#             frame_count += 1
#             total_bytes += info['data_size']
        
#             if frame_count % 50 == 0:
#                 elapsed = time.time() - start_time
#                 fps = frame_count / elapsed
#                 mbps = (total_bytes / elapsed) / (1024 * 1024)
#             print(f"已接收 {frame_count} 帧, FPS: {fps:.1f}, 带宽: {mbps:.2f} MB/s")
    
#             # with 确保相机资源正确释放
#         with CVSubscriber("./libCamera.so") as subscriber:
                
#                 if subscriber.connect("tcp://192.168.137.1:5556"):
#                     try:
#                 # 接收帧但不显示
#                         frames = subscriber.receive_loop(
#                         callback=count_frames,
#                         timeout_ms=10
#                         )
                
#                 # 性能统计
#                         elapsed = time.time() - start_time
#                         avg_fps = frames / elapsed
#                         avg_mbps = (total_bytes / elapsed) / (1024 * 1024)
                
#                         print(f"\n📈 性能统计:")
#                         print(f"  总帧数: {frames}")
#                         print(f"  总数据: {total_bytes / (1024*1024):.2f} MB")
#                         print(f"  平均FPS: {avg_fps:.2f}")
#                         print(f"  平均带宽: {avg_mbps:.2f} MB/s")
#                         print(f"  运行时间: {elapsed:.2f} 秒")
                
#                     except      KeyboardInterrupt:
#                         print("\n测试被中断")
#             # 不需要 finally 来关闭，with 会自动处理
    

    
#     # 运行示例
#     try:

#         example_high_performance()
#     except Exception as e:
#         print(f"❌ 运行出错: {e}")
#         import traceback
#         traceback.print_exc()