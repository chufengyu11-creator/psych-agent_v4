# PC调用

接收边缘设备发送的情绪分析数据的PC端SDK。

## 功能

- ✅ 订阅并接收视线数据 (Gaze)
- ✅ 订阅并接收心率数据 (HeartRate)
- ✅ 订阅并接收面部动作单元数据 (AU)
- ✅ 支持回调函数和轮询两种数据获取方式

## 安装依赖

```bash
pip install paho-mqtt opencv-python numpy
```

## 开始

### 方式 1: 使用回调函数（推荐）

```python
from pc_sdk import EmotionMQTTClient, GazeData, ImageData

# 创建客户端
client = EmotionMQTTClient(
    broker="192.168.8.99",  # 边缘设备 IP
    port=1883,
    base_topic="emotion"
)

# 设置回调函数
def on_gaze(gaze_data: GazeData):
    print(f"Pitch: {gaze_data.pitch}°, Yaw: {gaze_data.yaw}°")

def on_image(image_data: ImageData):
    cv2.imshow('Image', image_data.image)
    cv2.waitKey(1)

client.set_on_gaze_callback(on_gaze)
client.set_on_image_callback(on_image)

# 连接并开始接收数据
client.connect()

# 保持运行
import time
while True:
    time.sleep(1)
```

### 方式 2: 使用轮询方式

```python
from pc_sdk import EmotionMQTTClient

# 创建并连接客户端
client = EmotionMQTTClient(broker="192.168.8.99", port=1883)
client.connect()

# 轮询获取最新数据
while True:
    gaze = client.get_latest_gaze()
    if gaze:
        print(f"Pitch: {gaze.pitch}°, Yaw: {gaze.yaw}°")
    
    image = client.get_latest_image()
    if image:
        cv2.imshow('Image', image.image)
        cv2.waitKey(1)
    
    time.sleep(0.1)
```

## API 文档

### EmotionMQTTClient

#### 初始化

```python
client = EmotionMQTTClient(
    broker: str = "192.168.8.99",  # MQTT Broker 地址
    port: int = 1883,               # MQTT 端口
    client_id: str = None,          # 客户端 ID（可选）
    base_topic: str = "emotion"     # 基础主题路径
)
```

#### 连接方法

- `connect(timeout: float = 5.0) -> bool`: 连接到 MQTT Broker
- `disconnect()`: 断开连接
- `is_connected() -> bool`: 检查连接状态
- `wait_for_connection(timeout: float = 5.0) -> bool`: 等待连接建立

#### 回调函数设置

- `set_on_gaze_callback(callback: Callable[[GazeData], None])`: 设置视线数据回调
- `set_on_heartrate_callback(callback: Callable[[HeartRateData], None])`: 设置心率数据回调
- `set_on_au_callback(callback: Callable[[AUData], None])`: 设置 AU 数据回调
- `set_on_image_callback(callback: Callable[[ImageData], None])`: 设置图像数据回调
- `set_on_connect_callback(callback: Callable[[], None])`: 设置连接成功回调
- `set_on_disconnect_callback(callback: Callable[[], None])`: 设置断开连接回调

#### 数据获取方法（轮询方式）

- `get_latest_gaze() -> Optional[GazeData]`: 获取最新视线数据
- `get_latest_heartrate() -> Optional[HeartRateData]`: 获取最新心率数据
- `get_latest_au() -> Optional[AUData]`: 获取最新 AU 数据
- `get_latest_image() -> Optional[ImageData]`: 获取最新图像数据

### 数据类型

#### GazeData

```python
@dataclass
class GazeData:
    pitch: float          # 俯仰角（度）
    yaw: float            # 偏航角（度）
    focusing: int         # 专注度标志 (0/1)
    timestamp: Any        # 时间戳
    image_sequence: int   # 图像序列号
```

#### HeartRateData

```python
@dataclass
class HeartRateData:
    heart_rate: float     # 心率值 (bpm)
    hrv: float            # 心率变异性
    timestamp: Any        # 时间戳
    image_sequence: int   # 图像序列号
```

#### AUData

```python
@dataclass
class AUData:
    au_units: Dict[str, float]  # AU 单元字典，如 {"AU1": 0.12, "AU2": 0.09, ...}
    timestamp: Any               # 时间戳
    image_sequence: int          # 图像序列号
    inference_time: float        # 推理时间（毫秒）
```

#### ImageData

```python
@dataclass
class ImageData:
    image: np.ndarray      # OpenCV 图像数组 (BGR 格式)
    image_sequence: int    # 图像序列号
    timestamp: Any         # 时间戳
    width: int             # 图像宽度
    height: int            # 图像高度
    format: str            # 图像格式 ('jpeg')
```

## MQTT 主题

SDK 会自动订阅以下主题：

- `emotion/gaze` - 视线数据
- `emotion/heartrate` - 心率数据
- `emotion/au` - AU 数据
- `emotion/image` - 图像数据

## 完整示例
查看 `example.py` 文件获取完整的使用示例。
