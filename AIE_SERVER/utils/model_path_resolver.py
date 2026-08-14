#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模型路径解析器
自动处理加密模型文件的解密
"""

import os
from pathlib import Path
from typing import Optional

try:
    from .model_decryptor import get_decrypted_model_path, find_and_decrypt_model
except ImportError:
    # 如果相对导入失败，尝试绝对导入
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from utils.model_decryptor import get_decrypted_model_path, find_and_decrypt_model

def resolve_model_path(model_path: str, password: Optional[str] = None, 
                      key_file: Optional[str] = None) -> Optional[str]:
    """
    解析模型路径，如果是加密文件则自动解密
    
    Args:
        model_path: 模型文件路径（可以是加密或未加密的）
        password: 解密密码（如果为None，从环境变量读取）
        key_file: 密钥文件路径
    
    Returns:
        解密后的文件路径（如果是加密文件）或原始路径（如果未加密）
    """
    model_path = Path(model_path)
    
    # 如果文件不存在，尝试查找加密版本
    if not model_path.exists():
        # 尝试查找 .encrypted 版本
        encrypted_path = model_path.with_suffix(model_path.suffix + '.encrypted')
        if encrypted_path.exists():
            model_path = encrypted_path
    
    # 如果是加密文件，进行解密
    if model_path.suffix.endswith('.encrypted'):
        # 获取密码
        if password is None:
            password = os.environ.get('AIE_MODEL_PASSWORD')
        
        # 获取密钥文件路径
        key_path = None
        if key_file:
            key_path = Path(key_file)
        else:
            # 尝试在模型文件同目录查找密钥文件
            key_path = model_path.parent / '.model_key.key'
            if not key_path.exists():
                key_path = None
        
        # 解密文件
        decrypted_path = get_decrypted_model_path(
            model_path,
            password=password,
            key_file=key_path
        )
        
        if decrypted_path:
            return str(decrypted_path)
        else:
            raise ValueError(f"无法解密模型文件: {model_path}")
    
    # 如果文件存在且未加密，直接返回
    if model_path.exists():
        return str(model_path)
    
    # 文件不存在
    return None

def find_model_file(model_name: str, search_dirs: list, 
                   password: Optional[str] = None, key_file: Optional[str] = None) -> Optional[str]:
    """
    查找模型文件（自动处理加密）
    
    Args:
        model_name: 模型文件名（可以带或不带扩展名）
        search_dirs: 搜索目录列表
        password: 解密密码
        key_file: 密钥文件路径
    
    Returns:
        模型文件路径（已解密），如果失败返回None
    """
    # 获取密码
    if password is None:
        password = os.environ.get('AIE_MODEL_PASSWORD')
    
    # 使用解密工具查找
    result = find_and_decrypt_model(
        model_name,
        search_dirs,
        password=password,
        key_file=Path(key_file) if key_file else None
    )
    
    return str(result) if result else None
