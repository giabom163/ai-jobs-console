# -*- coding: utf-8 -*-
"""生产上线回读实证与哈希对账。
从 site/build-manifest.json 读取最新产物清单，与正式域及可选预览域逐项对账 SHA-256、
生成时间与按状态拆分的岗位数；验证账本输出到 reports/，不污染发布目录 site/。
"""
import argparse
import hashlib
import json
import ssl
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(r"D:\DSH\repos\ai-employment-monitor")
SITE = ROOT / "site"
REPORTS = ROOT / "reports"

RETIRED_ALI_IDS = [
    'job-5bb801bc9dac6c803485d76a',
    'job-d8294a0a5947e6619ba56f63',
    'job-37039a3661bb33dde31eff8b',
    'job-abf304ed717354a176b64664',
    'job-53754e73edd39dcf75d07dd5',
]
RETIRED_CMCC_ID = 'job-44f7b7c3337afd006a2ab6c8'


def get_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch(url: str) -> tuple[int, bytes, dict]:
    ctx = ssl.create_default_context()
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AIEmploymentMonitor/1.0"}
    )
    with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
        return resp.getcode(), resp.read(), dict(resp.getheaders())


def count_by_status(jobs: list[dict]) -> dict[str, int]:
    return dict(sorted(Counter(str(j.get("status") or "unknown") for j in jobs).items()))


def main():
    parser = argparse.ArgumentParser(description="线上静态发布包回读与哈希对账")
    parser.add_argument("--prod-url", default="https://giabom.online", help="正式域名")
    parser.add_argument("--preview-url", default="", help="可选的本次部署预览域 URL")
    parser.add_argument("--out", default=str(REPORTS / "latest-online-verification.json"), help="输出账本路径（默认写入 reports/）")
    parser.add_argument("--check-retired-sample", action="store_true", help="额外检查历史淘汰样本是否已从公开索引清除")
    args = parser.parse_args()

    out_path = Path(args.out).resolve()
    site_root = SITE.resolve()
    if out_path == site_root or out_path.is_relative_to(site_root):
        raise ValueError(f"拒绝将验证账本写入发布目录 site/ 及其子目录: {out_path}")

    manifest_path = SITE / "build-manifest.json"
    assert manifest_path.exists(), f"构建清单不存在: {manifest_path}"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    index_name = manifest["index_file"]
    expected_total = int(manifest["jobs"])
    expected_generated_at = manifest["generated_at"]
    manifest_index_sha = manifest["files"][index_name]

    local_index_path = SITE / index_name
    local_html_path = SITE / "index.html"
    assert local_index_path.exists(), f"本地主索引文件不存在: {local_index_path}"
    assert local_html_path.exists(), f"本地 index.html 不存在: {local_html_path}"

    local_index_bytes = local_index_path.read_bytes()
    local_index_sha = get_sha256(local_index_bytes)
    assert local_index_sha == manifest_index_sha, (
        f"本地主索引与 manifest 哈希不一致: {local_index_sha} != {manifest_index_sha}"
    )

    local_index_data = json.loads(local_index_bytes.decode("utf-8"))
    local_jobs = local_index_data.get("jobs", [])
    assert len(local_jobs) == expected_total, (
        f"本地主索引条数与 manifest 不一致: {len(local_jobs)} != {expected_total}"
    )
    local_status_counts = count_by_status(local_jobs)
    local_html_size = len(local_html_path.read_bytes())

    print("=== 本地基线数据 ===")
    print(f"构建时间: {expected_generated_at}")
    print(f"本地主索引: {index_name}")
    print(f"本地主索引 SHA-256: {local_index_sha}")
    print(f"本地岗位总数: {len(local_jobs)} (按状态: {local_status_counts})")
    print(f"本地 index.html 字节数: {local_html_size}")

    domains = [("正式域名", args.prod_url.rstrip("/"))]
    if args.preview_url.strip():
        domains.append(("部署预览域", args.preview_url.strip().rstrip("/")))

    ledger = {
        "generated_at": expected_generated_at,
        "index_file": index_name,
        "local_index_sha256": local_index_sha,
        "expected_total": expected_total,
        "expected_status_counts": local_status_counts,
        "domains": {},
    }

    for name, base_url in domains:
        print(f"\n=== 开始回读校验: {name} ({base_url}) ===")
        code_html, body_html, _ = fetch(base_url + "/")
        assert code_html == 200, f"{name} 首页 HTTP 状态码异常: {code_html}"
        html_size = len(body_html)
        assert index_name in body_html.decode("utf-8", errors="ignore"), (
            f"{name} 首页未引用最新索引 {index_name}"
        )

        code_idx, body_idx, _ = fetch(f"{base_url}/{index_name}")
        assert code_idx == 200, f"{name} 主索引 HTTP 状态码异常: {code_idx}"
        online_index_sha = get_sha256(body_idx)
        assert online_index_sha == local_index_sha, (
            f"{name} 线上主索引与本地 SHA-256 不匹配: {online_index_sha} != {local_index_sha}"
        )

        online_data = json.loads(body_idx.decode("utf-8"))
        online_jobs = online_data.get("jobs", [])
        online_status_counts = count_by_status(online_jobs)
        assert len(online_jobs) == expected_total, (
            f"{name} 线上岗位总数异常: {len(online_jobs)} != {expected_total}"
        )
        assert online_status_counts == local_status_counts, (
            f"{name} 线上按状态计数与本地不一致: {online_status_counts} != {local_status_counts}"
        )
        assert online_data.get("generated_at") == expected_generated_at, (
            f"{name} 生成时间不一致: {online_data.get('generated_at')} != {expected_generated_at}"
        )

        if args.check_retired_sample:
            online_ids = {j["id"] for j in online_jobs}
            assert RETIRED_CMCC_ID not in online_ids, f"{name} 仍存在过期岗位: {RETIRED_CMCC_ID}"
            for jid in RETIRED_ALI_IDS:
                assert jid not in online_ids, f"{name} 仍存在 2025 年旧岗位: {jid}"

        print(f"[{name}] PASS: 总数={len(online_jobs)}, 状态={online_status_counts}, SHA-256={online_index_sha}")
        ledger["domains"][name] = {
            "base_url": base_url,
            "status_code": 200,
            "index_sha256": online_index_sha,
            "sha256_match": True,
            "jobs_count": len(online_jobs),
            "status_counts": online_status_counts,
            "generated_at": online_data.get("generated_at"),
            "html_bytes": html_size,
        }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(ledger, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[验收通过] 账本已写入: {out_path}")


if __name__ == "__main__":
    main()
