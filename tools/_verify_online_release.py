# -*- coding: utf-8 -*-
"""生产上线回读实证与哈希对账。
通过网络实际请求正式域 https://giabom.online/ 与预览域，逐项对账 SHA-256 及岗位数量。
"""
import hashlib
import json
import ssl
import sys
import urllib.request
from pathlib import Path

ROOT = Path(r"D:\DSH\repos\ai-employment-monitor")
SITE = ROOT / "site"

INDEX_NAME = "jobs-index.cb789b6f58d6.json"
LOCAL_INDEX_PATH = SITE / INDEX_NAME
LOCAL_HTML_PATH = SITE / "index.html"

ALI_IDS = [
    'job-5bb801bc9dac6c803485d76a',
    'job-d8294a0a5947e6619ba56f63',
    'job-37039a3661bb33dde31eff8b',
    'job-abf304ed717354a176b64664',
    'job-53754e73edd39dcf75d07dd5',
]
CMCC_ID = 'job-44f7b7c3337afd006a2ab6c8'

def get_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def fetch(url: str) -> tuple[int, bytes, dict]:
    ctx = ssl.create_default_context()
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AIEmploymentMonitor/1.0"}
    )
    with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
        code = resp.getcode()
        body = resp.read()
        headers = dict(resp.getheaders())
        return code, body, headers

def main():
    assert LOCAL_INDEX_PATH.exists(), f"本地主索引文件不存在: {LOCAL_INDEX_PATH}"
    local_index_bytes = LOCAL_INDEX_PATH.read_bytes()
    local_index_sha = get_sha256(local_index_bytes)
    local_index_data = json.loads(local_index_bytes.decode("utf-8"))
    local_jobs = local_index_data.get("jobs", [])
    assert len(local_jobs) == 738, f"本地主索引条数异常: {len(local_jobs)}"

    local_html_bytes = LOCAL_HTML_PATH.read_bytes()
    local_html_size = len(local_html_bytes)

    print("=== 本地基线数据 ===")
    print(f"本地主索引: {INDEX_NAME}")
    print(f"本地主索引 SHA-256: {local_index_sha}")
    print(f"本地岗位条数: {len(local_jobs)}")
    print(f"本地 index.html 字节数: {local_html_size}")

    domains = [
        ("正式域名", "https://giabom.online"),
        ("部署预览域", "https://a3c894ba.giabom-blog.pages.dev"),
    ]

    ledger = {}

    for name, base_url in domains:
        print(f"\n=== 开始回读校验: {name} ({base_url}) ===")
        # 1. 回读首页 HTML
        code_html, body_html, _ = fetch(base_url + "/")
        assert code_html == 200, f"{name} 首页 HTTP 状态码异常: {code_html}"
        html_size = len(body_html)
        print(f"[{name}] 首页 HTTP 200, 字节数: {html_size}")
        
        # 验证是否引用了最新索引
        assert INDEX_NAME in body_html.decode("utf-8", errors="ignore"), (
            f"{name} 首页未引用最新索引 {INDEX_NAME}"
        )
        print(f"[{name}] 首页正确引用最新主索引文件名: {INDEX_NAME}")

        # 2. 回读主索引 JSON
        index_url = f"{base_url}/{INDEX_NAME}"
        code_idx, body_idx, _ = fetch(index_url)
        assert code_idx == 200, f"{name} 主索引 HTTP 状态码异常: {code_idx}"
        online_index_sha = get_sha256(body_idx)
        print(f"[{name}] 主索引 HTTP 200, 线上 SHA-256: {online_index_sha}")
        
        # 校验哈希严格一致
        is_match = (online_index_sha == local_index_sha)
        assert is_match, f"{name} 线上主索引与本地 SHA-256 不匹配！"
        print(f"[{name}] 主索引 SHA-256 与本地 100% 严格一致 (Match: True)")

        # 校验内容与岗位数
        online_data = json.loads(body_idx.decode("utf-8"))
        online_jobs = online_data.get("jobs", [])
        assert len(online_jobs) == 738, f"{name} 线上岗位数量非 738: {len(online_jobs)}"
        print(f"[{name}] 线上公开岗位数精确为 738 条")

        # 校验失效岗位确已被排除
        online_ids = set(j["id"] for j in online_jobs)
        assert CMCC_ID not in online_ids, f"{name} 线上仍存在过期岗位: {CMCC_ID}"
        for jid in ALI_IDS:
            assert jid not in online_ids, f"{name} 线上仍存在 2025 年旧岗位: {jid}"
        print(f"[{name}] 6 条失效/陈旧岗位已确证在线上完全清除！")

        ledger[name] = {
            "base_url": base_url,
            "status_code": 200,
            "index_sha256": online_index_sha,
            "sha256_match": True,
            "jobs_count": len(online_jobs),
            "html_bytes": html_size,
        }

    # 导出账本
    out_ledger = SITE / "latest-online-verification.json"
    out_ledger.write_text(json.dumps(ledger, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[验收通过] 线上网络回读与哈希对账 100% PASS！")

if __name__ == "__main__":
    main()
