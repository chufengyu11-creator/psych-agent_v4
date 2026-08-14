import tensorrt as trt
from pathlib import Path

TRT_LOGGER = trt.Logger(trt.Logger.INFO)

def fixed_shape(shape):
    out = []
    for i, d in enumerate(shape):
        if d > 0:
            out.append(int(d))
        elif i == 0:
            out.append(1)
        elif i == 1:
            out.append(3)
        else:
            out.append(224)
    return tuple(out)

def build_engine(onnx_path, engine_path, fp16=True):
    onnx_path = Path(onnx_path)
    engine_path = Path(engine_path)
    engine_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n========== Building ==========")
    print(f"ONNX  : {onnx_path}")
    print(f"ENGINE: {engine_path}")

    builder = trt.Builder(TRT_LOGGER)
    flags = 1 << int(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH)
    network = builder.create_network(flags)
    parser = trt.OnnxParser(network, TRT_LOGGER)

    data = onnx_path.read_bytes()
    if not parser.parse(data):
        print("ONNX parse failed:")
        for i in range(parser.num_errors):
            print(parser.get_error(i))
        raise SystemExit(1)

    config = builder.create_builder_config()
    config.set_memory_pool_limit(trt.MemoryPoolType.WORKSPACE, 4 << 30)

    if fp16 and builder.platform_has_fast_fp16:
        config.set_flag(trt.BuilderFlag.FP16)
        print("FP16 enabled")

    profile = builder.create_optimization_profile()
    has_dynamic = False

    for i in range(network.num_inputs):
        inp = network.get_input(i)
        shape = tuple(inp.shape)
        print(f"Input {i}: name={inp.name}, shape={shape}, dtype={inp.dtype}")

        if any(d < 0 for d in shape):
            has_dynamic = True
            opt_shape = fixed_shape(shape)
            profile.set_shape(inp.name, opt_shape, opt_shape, opt_shape)
            print(f"  dynamic input -> min/opt/max = {opt_shape}")

    if has_dynamic:
        config.add_optimization_profile(profile)

    serialized = builder.build_serialized_network(network, config)
    if serialized is None:
        raise RuntimeError(f"Failed to build engine: {onnx_path}")

    engine_path.write_bytes(serialized)
    print(f"Saved: {engine_path} ({engine_path.stat().st_size / 1024 / 1024:.2f} MB)")

if __name__ == "__main__":
    jobs = [
        ("trash/onnx_version/swin_gaze_nx.onnx", "convert/gaze.engine"),
        ("trash/onnx_version/bp4d_au_nx.onnx", "convert/bp4d.engine"),
        ("trash/onnx_version/disfa_au_nx.onnx", "convert/disfa.engine"),
    ]

    for onnx_path, engine_path in jobs:
        build_engine(onnx_path, engine_path, fp16=True)
