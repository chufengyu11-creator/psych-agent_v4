#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run the AU + emotion pipeline only."""

import argparse
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from api.apiEmotion import apiEmo
from core.logger_config import get_logger

logger = get_logger(__name__)


def format_au(au_units: dict) -> str:
    if not au_units:
        return "none"
    active = {key: value for key, value in au_units.items() if value > 0.1}
    if not active:
        return "none active"
    return ", ".join(f"{key}={value:.2f}" for key, value in sorted(active.items()))


def main(mqtt_enable: bool = True, poll_interval: float = 0.1) -> None:
    api = apiEmo(
        camera_id=0,
        device=os.environ.get("AIE_DEVICE", "cuda"),
        mqtt_broker=os.environ.get("MQTT_BROKER", "192.168.8.110"),
        mqtt_port=int(os.environ.get("MQTT_PORT", "1883")),
        mqtt_enable=mqtt_enable,
        gaze_only=False,
        au_only=True,
        heart_rate_only=False,
        server_address=os.environ.get("AIE_CAMERA_ZMQ", "tcp://localhost:5556"),
    )

    def on_exit(_sig, _frame) -> None:
        logger.info("退出中...")
        api.cleanup()
        sys.exit(0)

    signal.signal(signal.SIGINT, on_exit)
    signal.signal(signal.SIGTERM, on_exit)

    if not api.init():
        logger.error("初始化失败")
        return

    if not api.start_processing_threads({"face_detection", "au"}):
        logger.error("AU/情绪处理线程启动失败")
        api.cleanup()
        return

    logger.info("AU/情绪检测已启动。MQTT=%s", mqtt_enable)
    if mqtt_enable:
        logger.info("MQTT 数据发布到: emotion/au, emotion/emotion, emotion/face_status")

    try:
        while True:
            au = api.getEmoAU()
            if au:
                logger.info(
                    "[AU] seq=%s active=%s emotion=%s(%.2f)",
                    au.get("image_sequence"),
                    format_au(au.get("au_units", {})),
                    au.get("emotion_label", ""),
                    au.get("emotion_confidence", 0.0),
                )
            time.sleep(poll_interval)
    finally:
        api.cleanup()
        logger.info("已退出")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AU + emotion pipeline")
    parser.add_argument("--no-mqtt", action="store_true", help="disable MQTT publishing")
    parser.add_argument("--interval", type=float, default=0.1, help="poll interval seconds")
    args = parser.parse_args()
    main(mqtt_enable=not args.no_mqtt, poll_interval=args.interval)
