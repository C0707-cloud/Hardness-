# -*- coding: utf-8 -*-
"""输入包读取与指纹的唯一实现。

build_input.py 与 checker.py 都从这里取哈希口径，避免两处各算一套导致漂移。
"""
import hashlib
import os


def iter_files(root):
    """返回 [(相对路径, 字节)]，按相对路径字典序排序（与文件系统遍历顺序无关）。"""
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace("\\", "/")
            with open(full, "rb") as fh:
                out.append((rel, fh.read()))
    out.sort(key=lambda x: x[0])
    return out


def iter_input_paths(root):
    """返回输入包内相对路径集合。"""
    return {rel for rel, _data in iter_files(root)}


def content_hash(root):
    """SHA-256(排序后「相对路径 + \\x00 + 文件字节」拼接)。

    路径参与哈希，改名/移动/增删均可见；空文件以「路径 + \\x00 + 空」参与，
    因此空文件存不存在也可检测。排序保证与遍历顺序无关。
    """
    blob = b"".join(
        rel.encode("utf-8") + b"\x00" + data for rel, data in iter_files(root)
    )
    return hashlib.sha256(blob).hexdigest()


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    with open(path, "rb") as fh:
        return sha256_bytes(fh.read())
