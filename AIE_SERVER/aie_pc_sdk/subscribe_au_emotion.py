#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""订阅 emotion/au 和 emotion/emotion，打印AU值和情绪识别结果。"""

import json
import sys
import time

import paho.mqtt.client as mqtt

BROKER = "192.168.8.149"
PORT = 1883
TOPIC_AU = "emotion/au"
TOPIC_EMOTION = "emotion/emotion"


def format_au(au_units: dict) -> str:
    """格式化AU数据为简洁字符串 - 显示所有AU"""
    if not au_units:
        return "无"
    # 显示所有AU值（按AU编号排序）
    return ", ".join([f"{k}={v:.2f}" for k, v in sorted(au_units.items())])


def on_connect(client, userdata, flags, rc):
    """连接回调"""
    if rc == 0:
        print(f"[MQTT] 连接成功: broker={BROKER}:{PORT}")
        client.subscribe(TOPIC_AU, qos=0)
        client.subscribe(TOPIC_EMOTION, qos=0)
        print(f"[MQTT] 已订阅主题: {TOPIC_AU} 和 {TOPIC_EMOTION}\n")
    else:
        print(f"[MQTT] 连接失败，错误码: {rc}")


def on_message(client, userdata, msg):
    try:
        d = json.loads(msg.payload.decode("utf-8"))
        topic = msg.topic
        seq = d.get('image_sequence', '')
        
        if topic == TOPIC_AU:
            au_units = d.get('au_units', {})
            emotion_label = d.get('emotion_label', '')
            emotion_conf = d.get('emotion_confidence', 0.0)
            print(f"[AU] seq={seq}  AU: {format_au(au_units)} | 情绪={emotion_label}({emotion_conf:.2f})")
        
        elif topic == TOPIC_EMOTION:
            emotion_label = d.get('emotion_label', '')
            emotion_conf = d.get('emotion_confidence', 0.0)
            print(f"[情绪] seq={seq}  标签={emotion_label}  置信度={emotion_conf:.2f}")
    
    except Exception as e:
        print("parse err:", e)


def main():
    broker = sys.argv[1] if len(sys.argv) > 1 else BROKER
    # 使用 callback_api_version 修复 DeprecationWarning
    c = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION1)
    c.on_connect = on_connect
    c.on_message = on_message
    
    try:
        print(f"[MQTT] 正在连接到 {broker}:{PORT}...")
        c.connect(broker, PORT, 60)
        print(f"等待连接... (按 Ctrl+C 退出)\n")
        c.loop_forever()
    except KeyboardInterrupt:
        print("\n退出中...")
        c.disconnect()
    except Exception as e:
        print(f"连接错误: {e}")


if __name__ == "__main__":
    main()
