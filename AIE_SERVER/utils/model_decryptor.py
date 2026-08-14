#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模型文件解密工具
在运行时自动解密加密的模型文件
"""

import os
import tempfile
from pathlib import Path
from typing import Optional
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import base64

# 加密后的文件扩展名
ENCRYPTED_EXTENSION = '.encrypted'

# 默认密码（应该从环境变量或配置文件中读取）
DEFAULT_PASSWORD = os.environ.get('AIE_MODEL_PASSWORD', 'aie_default_model_password_2024')

def generate_key_from_password(password: bytes, salt: bytes = None) -> bytes:
    """从密码生成加密密钥"""
    if salt is None:
        salt = b'aie_model_encryption_salt_2024'  # 固定盐值，确保可重复
    
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(password))
    return key

def load_key_from_file(key_file: Path) -> Optional[bytes]:
    """从文件加载密钥"""
    try:
        with open(key_file, 'rb') as f:
            return f.read()
    except Exception:
        return None

def decrypt_model_file(encrypted_path: Path, output_path: Optional[Path] = None, 
                       password: Optional[str] = None, key_file: Optional[Path] = None) -> Optional[Path]:
    """
    解密模型文件
    
    Args:
        encrypted_path: 加密文件路径
        output_path: 输出路径（如果为None，则使用临时文件）
        password: 解密密码
        key_file: 密钥文件路径
    
    Returns:
        解密后的文件路径，如果失败返回None
    """
    encrypted_path = Path(encrypted_path)
    
    if not encrypted_path.exists():
        return None
    
    # 获取密钥
    key = None
    if key_file and Path(key_file).exists():
        key = load_key_from_file(key_file)
    elif password:
        key = generate_key_from_password(password.encode('utf-8'))
    else:
        # 尝试从环境变量或默认密码
        key = generate_key_from_password(DEFAULT_PASSWORD.encode('utf-8'))
    
    if key is None:
        return None
    
    try:
        # 读取加密文件
        with open(encrypted_path, 'rb') as f:
            encrypted_data = f.read()
        
        # 解密
        fernet = Fernet(key)
        decrypted_data = fernet.decrypt(encrypted_data)
        
        # 确定输出路径
        if output_path is None:
            # 使用临时文件
            original_ext = encrypted_path.suffix.replace(ENCRYPTED_EXTENSION, '')
            temp_file = tempfile.NamedTemporaryFile(
                suffix=original_ext,
                delete=False,
                dir=encrypted_path.parent
            )
            output_path = Path(temp_file.name)
            temp_file.close()
        else:
            output_path = Path(output_path)
        
        # 写入解密文件
        with open(output_path, 'wb') as f:
            f.write(decrypted_data)
        
        return output_path
    except Exception as e:
        print(f"解密模型文件失败: {e}")
        return None

def get_decrypted_model_path(encrypted_path: Path, cache_dir: Optional[Path] = None,
                             password: Optional[str] = None, key_file: Optional[Path] = None) -> Optional[Path]:
    """
    获取解密后的模型文件路径（带缓存）
    
    Args:
        encrypted_path: 加密文件路径
        cache_dir: 缓存目录（如果为None，则使用临时目录）
        password: 解密密码
        key_file: 密钥文件路径
    
    Returns:
        解密后的文件路径，如果失败返回None
    """
    encrypted_path = Path(encrypted_path)
    
    # 如果文件未加密，直接返回
    if not encrypted_path.suffix.endswith(ENCRYPTED_EXTENSION):
        if encrypted_path.exists():
            return encrypted_path
        return None
    
    # 确定缓存目录
    if cache_dir is None:
        cache_dir = Path(tempfile.gettempdir()) / 'aie_models'
    else:
        cache_dir = Path(cache_dir)
    
    cache_dir.mkdir(parents=True, exist_ok=True)
    
    # 生成缓存文件名（基于原始文件名）
    original_name = encrypted_path.stem.replace(ENCRYPTED_EXTENSION, '')
    original_ext = encrypted_path.suffix.replace(ENCRYPTED_EXTENSION, '')
    cached_path = cache_dir / f"{original_name}{original_ext}"
    
    # 如果缓存文件已存在且较新，直接返回
    if cached_path.exists():
        if cached_path.stat().st_mtime >= encrypted_path.stat().st_mtime:
            return cached_path
    
    # 解密到缓存
    decrypted_path = decrypt_model_file(encrypted_path, cached_path, password, key_file)
    return decrypted_path

def find_and_decrypt_model(model_name: str, search_dirs: list, 
                          password: Optional[str] = None, key_file: Optional[Path] = None) -> Optional[Path]:
    """
    查找并解密模型文件
    
    Args:
        model_name: 模型文件名（可以带或不带扩展名）
        search_dirs: 搜索目录列表
        password: 解密密码
        key_file: 密钥文件路径
    
    Returns:
        解密后的文件路径，如果失败返回None
    """
    # 尝试不同的扩展名
    extensions = ['', '.encrypted', '.onnx', '.engine', '.plan']
    
    for search_dir in search_dirs:
        search_dir = Path(search_dir)
        if not search_dir.exists():
            continue
        
        for ext in extensions:
            if ext == '.encrypted':
                # 查找加密文件
                encrypted_path = search_dir / f"{model_name}{ext}"
                if encrypted_path.exists():
                    return get_decrypted_model_path(encrypted_path, password=password, key_file=key_file)
            else:
                # 查找普通文件
                if ext:
                    file_path = search_dir / f"{model_name}{ext}"
                else:
                    file_path = search_dir / model_name
                
                if file_path.exists():
                    return file_path
    
    return None
