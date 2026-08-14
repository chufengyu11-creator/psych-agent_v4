from __future__ import annotations

import sys
from pathlib import Path

import tensorrt as trt


TRT_LOGGER = trt.Logger(trt.Logger.INFO)

PROJECT_ROOT = Path("/data/agent/psych-agent/psych-agent_v4_test/AIE_SERVER")
ONNX_ROOT = PROJECT_ROOT / "trash" / "onnx_version"
ENGINE_ROOT = PROJECT_ROOT / "convert"


def fixed_shape(shape: tuple[int, ...]) -> tuple[int, ...]:
    """
    将 ONNX 中的动态输入维度固定下来。

    当前默认模型输入为：
        batch = 1
        channel = 3
        height = 224
        width = 224

    静态维度保持 ONNX 原始值不变。
    """
    if len(shape) != 4:
        raise ValueError(
            f"当前脚本仅自动处理 NCHW 四维图像输入，"
            f"但检测到输入 shape={shape}"
        )

    result = []

    for index, dim in enumerate(shape):
        if dim > 0:
            result.append(int(dim))
        elif index == 0:
            result.append(1)
        elif index == 1:
            result.append(3)
        elif index in (2, 3):
            result.append(224)
        else:
            raise ValueError(f"无法处理动态维度：shape={shape}")

    return tuple(result)


def verify_engine(engine_path: Path) -> None:
    """
    在当前 TensorRT 环境中反序列化 engine，
    确认 engine 文件至少能够正常加载。
    """
    runtime = trt.Runtime(TRT_LOGGER)
    serialized_engine = engine_path.read_bytes()

    engine = runtime.deserialize_cuda_engine(serialized_engine)

    if engine is None:
        raise RuntimeError(
            f"Engine 已写入，但在当前环境反序列化失败：{engine_path}"
        )

    print(f"[OK] Engine deserialize succeeded: {engine_path}")


def build_engine(
    onnx_path: Path,
    engine_path: Path,
    fp16: bool = True,
) -> None:
    onnx_path = onnx_path.resolve()
    engine_path = engine_path.resolve()

    if not onnx_path.is_file():
        raise FileNotFoundError(f"找不到 ONNX 文件：{onnx_path}")

    engine_path.parent.mkdir(parents=True, exist_ok=True)

    # 先生成临时文件，构建和校验成功后再替换正式文件
    temporary_engine_path = engine_path.with_suffix(
        engine_path.suffix + ".tmp"
    )

    if temporary_engine_path.exists():
        temporary_engine_path.unlink()

    print("\n" + "=" * 70)
    print("Building TensorRT engine")
    print(f"TensorRT : {trt.__version__}")
    print(f"ONNX     : {onnx_path}")
    print(f"ENGINE   : {engine_path}")
    print("=" * 70)

    trt.init_libnvinfer_plugins(TRT_LOGGER, "")

    builder = trt.Builder(TRT_LOGGER)

    if builder is None:
        raise RuntimeError("无法创建 TensorRT Builder")

    # TensorRT 10+ 的网络默认就是 explicit batch，且 TensorRT 11 已移除
    # EXPLICIT_BATCH 枚举；旧版本仍需显式传入该标志。
    explicit_batch = getattr(
        trt.NetworkDefinitionCreationFlag,
        "EXPLICIT_BATCH",
        None,
    )
    network_flags = 0 if explicit_batch is None else 1 << int(explicit_batch)

    if explicit_batch is None:
        print("[INFO] TensorRT uses explicit batch by default")

    network = builder.create_network(network_flags)

    if network is None:
        raise RuntimeError("无法创建 TensorRT Network")

    parser = trt.OnnxParser(network, TRT_LOGGER)

    if parser is None:
        raise RuntimeError("无法创建 TensorRT ONNX Parser")

    onnx_data = onnx_path.read_bytes()

    if not parser.parse(onnx_data):
        print("\n[ERROR] ONNX parse failed:")

        for error_index in range(parser.num_errors):
            print(f"  [{error_index}] {parser.get_error(error_index)}")

        raise RuntimeError(f"ONNX 解析失败：{onnx_path}")

    print(
        f"[OK] ONNX parsed: "
        f"inputs={network.num_inputs}, outputs={network.num_outputs}"
    )

    config = builder.create_builder_config()

    if config is None:
        raise RuntimeError("无法创建 TensorRT BuilderConfig")

    # 4 GiB workspace
    config.set_memory_pool_limit(
        trt.MemoryPoolType.WORKSPACE,
        4 << 30,
    )

    if fp16:
        # TensorRT 11 移除了 FP16 BuilderFlag 和 platform_has_fast_fp16；
        # 此时精度由 strongly typed ONNX 网络与 TensorRT 自动选择。
        fp16_flag = getattr(trt.BuilderFlag, "FP16", None)
        platform_has_fast_fp16 = getattr(
            builder,
            "platform_has_fast_fp16",
            None,
        )

        if fp16_flag is None:
            print(
                "[INFO] TensorRT selects precision from the ONNX network "
                "(legacy FP16 flag unavailable)"
            )
        elif platform_has_fast_fp16 is False:
            print(
                "[WARN] 当前 GPU/环境未报告 fast FP16，"
                "将使用默认精度构建"
            )
        else:
            config.set_flag(fp16_flag)
            print("[OK] FP16 enabled")

    profile = builder.create_optimization_profile()

    if profile is None:
        raise RuntimeError("无法创建 TensorRT optimization profile")

    has_dynamic_input = False

    for input_index in range(network.num_inputs):
        input_tensor = network.get_input(input_index)

        input_name = input_tensor.name
        input_shape = tuple(input_tensor.shape)
        input_dtype = input_tensor.dtype

        print(
            f"Input {input_index}: "
            f"name={input_name}, "
            f"shape={input_shape}, "
            f"dtype={input_dtype}"
        )

        if any(dim < 0 for dim in input_shape):
            has_dynamic_input = True
            opt_shape = fixed_shape(input_shape)

            success = profile.set_shape(
                input_name,
                opt_shape,  # min
                opt_shape,  # opt
                opt_shape,  # max
            )

            if not success:
                raise RuntimeError(
                    f"设置动态输入 shape 失败："
                    f"name={input_name}, shape={opt_shape}"
                )

            print(
                f"  Dynamic input profile: "
                f"min={opt_shape}, "
                f"opt={opt_shape}, "
                f"max={opt_shape}"
            )

    if has_dynamic_input:
        profile_index = config.add_optimization_profile(profile)

        if profile_index < 0:
            raise RuntimeError("添加 optimization profile 失败")

        print(f"[OK] Optimization profile added: {profile_index}")
    else:
        print("[INFO] ONNX 输入为静态 shape，无需 optimization profile")

    print("[INFO] Starting TensorRT engine build...")

    serialized_engine = builder.build_serialized_network(
        network,
        config,
    )

    if serialized_engine is None:
        raise RuntimeError(
            f"TensorRT engine 构建失败：{onnx_path}"
        )

    temporary_engine_path.write_bytes(serialized_engine)

    size_mb = temporary_engine_path.stat().st_size / 1024 / 1024
    print(
        f"[OK] Temporary engine saved: "
        f"{temporary_engine_path} ({size_mb:.2f} MB)"
    )

    # 在当前 4090 TensorRT 环境中验证
    verify_engine(temporary_engine_path)

    # 验证成功后覆盖正式文件
    temporary_engine_path.replace(engine_path)

    print(
        f"[SUCCESS] Engine generated: "
        f"{engine_path} ({engine_path.stat().st_size / 1024 / 1024:.2f} MB)"
    )


def main() -> int:
    jobs = [
        (
            ONNX_ROOT / "swin_gaze_nx.onnx",
            ENGINE_ROOT / "gaze.engine",
        ),
        (
            ONNX_ROOT / "bp4d_au_nx.onnx",
            ENGINE_ROOT / "bp4d.engine",
        ),
        (
            ONNX_ROOT / "disfa_au_nx.onnx",
            ENGINE_ROOT / "disfa.engine",
        ),
    ]

    print(f"TensorRT version: {trt.__version__}")
    print(f"Project root    : {PROJECT_ROOT}")

    failed_jobs = []

    for onnx_path, engine_path in jobs:
        try:
            build_engine(
                onnx_path=onnx_path,
                engine_path=engine_path,
                fp16=True,
            )
        except Exception as exc:
            print("\n" + "!" * 70)
            print(f"[FAILED] {onnx_path.name}")
            print(f"Reason: {exc}")
            print("!" * 70)

            failed_jobs.append((onnx_path, str(exc)))

    print("\n" + "=" * 70)

    if failed_jobs:
        print(
            f"Completed with failures: "
            f"{len(failed_jobs)}/{len(jobs)}"
        )

        for onnx_path, reason in failed_jobs:
            print(f"- {onnx_path.name}: {reason}")

        return 1

    print(f"All {len(jobs)} TensorRT engines built successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
