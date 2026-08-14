#!/usr/bin/env python3
"""
授权文件生成工具
用于为客户设备生成带数字签名的授权文件
"""

import argparse
import base64
import datetime as dt
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, Any, Optional, List

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


def generate_rsa_keypair(bits: int = 3072) -> tuple:
    """生成RSA密钥对"""
    print(f"生成 {bits} 位 RSA 密钥对...")
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=bits,
    )
    public_key = private_key.public_key()
    return private_key, public_key


def save_keypair(private_key, public_key, output_dir: Path) -> None:
    """保存密钥对到文件"""
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # 保存私钥（PEM格式，无密码保护）
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )
    private_path = output_dir / "private_key.pem"
    private_path.write_bytes(private_pem)
    print(f"✓ 私钥已保存: {private_path}")
    
    # 保存公钥（PEM格式）
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    )
    public_path = output_dir / "public_key.pem"
    public_path.write_bytes(public_pem)
    print(f"✓ 公钥已保存: {public_path}")
    
    # 显示公钥内容（用于嵌入到 license_check.py）
    print("\n" + "="*60)
    print("请将以下公钥内容复制到 license_check.py 的 PUBLIC_KEY_PEM 变量中:")
    print("="*60)
    print(public_pem.decode('utf-8'))
    print("="*60)


def read_text_file(path: Path) -> Optional[str]:
    """读取文本文件"""
    try:
        return path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, PermissionError, OSError):
        return None


def read_sys_file(path: str) -> Optional[str]:
    """读取系统文件"""
    return read_text_file(Path(path))


def get_mac_address() -> Optional[str]:
    """获取MAC地址"""
    candidates = ["eth0", "wlan0", "usb0"]
    for name in candidates:
        mac_path = Path(f"/sys/class/net/{name}/address")
        mac = read_text_file(mac_path)
        if mac:
            return mac
    return None


def collect_hardware_ids() -> List[str]:
    """采集硬件标识"""
    emmc_cid = read_sys_file("/sys/block/mmcblk0/device/cid")
    devtree_sn = read_sys_file("/proc/device-tree/serial-number")
    mac = get_mac_address()
    return [val for val in (emmc_cid, devtree_sn, mac) if val]


def calculate_hardware_hash() -> str:
    """计算当前设备的硬件哈希"""
    ids = collect_hardware_ids()
    if not ids:
        raise RuntimeError("无法采集硬件指纹")
    
    print("\n当前设备硬件信息:")
    for i, hw_id in enumerate(ids, 1):
        print(f"  {i}. {hw_id[:50]}{'...' if len(hw_id) > 50 else ''}")
    
    digest = hashlib.sha256("|".join(ids).encode("utf-8")).digest()
    hw_hash = base64.b64encode(digest).decode("ascii")
    print(f"\n硬件哈希: {hw_hash}")
    return hw_hash


def canonical_json(payload: Dict[str, Any]) -> bytes:
    """规范化JSON（排除签名字段）"""
    data = {k: payload[k] for k in sorted(payload.keys()) if k != "sig"}
    return json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")


def sign_license(license_data: Dict[str, Any], private_key) -> str:
    """使用私钥签名授权文件"""
    message = canonical_json(license_data)
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH
        ),
        hashes.SHA256()
    )
    return base64.b64encode(signature).decode("ascii")


def load_private_key(key_path: Path):
    """加载私钥"""
    if not key_path.exists():
        raise FileNotFoundError(f"私钥文件不存在: {key_path}")
    
    pem_data = key_path.read_bytes()
    return serialization.load_pem_private_key(pem_data, password=None)


def generate_license(
    hw_hash: str,
    expiry_date: str,
    private_key_path: Path,
    output_path: Path,
    customer_name: str = "",
    device_id: str = "",
) -> None:
    """生成授权文件"""
    
    print("\n生成授权文件...")
    
    # 加载私钥
    private_key = load_private_key(private_key_path)
    
    # 构建授权数据
    license_data = {
        "hw_hash": hw_hash,
        "expiry": expiry_date,
    }
    
    # 添加可选字段
    if customer_name:
        license_data["customer"] = customer_name
    if device_id:
        license_data["device_id"] = device_id
    
    license_data["issued_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    
    # 签名
    signature = sign_license(license_data, private_key)
    license_data["sig"] = signature
    
    # 保存授权文件
    output_path.parent.mkdir(parents=True, exist_ok=True)
    license_json = json.dumps(license_data, indent=2, ensure_ascii=False)
    output_path.write_text(license_json, encoding="utf-8")
    
    print(f"✓ 授权文件已生成: {output_path}")
    print("\n授权文件内容:")
    print("="*60)
    print(license_json)
    print("="*60)


def interactive_mode():
    """交互式模式 - 简化的授权生成流程"""
    print("="*60)
    print("AIE 授权文件生成工具 - 交互式模式")
    print("="*60)
    print()
    
    # 检查私钥文件
    default_key_path = Path("./keys/private_key.pem")
    if not default_key_path.exists():
        print("❌ 未找到私钥文件: ./keys/private_key.pem")
        print()
        print("请先生成密钥对:")
        print("  python generate_license.py --generate-keys")
        print()
        sys.exit(1)
    
    print("✓ 找到私钥文件: ./keys/private_key.pem")
    print()
    
    # 获取硬件哈希
    print("正在获取当前设备硬件信息...")
    print("-" * 60)
    try:
        hw_hash = calculate_hardware_hash()
    except Exception as e:
        print(f"❌ 无法获取硬件信息: {e}")
        sys.exit(1)
    print()
    
    # 输入到期日期
    print("请输入授权到期日期")
    print("提示: 格式为 YYYY-MM-DD，例如 2025-12-31")
    print("-" * 60)
    
    while True:
        expiry_str = input("到期日期: ").strip()
        if not expiry_str:
            print("❌ 日期不能为空，请重新输入")
            continue
        
        try:
            expiry_datetime = dt.datetime.strptime(expiry_str, "%Y-%m-%d")
            # 检查日期是否在未来
            if expiry_datetime.date() <= dt.datetime.now().date():
                print("⚠️  警告: 日期在今天或更早，授权可能立即过期")
                confirm = input("确认使用此日期? (y/n): ").strip().lower()
                if confirm != 'y':
                    continue
            
            # 转换为ISO格式（当天23:59:59）
            expiry_iso = expiry_datetime.replace(
                hour=23, minute=59, second=59,
                tzinfo=dt.timezone.utc
            ).isoformat()
            break
        except ValueError:
            print("❌ 日期格式错误，请使用 YYYY-MM-DD 格式，例如: 2025-12-31")
    
    print()
    print(f"✓ 到期日期: {expiry_str} 23:59:59 UTC")
    print()
    
    # 可选：客户名称
    print("请输入客户名称（可选，直接回车跳过）")
    print("-" * 60)
    customer_name = input("客户名称: ").strip()
    if customer_name:
        print(f"✓ 客户名称: {customer_name}")
    else:
        print("- 跳过客户名称")
    print()
    
    # 可选：设备编号
    print("请输入设备编号（可选，直接回车跳过）")
    print("-" * 60)
    device_id = input("设备编号: ").strip()
    if device_id:
        print(f"✓ 设备编号: {device_id}")
    else:
        print("- 跳过设备编号")
    print()
    
    # 输出路径
    output_path = Path("license.json")
    if output_path.exists():
        print(f"⚠️  警告: 文件 {output_path} 已存在")
        overwrite = input("是否覆盖? (y/n): ").strip().lower()
        if overwrite != 'y':
            print("已取消操作")
            sys.exit(0)
        print()
    
    # 确认信息
    print("="*60)
    print("授权信息确认")
    print("="*60)
    print(f"硬件哈希: {hw_hash}")
    print(f"到期日期: {expiry_str} 23:59:59 UTC")
    if customer_name:
        print(f"客户名称: {customer_name}")
    if device_id:
        print(f"设备编号: {device_id}")
    print(f"输出文件: {output_path}")
    print("="*60)
    
    confirm = input("确认生成授权文件? (y/n): ").strip().lower()
    if confirm != 'y':
        print("已取消操作")
        sys.exit(0)
    
    print()
    print("正在生成授权文件...")
    print("-" * 60)
    
    try:
        generate_license(
            hw_hash=hw_hash,
            expiry_date=expiry_iso,
            private_key_path=default_key_path,
            output_path=output_path,
            customer_name=customer_name,
            device_id=device_id,
        )
        
        print()
        print("="*60)
        print("✅ 授权文件生成成功!")
        print("="*60)
        print(f"📄 文件路径: {output_path.absolute()}")
        print()
        print("📋 后续步骤:")
        print("  1. 将此授权文件交付给客户")
        print("  2. 客户将文件放置在系统中:")
        print("     - ./license.json (当前目录)")
        print("     - /etc/aie/license.json (系统目录)")
        print("  3. 运行 ./run.sh 启动系统")
        print()
        
    except Exception as e:
        print()
        print(f"❌ 生成失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="AIE授权文件生成工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用方式:

【交互式模式】（推荐）
  直接运行，按提示输入信息:
    python generate_license.py

【命令行模式】
1. 生成密钥对（首次使用）:
   python generate_license.py --generate-keys

2. 获取当前设备硬件哈希:
   python generate_license.py --get-hw-hash

3. 一键生成（为当前设备）:
   python generate_license.py --auto --expiry 2025-12-31

4. 完整参数生成:
   python generate_license.py \\
       --auto \\
       --expiry 2025-12-31 \\
       --customer "客户名称" \\
       --device-id "设备编号"
        """
    )
    
    # 密钥生成选项
    parser.add_argument("--generate-keys", action="store_true", 
                        help="生成RSA密钥对")
    parser.add_argument("--keys-dir", type=Path, default=Path("./keys"),
                        help="密钥保存目录 (默认: ./keys)")
    parser.add_argument("--key-bits", type=int, default=3072,
                        help="RSA密钥位数 (默认: 3072)")
    
    # 硬件信息选项
    parser.add_argument("--get-hw-hash", action="store_true",
                        help="获取当前设备的硬件哈希")
    
    # 授权生成选项
    parser.add_argument("--hw-hash", type=str,
                        help="设备硬件哈希值")
    parser.add_argument("--expiry", type=str,
                        help="授权到期日期 (格式: YYYY-MM-DD)")
    parser.add_argument("--private-key", type=Path,
                        help="私钥文件路径 (默认: ./keys/private_key.pem)")
    parser.add_argument("--output", type=Path, default=Path("license.json"),
                        help="输出授权文件路径 (默认: license.json)")
    parser.add_argument("--customer", type=str, default="",
                        help="客户名称（可选）")
    parser.add_argument("--device-id", type=str, default="",
                        help="设备编号（可选）")
    
    # 便捷选项
    parser.add_argument("--auto", action="store_true",
                        help="自动获取当前设备硬件哈希并生成授权")
    
    # 交互式模式标志
    parser.add_argument("--interactive", "-i", action="store_true",
                        help="交互式模式（默认）")
    
    args = parser.parse_args()
    
    # 如果没有任何参数，进入交互式模式
    if len(sys.argv) == 1:
        try:
            interactive_mode()
            return
        except KeyboardInterrupt:
            print("\n\n已取消操作")
            sys.exit(0)
    
    try:
        # 生成密钥对
        if args.generate_keys:
            print("="*60)
            print("生成RSA密钥对")
            print("="*60)
            private_key, public_key = generate_rsa_keypair(args.key_bits)
            save_keypair(private_key, public_key, args.keys_dir)
            print("\n✓ 密钥对生成完成")
            print("\n⚠️  重要提示:")
            print("  1. 私钥文件务必妥善保管，不要泄露")
            print("  2. 将公钥内容更新到 license_check.py 的 PUBLIC_KEY_PEM")
            print("  3. 更新公钥后需要重新编译项目")
            return
        
        # 获取硬件哈希
        if args.get_hw_hash:
            print("="*60)
            print("获取设备硬件哈希")
            print("="*60)
            hw_hash = calculate_hardware_hash()
            print(f"\n请使用此硬件哈希生成授权文件:")
            print(f"  --hw-hash {hw_hash}")
            return
        
        # 一键生成授权
        if args.auto:
            if not args.expiry:
                print("错误: --auto 模式需要指定 --expiry 参数")
                print("示例: python generate_license.py --auto --expiry 2025-12-31")
                sys.exit(1)
            
            # 使用默认私钥路径（如果未指定）
            private_key_path = args.private_key if args.private_key else Path("./keys/private_key.pem")
            if not private_key_path.exists():
                print(f"错误: 私钥文件不存在: {private_key_path}")
                print()
                print("请先生成密钥对:")
                print("  python generate_license.py --generate-keys")
                sys.exit(1)
            
            print("="*60)
            print("自动生成授权文件")
            print("="*60)
            hw_hash = calculate_hardware_hash()
            
            # 验证到期日期格式
            try:
                expiry_datetime = dt.datetime.strptime(args.expiry, "%Y-%m-%d")
                expiry_iso = expiry_datetime.replace(
                    hour=23, minute=59, second=59, 
                    tzinfo=dt.timezone.utc
                ).isoformat()
            except ValueError:
                print(f"错误: 无效的日期格式 '{args.expiry}'，请使用 YYYY-MM-DD")
                sys.exit(1)
            
            generate_license(
                hw_hash=hw_hash,
                expiry_date=expiry_iso,
                private_key_path=private_key_path,
                output_path=args.output,
                customer_name=args.customer,
                device_id=args.device_id,
            )
            
            print("\n✓ 授权文件生成完成")
            print(f"✓ 可以将 {args.output} 交付给客户")
            return
        
        # 手动生成授权
        if args.hw_hash and args.expiry:
            # 使用默认私钥路径（如果未指定）
            private_key_path = args.private_key if args.private_key else Path("./keys/private_key.pem")
            if not private_key_path.exists():
                print(f"错误: 私钥文件不存在: {private_key_path}")
                print()
                print("请先生成密钥对:")
                print("  python generate_license.py --generate-keys")
                sys.exit(1)
            
            print("="*60)
            print("生成授权文件")
            print("="*60)
            
            # 验证到期日期格式
            try:
                expiry_datetime = dt.datetime.strptime(args.expiry, "%Y-%m-%d")
                expiry_iso = expiry_datetime.replace(
                    hour=23, minute=59, second=59,
                    tzinfo=dt.timezone.utc
                ).isoformat()
            except ValueError:
                print(f"错误: 无效的日期格式 '{args.expiry}'，请使用 YYYY-MM-DD")
                sys.exit(1)
            
            generate_license(
                hw_hash=args.hw_hash,
                expiry_date=expiry_iso,
                private_key_path=private_key_path,
                output_path=args.output,
                customer_name=args.customer,
                device_id=args.device_id,
            )
            
            print("\n✓ 授权文件生成完成")
            print(f"✓ 可以将 {args.output} 交付给客户")
            return
        
        # 未指定操作
        parser.print_help()
        
    except Exception as e:
        print(f"\n错误: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

