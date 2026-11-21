# -*- coding: utf-8 -*-
"""T1 外部检查器。

判定只依赖输入包路径清单 + 冻结的 ground_truth；产品名/模型名仅透传，不参与判定。
两种形态同一解析器、同一断言：
  - 本地输出：--output-dir
  - 云端响应：--response

判定三层，全部机械可查：
  schema    ：表头列集合、行列数、值域、重复、歧义      → 门禁
  inventory ：path 集合与输入包逐一比对                 → 门禁
  extract   ：5 个键存在且按类型精确匹配                → 门禁

overall_pass = 解析成功 ∧ schema 通过 ∧ inventory 通过 ∧ extract 通过 ∧ 无部署泄漏。
"""
import argparse
import csv
import io
import json
import os
import re
import sys
from decimal import Decimal, InvalidOperation

# 保证 verdict 输出为 UTF-8（Windows 控制台/重定向默认 GBK，会污染 JSON 中文）
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError, OSError):
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import inputpkg  # 与 build_input.py 同一套哈希口径

DEFAULT_GT = os.path.join(HERE, "ground_truth.json")
DEFAULT_MANIFEST = os.path.join(HERE, "input_manifest.json")

FORMAT_DOMAIN = {"json", "csv", "txt", "md", "log", "other"}
ENCODING_DOMAIN = {"utf-8", "gbk", "other"}
NUMBER_RE = re.compile(r"^[+-]?\d+(\.\d+)?$")      # 十进制定点，禁科学计数法
INTEGER_RE = re.compile(r"^\d+$")
MAX_ERRORS = 10

# 部署守卫：这些文件若出现在工作区内，意味着产品可直接读到答案
EVAL_ASSET_NAMES = {"ground_truth.json", "checker.py", "build_input.py",
                    "input_manifest.json", "test_checker.py"}


# ---------- 基础工具 ----------

def _normalize(text):
    if text is None:
        return ""
    return text.replace("﻿", "").replace("\r\n", "\n").replace("\r", "\n")


def _cap(errors):
    if len(errors) <= MAX_ERRORS:
        return errors
    return errors[:MAX_ERRORS] + ["……另有 %d 条未列出" % (len(errors) - MAX_ERRORS)]


def iter_input_paths(input_dir):
    """遍历输入目录，返回相对路径集合（/ 分隔，相对 input_dir 根）。

    仅供自测与诊断使用；判定用的期望路径一律取 ground_truth.json 的冻结 paths。
    """
    return inputpkg.iter_input_paths(input_dir)


def parse_csv_table(text):
    """解析 CSV 文本，返回 (header, rows)；header 已 lowercase + strip。失败返回 (None, None)。"""
    if text is None:
        return None, None
    text = _normalize(text)
    rows = []
    for row in csv.reader(io.StringIO(text)):
        if row and any(c.strip() for c in row):
            rows.append([c.strip() for c in row])
    if not rows:
        return None, None
    return [c.lower() for c in rows[0]], rows[1:]


def find_fenced_blocks(text):
    """提取所有 ``` 围起的代码块，返回 [(lang, content), ...]。"""
    if text is None:
        return []
    blocks = re.findall(r"```[ \t]*([^\n`]*)[ \t]*\r?\n(.*?)```", text, re.DOTALL)
    return [(lang.strip().lower(), content.strip("\n")) for lang, content in blocks]


def _first_nonempty_line(text):
    for line in text.split("\n"):
        if line.strip():
            return line
    return ""


def _header_kind(line):
    """判断一行是否形如 inventory / extract 表头（列名大小写、空格不敏感）。"""
    cols = {c.strip().lower() for c in line.split(",") if c.strip()}
    if "path" in cols:
        return "inventory"
    if "key" in cols and "value" in cols:
        return "extract"
    return None


def extract_csv_texts_from_response(text):
    """从响应文本按表头识别 inventory / extract 的 CSV 文本。

    返回 dict：inventory / extract 为选中的文本（无则 None），
    并记录各自候选块数量 candidates，用于识别「草稿表 + 正表」造成的歧义。
    """
    result = {"inventory": None, "extract": None,
              "inventory_candidates": 0, "extract_candidates": 0}
    for _lang, content in find_fenced_blocks(text):
        if not content:
            continue
        kind = _header_kind(_first_nonempty_line(content))
        if not kind:
            continue
        result["%s_candidates" % kind] += 1
        if result[kind] is None:
            result[kind] = content
    return result


def find_files_named(root, name):
    """在 root 下递归查找名为 name 的文件，返回排序后的路径列表。"""
    hits = []
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if fn == name:
                hits.append(os.path.join(dirpath, fn))
    hits.sort()
    return hits


def _read_text(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def read_output_local(output_dir):
    """返回 dict：文本、候选数、命中的文件路径（用于输出证据）。"""
    inv = find_files_named(output_dir, "inventory.csv")
    ext = find_files_named(output_dir, "extract.csv")
    return {
        "inventory_text": _read_text(inv[0]) if inv else None,
        "extract_text": _read_text(ext[0]) if ext else None,
        "inventory_candidates": len(inv),
        "extract_candidates": len(ext),
        "files": inv + ext,
    }


# ---------- 输入完整性（5b / 5c / 5d） ----------

def check_input_integrity(input_dir, ground_truth, manifest_path, ground_truth_path=None):
    """验证「这次判的输入包 = 清单锁定的那一份」，并确认检查器用的是同一份 ground_truth。

    任一不符即判输入完整性失败：此时产品表现不可判定，
    应记为该 Run 环境问题（协议附 A 需扩展条目，见 README）。
    """
    live_paths = inputpkg.iter_input_paths(input_dir)
    frozen = set(ground_truth.get("paths", []))
    errors = []

    if not frozen:
        errors.append("ground_truth.json 缺少冻结 paths（期望路径会随输入漂移）")
    missing_in_live = sorted(frozen - live_paths)
    extra_in_live = sorted(live_paths - frozen)
    if missing_in_live:
        errors.append("输入包缺少冻结路径: %s" % missing_in_live)
    if extra_in_live:
        errors.append("输入包出现冻结清单外路径: %s" % extra_in_live)

    live_hash = inputpkg.content_hash(input_dir)
    manifest = None
    if not manifest_path or not os.path.exists(manifest_path):
        errors.append("input_manifest.json 缺失，无法校验 content_hash")
    else:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        expected_hash = manifest.get("content_hash")
        if not expected_hash:
            errors.append("input_manifest.json 未含 content_hash")
        elif expected_hash != live_hash:
            errors.append("content_hash 不一致：现场 %s ≠ 清单 %s"
                          % (live_hash[:16] + "…", expected_hash[:16] + "…"))

    gt_sha = None
    gt_path = ground_truth_path or DEFAULT_GT
    if os.path.exists(gt_path):
        gt_sha = inputpkg.sha256_file(gt_path)
        manifest_gt_sha = (manifest or {}).get("ground_truth_sha256")
        if manifest_gt_sha and manifest_gt_sha != gt_sha:
            errors.append("ground_truth 指纹与清单不一致：答案文件在冻结后被改动")

    return {
        "pass": not errors,
        "errors": errors,
        "content_hash_live": live_hash,
        "content_hash_expected": (manifest or {}).get("content_hash"),
        "paths_live": sorted(live_paths),
        "paths_frozen": sorted(frozen),
        "ground_truth_sha256": gt_sha,
    }


# ---------- 输出证据（5f） ----------

def inspect_output_file(path):
    """记录输出文件的字节级证据：哈希、大小、BOM、换行。只记录，不判定（H4 归属）。"""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return {"path": path, "error": "unreadable"}
    try:
        raw.decode("utf-8")
        utf8_ok = True
    except UnicodeDecodeError:
        utf8_ok = False
    if b"\r\n" in raw:
        newline = "crlf"
    elif b"\n" in raw:
        newline = "lf"
    else:
        newline = "none"
    return {
        "path": path,
        "sha256": inputpkg.sha256_bytes(raw),
        "size": len(raw),
        "bom_utf8": raw.startswith(b"\xef\xbb\xbf"),
        "newline": newline,
        "decodable_utf8": utf8_ok,
    }


# ---------- 部署守卫 ----------

def _is_within(path, root):
    try:
        return os.path.commonpath([os.path.abspath(path), os.path.abspath(root)]) == os.path.abspath(root)
    except ValueError:
        return False


def check_deployment(workspace, ground_truth_path):
    """检查答案资产是否落在产品可访问的工作区内。

    只能发现「资产被复制进工作区」这类可观测泄漏，
    无法发现产品读取工作区外文件、记忆答案或凭空写出正确答案。
    """
    if not workspace:
        return {"leak_guard": "not_checked", "workspace": None, "eval_assets_in_workspace": []}
    ws = os.path.abspath(workspace)
    assets = []
    if os.path.exists(ws) and _is_within(ground_truth_path, ws):
        assets.append(os.path.abspath(ground_truth_path))
    if os.path.isdir(ws):
        for dirpath, _dirs, files in os.walk(ws):
            for fn in files:
                if fn in EVAL_ASSET_NAMES:
                    full = os.path.join(dirpath, fn)
                    if full not in assets:
                        assets.append(full)
    return {"leak_guard": "fail" if assets else "pass",
            "workspace": ws,
            "eval_assets_in_workspace": assets}


# ---------- schema 检查 ----------

def _schema_result(errors):
    return {"pass": not errors, "errors": errors}


def check_schema_inventory(header, rows):
    if header is None:
        return _schema_result(["inventory 缺失或无法解析"])
    if len(header) != 3 or set(header) != {"path", "format", "encoding"}:
        return _schema_result([
            "inventory 表头必须正好是 path,format,encoding 三列，实际 %s" % header])
    idx = {c: i for i, c in enumerate(header)}
    errors = []
    seen = set()
    for n, row in enumerate(rows, start=2):
        if len(row) != 3:
            errors.append("inventory 第 %d 行应为 3 列，实际 %d 列" % (n, len(row)))
            continue
        p = _normalize_path(row[idx["path"]])
        if not p:
            errors.append("inventory 第 %d 行 path 为空" % n)
            continue
        if p in seen:
            errors.append("inventory 第 %d 行 path 重复: %s" % (n, p))
        seen.add(p)
        fmt = row[idx["format"]].strip().lower()
        enc = row[idx["encoding"]].strip().lower()
        if fmt not in FORMAT_DOMAIN:
            errors.append("inventory 第 %d 行 format 值域非法: %r" % (n, row[idx["format"]]))
        if enc not in ENCODING_DOMAIN:
            errors.append("inventory 第 %d 行 encoding 值域非法: %r" % (n, row[idx["encoding"]]))
    return _schema_result(_cap(errors))


def check_schema_extract(header, rows, gt_extract):
    if header is None:
        return _schema_result(["extract 缺失或无法解析"])
    if len(header) != 2 or set(header) != {"key", "value"}:
        return _schema_result(["extract 表头必须正好是 key,value 两列，实际 %s" % header])
    idx = {c: i for i, c in enumerate(header)}
    errors = []
    seen = set()
    for n, row in enumerate(rows, start=2):
        if len(row) != 2:
            errors.append("extract 第 %d 行应为 2 列，实际 %d 列" % (n, len(row)))
            continue
        key = row[idx["key"]]
        val = row[idx["value"]]
        if not key:
            errors.append("extract 第 %d 行 key 为空" % n)
            continue
        if key in seen:
            errors.append("extract 第 %d 行 key 重复: %s" % (n, key))
        seen.add(key)
        if not val:
            errors.append("extract 第 %d 行 value 为空: %s" % (n, key))
        if key not in gt_extract:
            errors.append("extract 出现契约外的 key: %s" % key)
            continue
        typ = gt_extract[key]["type"]
        if typ == "number" and not NUMBER_RE.match(val):
            errors.append("extract %s 须为十进制定点数字（禁千分位/单位/科学计数法），实际 %r" % (key, val))
        elif typ == "integer" and not INTEGER_RE.match(val):
            errors.append("extract %s 须为整数，实际 %r" % (key, val))
    return _schema_result(_cap(errors))


# ---------- 内容比对 ----------

def _normalize_path(p):
    p = p.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    # 本地形态的资料包根是 input/，产品可能把工作区当根输出 input/xxx；
    # 云端 ZIP 根天然无前缀。剥掉前导 input/ 使两形态路径对齐，不因执行基板差异扣分。
    if p.startswith("input/"):
        p = p[len("input/"):]
    return p


def check_inventory(header, rows, expected_paths):
    if header is None or "path" not in header:
        return {"expected": len(expected_paths), "found": 0,
                "missing": sorted(expected_paths), "extra": [], "pass": False}
    idx = header.index("path")
    found = set()
    for row in rows:
        if len(row) > idx:
            p = _normalize_path(row[idx])
            if p:
                found.add(p)
    missing = sorted(expected_paths - found)
    extra = sorted(found - expected_paths)
    return {"expected": len(expected_paths), "found": len(found & expected_paths),
            "missing": missing, "extra": extra,
            "pass": not missing and not extra}


def _compare(spec, actual):
    if actual is None:
        return False
    typ = spec["type"]
    expected = spec["value"]
    if typ == "string":
        return actual == expected
    if typ == "number":
        if not NUMBER_RE.match(actual):
            return False
        try:
            return Decimal(actual) == Decimal(str(expected))
        except InvalidOperation:
            return False
    if typ == "integer":
        if not INTEGER_RE.match(actual):
            return False
        try:
            return int(actual) == int(expected)
        except ValueError:
            return False
    return False


def check_extract(header, rows, gt_extract):
    if header is None or "key" not in header or "value" not in header:
        results = [{"key": k, "expected": s["value"], "actual": None, "pass": False}
                   for k, s in gt_extract.items()]
        return {"expected": len(gt_extract), "found": 0, "results": results,
                "extra_keys": [], "pass": False}
    key_idx = header.index("key")
    val_idx = header.index("value")
    actual = {}
    for row in rows:
        if len(row) <= key_idx:
            continue
        k = row[key_idx].strip()
        if k and k not in actual:
            actual[k] = row[val_idx].strip() if len(row) > val_idx else ""
    results = []
    for k, spec in gt_extract.items():
        av = actual.get(k)
        results.append({"key": k, "expected": spec["value"], "actual": av,
                        "pass": _compare(spec, av)})
    extra_keys = sorted(set(actual) - set(gt_extract))
    found = sum(1 for r in results if r["pass"])
    return {"expected": len(gt_extract), "found": found, "results": results,
            "extra_keys": extra_keys, "pass": found == len(results) and len(results) > 0}


def collect_format_encoding(header, rows):
    """采集 format/encoding 标注，仅用于报告；标注正确性不进门禁（问题2 挂起）。"""
    out = []
    if header is None:
        return out
    idx = {c: i for i, c in enumerate(header) if c in ("path", "format", "encoding")}
    if "path" not in idx:
        return out
    for row in rows:
        item = {"path": _normalize_path(row[idx["path"]]) if len(row) > idx["path"] else ""}
        for col in ("format", "encoding"):
            item[col] = row[idx[col]].strip() if col in idx and len(row) > idx[col] else ""
        if item["path"]:
            out.append(item)
    return out


# ---------- 汇总 ----------

def check_output(expected_paths, ground_truth, inventory_text, extract_text,
                 inv_candidates=1, ext_candidates=1, deployment=None,
                 integrity=None, output_evidence=None):
    gt_extract = ground_truth.get("extract", {})
    inv_header, inv_rows = parse_csv_table(inventory_text)
    ext_header, ext_rows = parse_csv_table(extract_text)

    schema_inv = check_schema_inventory(inv_header, inv_rows if inv_rows is not None else [])
    schema_ext = check_schema_extract(ext_header, ext_rows if ext_rows is not None else [], gt_extract)

    # 候选歧义：本地多个同名文件 / 云端多个同表头代码块 → 无法确定哪份是最终交付
    ambiguous = []
    if inv_candidates > 1:
        ambiguous.append("inventory 存在 %d 份候选（多个文件或多个同表头代码块）" % inv_candidates)
    if ext_candidates > 1:
        ambiguous.append("extract 存在 %d 份候选（多个文件或多个同表头代码块）" % ext_candidates)
    if ambiguous:
        schema_inv = _schema_result(schema_inv["errors"] + ambiguous)

    inventory = check_inventory(inv_header, inv_rows or [], expected_paths)
    extract = check_extract(ext_header, ext_rows or [], gt_extract)
    fmt = collect_format_encoding(inv_header, inv_rows or [])

    parse_ok = inv_header is not None and ext_header is not None
    parse_errors = []
    if inv_header is None:
        parse_errors.append("inventory 缺失或无法解析")
    if ext_header is None:
        parse_errors.append("extract 缺失或无法解析")

    deploy = deployment or {"leak_guard": "not_checked"}
    leak_fail = deploy.get("leak_guard") == "fail"
    integrity_fail = integrity is not None and not integrity.get("pass", False)

    # 输入不完整 / 部署泄漏时，产品表现不可判定，单列原因，不与产品失败混为一谈
    invalid_reason = None
    if integrity_fail:
        invalid_reason = "input_integrity"
    elif leak_fail:
        invalid_reason = "deployment_leak"

    verdict = {
        "parse": {"ok": parse_ok, "error": "; ".join(parse_errors) or None},
        "schema": {"inventory": schema_inv, "extract": schema_ext,
                   "pass": schema_inv["pass"] and schema_ext["pass"]},
        "inventory": inventory,
        "extract": extract,
        "format_encoding": fmt,
        "deployment": deploy,
        "overall_pass": (parse_ok
                         and schema_inv["pass"]
                         and schema_ext["pass"]
                         and inventory["pass"]
                         and extract["pass"]
                         and not leak_fail
                         and not integrity_fail),
        "invalid_reason": invalid_reason,
    }
    if integrity is not None:
        verdict["input_integrity"] = integrity
    if output_evidence is not None:
        verdict["output_files"] = output_evidence
    return verdict


def main(argv=None):
    ap = argparse.ArgumentParser(description="T1 外部检查器")
    ap.add_argument("--input-dir", required=True, help="输入包目录（期望路径清单来源）")
    ap.add_argument("--ground-truth", default=DEFAULT_GT, help="ground_truth.json 路径")
    ap.add_argument("--manifest", default=DEFAULT_MANIFEST,
                    help="input_manifest.json 路径（校验 content_hash / ground_truth 指纹）")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--output-dir", help="本地产品输出目录")
    src.add_argument("--response", help="云端产品响应文本文件")
    ap.add_argument("--workspace", help="产品工作区（可选；检查答案资产是否泄漏进工作区）")
    ap.add_argument("--out", help="verdict JSON 输出路径（默认 stdout）")
    ap.add_argument("--product", help="产品名（仅透传，不参与判定）")
    args = ap.parse_args(argv)

    with open(args.ground_truth, "r", encoding="utf-8") as fh:
        gt = json.load(fh)

    # 期望路径取冻结值，不取现场遍历结果（防止输入被污染后期望跟着漂移）
    expected_paths = set(gt.get("paths", []))

    integrity = check_input_integrity(args.input_dir, gt, args.manifest,
                                      ground_truth_path=args.ground_truth)

    output_evidence = []
    if args.output_dir:
        out = read_output_local(args.output_dir)
        inventory_text, extract_text = out["inventory_text"], out["extract_text"]
        inv_c, ext_c = out["inventory_candidates"], out["extract_candidates"]
        mode = "output-dir"
        output_evidence = [inspect_output_file(p) for p in out["files"]]
    else:
        with open(args.response, "r", encoding="utf-8-sig") as fh:
            resp = fh.read()
        blocks = extract_csv_texts_from_response(resp)
        inventory_text, extract_text = blocks["inventory"], blocks["extract"]
        inv_c = blocks["inventory_candidates"]
        ext_c = blocks["extract_candidates"]
        mode = "response"
        output_evidence = [{
            "path": args.response,
            "sha256": inputpkg.sha256_bytes(resp.encode("utf-8")),
            "size": len(resp.encode("utf-8")),
            "source": "response-text",
        }]

    deploy = check_deployment(args.workspace, args.ground_truth)
    verdict = check_output(expected_paths, gt, inventory_text, extract_text,
                           inv_candidates=inv_c, ext_candidates=ext_c,
                           deployment=deploy, integrity=integrity,
                           output_evidence=output_evidence)
    verdict["task"] = "T1"
    verdict["mode"] = mode
    if args.product:
        verdict["product"] = args.product
    verdict["expected_paths"] = sorted(expected_paths)

    text = json.dumps(verdict, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        print(text)
    return 0 if verdict["overall_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
