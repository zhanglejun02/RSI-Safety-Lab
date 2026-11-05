"""Measure integration deltas against preserved, uncommitted source snapshots."""
import argparse
from difflib import SequenceMatcher
import hashlib
from pathlib import Path
from zipfile import ZipFile

from rsi_lab.engine.continuation import read_record, save_record
from rsi_lab.serialization import digest

REPO = Path(__file__).resolve().parents[1]


def archive(path, hashes):
    with ZipFile(path) as z:
        files = {n: z.read(n) for n in z.namelist()}
    assert all(hashlib.sha256(files[n]).hexdigest() == h for n, h in hashes.items())
    return files


def compare(before, after, prefixes=("src/",)):
    rows = []
    for name in sorted(before.keys() | after.keys()):
        if not name.startswith(prefixes) or not name.endswith(".py") or before.get(name) == after.get(name):
            continue
        old, new = before.get(name, b"").decode().splitlines(), after.get(name, b"").decode().splitlines()
        changes = SequenceMatcher(a=old, b=new, autojunk=False).get_opcodes()
        rows.append({"path": name, "added_lines": sum(j2-j1 for t, i1, i2, j1, j2 in changes if t != "equal"),
            "deleted_lines": sum(i2-i1 for t, i1, i2, j1, j2 in changes if t != "equal"),
            "before_sha256": hashlib.sha256(before[name]).hexdigest() if name in before else None,
            "after_sha256": hashlib.sha256(after[name]).hexdigest() if name in after else None})
    return rows


def report(output):
    old_dir = REPO / "runs/stress-readiness-20261105/registration"
    previous_dir = REPO / "runs/mechanism-registration-20261105"
    current_dir = REPO / "runs/adaptation-rl-registration-20261105"
    baseline_dir = REPO / "runs/adaptation-rl-preparation-20261105"
    old, previous, current = [read_record(p / "registration.json") for p in (old_dir, previous_dir, current_dir)]
    baseline = read_record(baseline_dir / "baseline.json")
    first = archive(old_dir / "source-snapshot.zip", old["source_files_sha256"])
    second = archive(previous_dir / "source-snapshot.zip", previous["source_hashes"])
    start = archive(baseline_dir / "baseline.zip", baseline["files"])
    end = archive(current_dir / "source-snapshot.zip", current["source_hashes"])
    # These exact prior archives cover all installed source, but not the old
    # tests/scripts. Do not interpret archive-selection differences as deletions.
    prior_rows = compare(first, second)
    current_rows = compare(start, end)
    kernel_prefixes = ("src/rsi_lab/api/", "src/rsi_lab/engine/", "src/rsi_lab/infra/")
    for rows in (prior_rows, current_rows):
        assert not any(r["path"].startswith(kernel_prefixes) for r in rows)
    tests = {str(p.relative_to(REPO)): p.read_bytes() for p in (REPO / "tests/platform").glob("*.py")}
    test_rows = compare(start, tests, ("tests/platform/",))
    value = {"method": "Python files only;physical lines including blanks/comments;difflib.SequenceMatcher(autojunk=False);not labor hours",
        "previous_integration": {"baseline_registration_sha256": digest(old), "final_registration_sha256": digest(previous),
            "files": prior_rows, "tests_scripts_increment": None, "hours": None},
        "current_integration": {"baseline_manifest_sha256": digest(baseline), "final_registration_sha256": digest(current),
            "source_files": current_rows, "test_files": test_rows, "hours": None},
        "public_api_engine_infra_source_modified": False,
        "baseline_already_dirty": baseline["existing_changes"],
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    save_record(output / "integration.json", value)
    lines = ["# 接入增量的可复核计量", "", "口径：仅 Python 文件的物理行，含空行与注释；逐文件 SequenceMatcher(autojunk=False)。JSON 数据／LICENSE 的基线收集范围不同，排除；不能把归档缺席当新增。只比较具备完整前后源码的范围，不能当作工时或方法复杂度。", "",
             "此前机制接入：stress-readiness 冻结源码 → mechanism-registration 冻结源码。测试／脚本的完整前基线未归档，旧增量记未知。", "",
             "| 文件 | 增加行 | 删除行 |", "| --- | --- | --- |"]
    for r in prior_rows:
        lines.append(f"| {r['path']} | {r['added_lines']} | {r['deleted_lines']} |")
    lines += ["", "本轮：实施前工作区 baseline.zip → 本轮付费登记源码。保留了实施前的全部未提交修改；不以 Git HEAD 混入此前工作。", "",
              "| 文件 | 增加行 | 删除行 |", "| --- | --- | --- |"]
    for r in current_rows + test_rows:
        lines.append(f"| {r['path']} | {r['added_lines']} | {r['deleted_lines']} |")
    lines += ["", "两次接入的 src/rsi_lab/api、engine、infra 均无源码差异。适配器局部修改、研究驱动和任务包不计作公共内核。",
              "新增配置、研究协议、验证／计量脚本和结果文档另列在接入说明，未计入上表。工时未知。"]
    (output / "lines.md").write_text("\n".join(lines) + "\n")
    return value


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    result = report(p.parse_args().output)
    print({"previous_source_files": len(result["previous_integration"]["files"]),
           "current_source_files": len(result["current_integration"]["source_files"]),
           "kernel_modified": result["public_api_engine_infra_source_modified"]})
