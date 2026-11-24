# -*- coding: utf-8 -*-
"""checker 自测：覆盖 空/错/对、云端响应、前缀容错、schema 门禁、歧义、部署守卫。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import checker

# 保证输出为 UTF-8（Windows 控制台/重定向默认 GBK，会污染自测日志）
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError, OSError):
    pass

CASE_DIR = os.path.dirname(HERE)
INPUT_DIR = os.path.join(CASE_DIR, "input")
GT_PATH = os.path.join(HERE, "ground_truth.json")

import json

CORRECT_INVENTORY = (
    "path,format,encoding\n"
    "config.json,json,utf-8\n"
    "报表.txt,txt,gbk\n"
    "app.log,log,utf-8\n"
    "notes/readme.md,md,utf-8\n"
    "data/2024.csv,csv,utf-8\n"
    "data/empty.txt,txt,other\n"
)

CORRECT_EXTRACT = (
    "key,value\n"
    "config.version,1.4.2\n"
    "report.total,1284500.00\n"
    "sales.row_count,12\n"
    "log.last_status,OK\n"
    "readme.owner,张三\n"
)

WRONG_INVENTORY = (
    "path,format,encoding\n"
    "config.json,json,utf-8\n"
    "报表.txt,txt,gbk\n"
    "app.log,log,utf-8\n"
    "notes/readme.md,md,utf-8\n"
    "data/2024.csv,csv,utf-8\n"
)

WRONG_EXTRACT = (
    "key,value\n"
    "config.version,1.4.2\n"
    "report.total,999.00\n"
    "sales.row_count,13\n"
    "log.last_status,OK\n"
    "readme.owner,张三\n"
)

# 4b：只剩 path 一列
PATH_ONLY_INVENTORY = (
    "path\n"
    "config.json\n报表.txt\napp.log\nnotes/readme.md\ndata/2024.csv\ndata/empty.txt\n"
)

# 4c：列齐全但 format 值域非法
BAD_DOMAIN_INVENTORY = CORRECT_INVENTORY.replace("报表.txt,txt,gbk", "报表.txt,excel,gbk")

# 4d：科学计数法
SCI_EXTRACT = CORRECT_EXTRACT.replace("report.total,1284500.00", "report.total,1.2845e6")

# 4d 变体：去掉 .00（数值等价，允许）
NO_TRAILING_ZERO_EXTRACT = CORRECT_EXTRACT.replace("report.total,1284500.00", "report.total,1284500")

# 4e：契约外的 key
EXTRA_KEY_EXTRACT = CORRECT_EXTRACT + "readme.version,1.0\n"

PREFIXED_INVENTORY = (
    "path,format,encoding\n"
    "input/config.json,json,utf-8\n"
    "input/报表.txt,txt,gbk\n"
    "input/app.log,log,utf-8\n"
    "input/notes/readme.md,md,utf-8\n"
    "input/data/2024.csv,csv,utf-8\n"
    "input/data/empty.txt,txt,other\n"
)

CLOUD_LOOSE = (
    "```csv\n"
    "Path, Format, Encoding\n"
    "config.json,json,utf-8\n"
    "报表.txt,txt,gbk\n"
    "app.log,log,utf-8\n"
    "notes/readme.md,md,utf-8\n"
    "data/2024.csv,csv,utf-8\n"
    "data/empty.txt,txt,other\n"
    "```\n\n"
    "```CSV\n"
    "Key,Value\n"
    "config.version,1.4.2\n"
    "report.total,1284500.00\n"
    "sales.row_count,12\n"
    "log.last_status,OK\n"
    "readme.owner,张三\n"
    "```\n"
)

# 4f：先贴一个「示例表」，再贴正表
CLOUD_AMBIGUOUS = (
    "下面是一个格式示例（草稿）：\n\n"
    "```csv\npath,format,encoding\n示例.txt,txt,utf-8\n```\n\n"
    "正确结果：\n\n"
    "```csv\n" + CORRECT_INVENTORY + "```\n\n"
    "```csv\n" + CORRECT_EXTRACT + "```\n"
)


def main():
    results = []

    def record(name, cond, detail=""):
        results.append(bool(cond))
        print("[%s] %-34s %s" % ("PASS" if cond else "FAIL", name, detail))

    with open(GT_PATH, encoding="utf-8") as fh:
        gt = json.load(fh)
    paths = set(gt["paths"])          # 判定用冻结期望，与 checker 主流程一致

    # 基线三态
    v = checker.check_output(paths, gt, None, None)
    record("empty-output", v["overall_pass"] is False and v["parse"]["ok"] is False)

    v = checker.check_output(paths, gt, WRONG_INVENTORY, WRONG_EXTRACT)
    record("wrong-output",
           v["overall_pass"] is False
           and v["inventory"]["missing"] == ["data/empty.txt"]
           and v["extract"]["found"] == 3)

    v = checker.check_output(paths, gt, CORRECT_INVENTORY, CORRECT_EXTRACT)
    record("correct-output", v["overall_pass"] is True,
           "inv=%d ext=%d" % (v["inventory"]["found"], v["extract"]["found"]))

    # 云端
    blocks = checker.extract_csv_texts_from_response(
        "```csv\n" + CORRECT_INVENTORY + "```\n```csv\n" + CORRECT_EXTRACT + "```\n")
    v = checker.check_output(paths, gt, blocks["inventory"], blocks["extract"],
                             inv_candidates=blocks["inventory_candidates"],
                             ext_candidates=blocks["extract_candidates"])
    record("cloud-response", v["overall_pass"] is True)

    # 前缀容错
    v = checker.check_output(paths, gt, PREFIXED_INVENTORY, CORRECT_EXTRACT)
    record("prefix-tolerance(input/)", v["overall_pass"] is True,
           "missing=%s extra=%s" % (v["inventory"]["missing"], v["inventory"]["extra"]))

    # 云端表头宽松
    b = checker.extract_csv_texts_from_response(CLOUD_LOOSE)
    v = checker.check_output(paths, gt, b["inventory"], b["extract"],
                             inv_candidates=b["inventory_candidates"],
                             ext_candidates=b["extract_candidates"])
    record("cloud-loose-header", v["overall_pass"] is True)

    # 4b：缺列
    v = checker.check_output(paths, gt, PATH_ONLY_INVENTORY, CORRECT_EXTRACT)
    record("4b-schema-missing-columns",
           v["overall_pass"] is False and v["schema"]["inventory"]["pass"] is False,
           v["schema"]["inventory"]["errors"][:1])

    # 4c：值域非法
    v = checker.check_output(paths, gt, BAD_DOMAIN_INVENTORY, CORRECT_EXTRACT)
    record("4c-schema-bad-format-value",
           v["overall_pass"] is False
           and any("值域非法" in e for e in v["schema"]["inventory"]["errors"]),
           v["schema"]["inventory"]["errors"][:1])

    # 4d：科学计数法拒绝
    v = checker.check_output(paths, gt, CORRECT_INVENTORY, SCI_EXTRACT)
    record("4d-scientific-notation-rejected", v["overall_pass"] is False,
           v["schema"]["extract"]["errors"][:1])

    # 4d 变体：去掉尾零仍等价（设计允许）
    v = checker.check_output(paths, gt, CORRECT_INVENTORY, NO_TRAILING_ZERO_EXTRACT)
    record("4d-trailing-zero-accepted", v["overall_pass"] is True)

    # 4e：多余 key 拒绝
    v = checker.check_output(paths, gt, CORRECT_INVENTORY, EXTRA_KEY_EXTRACT)
    record("4e-extra-key-rejected",
           v["overall_pass"] is False and v["extract"]["extra_keys"] == ["readme.version"],
           v["schema"]["extract"]["errors"][:1])

    # 4f：云端歧义
    b = checker.extract_csv_texts_from_response(CLOUD_AMBIGUOUS)
    v = checker.check_output(paths, gt, b["inventory"], b["extract"],
                             inv_candidates=b["inventory_candidates"],
                             ext_candidates=b["extract_candidates"])
    record("4f-cloud-ambiguous-rejected",
           v["overall_pass"] is False and b["inventory_candidates"] == 2,
           "candidates=%d" % b["inventory_candidates"])

    # 部署守卫：答案资产在工作区内
    import tempfile
    ws = tempfile.mkdtemp()
    with open(os.path.join(ws, "ground_truth.json"), "w", encoding="utf-8") as fh:
        fh.write("{}")
    deploy = checker.check_deployment(ws, GT_PATH)
    v = checker.check_output(paths, gt, CORRECT_INVENTORY, CORRECT_EXTRACT,
                             deployment=deploy)
    record("deployment-leak-detected",
           deploy["leak_guard"] == "fail" and v["overall_pass"] is False,
           "assets=%d" % len(deploy["eval_assets_in_workspace"]))

    # 部署守卫：干净工作区
    ws2 = tempfile.mkdtemp()
    deploy2 = checker.check_deployment(ws2, GT_PATH)
    v = checker.check_output(paths, gt, CORRECT_INVENTORY, CORRECT_EXTRACT,
                             deployment=deploy2)
    record("deployment-clean-pass",
           deploy2["leak_guard"] == "pass" and v["overall_pass"] is True)

    # 5b/5c/5d：输入完整性与指纹
    manifest = os.path.join(HERE, "input_manifest.json")
    integ = checker.check_input_integrity(INPUT_DIR, gt, manifest, GT_PATH)
    record("5b-5c-input-integrity-pass", integ["pass"] is True, integ["errors"][:1])

    gt_drift = dict(gt)
    gt_drift["paths"] = list(gt["paths"]) + ["ghost.txt"]
    integ = checker.check_input_integrity(INPUT_DIR, gt_drift, manifest, GT_PATH)
    record("5b-frozen-path-drift-detected",
           integ["pass"] is False
           and any("ghost.txt" in e for e in integ["errors"]),
           integ["errors"][:1])
    v = checker.check_output(paths, gt, CORRECT_INVENTORY, CORRECT_EXTRACT,
                             integrity=integ)
    record("5b-integrity-fail-blocks-verdict",
           v["overall_pass"] is False and v["invalid_reason"] == "input_integrity")

    tmpm = tempfile.mkdtemp()
    real = json.load(open(manifest, encoding="utf-8"))
    bad_hash = os.path.join(tmpm, "m_hash.json")
    with open(bad_hash, "w", encoding="utf-8") as fh:
        json.dump(dict(real, content_hash="0" * 64), fh)
    integ = checker.check_input_integrity(INPUT_DIR, gt, bad_hash, GT_PATH)
    record("5c-content-hash-mismatch-detected",
           integ["pass"] is False
           and any("content_hash 不一致" in e for e in integ["errors"]),
           integ["errors"][:1])

    bad_gt = os.path.join(tmpm, "m_gt.json")
    with open(bad_gt, "w", encoding="utf-8") as fh:
        json.dump(dict(real, ground_truth_sha256="f" * 64), fh)
    integ = checker.check_input_integrity(INPUT_DIR, gt, bad_gt, GT_PATH)
    record("5d-ground-truth-sha-mismatch-detected",
           integ["pass"] is False
           and any("ground_truth 指纹" in e for e in integ["errors"]),
           integ["errors"][:1])

    # 5f：输出字节证据
    tmpf = tempfile.mkdtemp()
    p = os.path.join(tmpf, "inventory.csv")
    with open(p, "wb") as fh:
        fh.write(b"\xef\xbb\xbfkey,value\r\n")
    info = checker.inspect_output_file(p)
    record("5f-output-evidence",
           info["bom_utf8"] is True and info["newline"] == "crlf"
           and len(info["sha256"]) == 64 and info["size"] == 14,
           "size=%d newline=%s bom=%s" % (info["size"], info["newline"], info["bom_utf8"]))

    with open(p, "wb") as fh:
        fh.write(b"path\tformat\tencoding\r\na.txt\ttxt\tutf-8\n")
    info = checker.inspect_output_file(p)
    record("5f-tab-delimiter-and-mixed-endings",
           info["comma_delimited"] is False and info["newline"] == "mixed")
    with open(p, "wb") as fh:
        fh.write(b'\xef\xbb\xbf"path","format","encoding"\r\na.txt,txt,utf-8\r\n')
    info = checker.inspect_output_file(p)
    record("5f-comma-delimiter-with-bom",
           info["comma_delimited"] is True and info["bom_utf8"] is True)

    print("-" * 50)
    if all(results):
        print("ALL PASS (%d/%d)" % (len(results), len(results)))
        return 0
    print("FAILED (%d/%d)" % (sum(results), len(results)))
    return 1


if __name__ == "__main__":
    sys.exit(main())
