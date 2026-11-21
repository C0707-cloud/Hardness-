# -*- coding: utf-8 -*-
"""T1 输入包生成与确定性打包。

职责：
1. 幂等生成 input/ 下的 6 个文件（编码/换行受控，字节精确）。
2. 校验 ground_truth.json 的冻结 paths 与实际输入包一致（不一致直接报错，防止期望脱节）。
3. 确定性打包 input.zip（ZIP_STORED、固定时间戳、ZIP 根不含 input/ 前缀）。
4. 生成 input_manifest.json（content_hash / zip_sha256 / 逐文件 sha256 / ground_truth_sha256）。

哈希口径统一取自 inputpkg.py，与 checker.py 同源。
"""
import json
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import inputpkg

CASE_DIR = os.path.dirname(HERE)              # cases/T1
INPUT_DIR = os.path.join(CASE_DIR, "input")
ZIP_PATH = os.path.join(CASE_DIR, "input.zip")
MANIFEST_PATH = os.path.join(HERE, "input_manifest.json")
GT_PATH = os.path.join(HERE, "ground_truth.json")

ZIP_DATE = (1980, 1, 1, 0, 0, 0)              # 固定时间戳保证 zip 字节可复现

# 输入包定义：相对路径 -> (编码, 换行, 内容)。内容统一用 \n，编码时按换行模式替换。
FILES = {
    "config.json": ("utf-8", "lf", (
        "{\n"
        "  \"app_name\": \"盘点演示系统\",\n"
        "  \"version\": \"1.4.2\",\n"
        "  \"owner\": \"李四\",\n"
        "  \"enabled\": true\n"
        "}\n"
    )),
    "报表.txt": ("gbk", "crlf", (
        "季度经营报表\n"
        "\n"
        "总金额: 1284500.00\n"
    )),
    "app.log": ("utf-8", "lf", (
        "2024-01-01 10:00:00 INFO  service started\n"
        "2024-01-01 10:01:00 INFO  loading config\n"
        "2024-01-01 10:02:00 WARN  config missing optional field\n"
        "2024-01-01 10:03:00 INFO  processing batch 1\n"
        "2024-01-01 10:03:30 INFO  batch 1 done\n"
        "2024-01-01 10:04:00 INFO  status: OK\n"
    )),
    "notes/readme.md": ("utf-8", "lf", (
        "# 项目说明\n"
        "\n"
        "本目录为演示资料包。\n"
        "\n"
        "负责人: 张三\n"
    )),
    "data/2024.csv": ("utf-8", "crlf", (
        "date,region,amount\n"
        "2024-01-05,华东,1200.50\n"
        "2024-01-12,华北,980.00\n"
        "2024-01-19,华南,1500.75\n"
        "2024-01-26,华东,760.25\n"
        "2024-02-02,华北,2100.00\n"
        "2024-02-09,华南,890.50\n"
        "2024-02-16,华东,1350.00\n"
        "2024-02-23,华北,670.75\n"
        "2024-03-01,华南,1880.25\n"
        "2024-03-08,华东,940.00\n"
        "2024-03-15,华北,1720.50\n"
        "2024-03-22,华南,1050.00\n"
    )),
    "data/empty.txt": ("utf-8", "lf", ""),
}


def _encode(encoding, newline, content):
    if newline == "crlf":
        content = content.replace("\n", "\r\n")
    return content.encode(encoding)


def make_input_files():
    for rel, (enc, nl, content) in FILES.items():
        full = os.path.join(INPUT_DIR, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(_encode(enc, nl, content))


def build_zip(root, zip_path):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
        for rel, data in inputpkg.iter_files(root):
            zi = zipfile.ZipInfo(rel)
            zi.date_time = ZIP_DATE
            zi.compress_type = zipfile.ZIP_STORED
            zi.external_attr = 0o644 << 16
            zi.create_system = 3
            zf.writestr(zi, data)


def load_ground_truth():
    with open(GT_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def verify_frozen_paths(gt, actual_paths):
    """冻结 paths 必须与实际输入包完全一致，否则期望会与输入脱节。"""
    frozen = set(gt.get("paths", []))
    missing = sorted(frozen - actual_paths)
    extra = sorted(actual_paths - frozen)
    if missing or extra:
        print("错误：ground_truth.paths 与实际输入包不一致")
        if missing:
            print("  冻结清单有、输入包无:", missing)
        if extra:
            print("  输入包有、冻结清单无:", extra)
        return False
    return True


def main():
    os.makedirs(INPUT_DIR, exist_ok=True)
    make_input_files()
    build_zip(INPUT_DIR, ZIP_PATH)

    files = inputpkg.iter_files(INPUT_DIR)
    actual_paths = {rel for rel, _ in files}

    gt = load_ground_truth()
    if not verify_frozen_paths(gt, actual_paths):
        return 1

    manifest = {
        "task": "T1",
        "content_hash": inputpkg.content_hash(INPUT_DIR),
        "zip_sha256": inputpkg.sha256_file(ZIP_PATH),
        "ground_truth_sha256": inputpkg.sha256_file(GT_PATH),
        "frozen_paths": sorted(actual_paths),
        "files": [
            {"path": rel, "sha256": inputpkg.sha256_bytes(data), "size": len(data)}
            for rel, data in files
        ],
    }
    with open(MANIFEST_PATH, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)

    print("input 文件数:", len(manifest["files"]))
    for f in manifest["files"]:
        print("  -", f["path"], f["size"], "bytes")
    print("content_hash       :", manifest["content_hash"])
    print("zip_sha256         :", manifest["zip_sha256"])
    print("ground_truth_sha256:", manifest["ground_truth_sha256"])
    print("manifest           :", MANIFEST_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
