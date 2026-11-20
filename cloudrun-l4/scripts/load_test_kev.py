"""Kev Cloud Run サービス負荷試験スクリプト

並行度（Concurrency）を段階的に変更しながら、スループットとレイテンシを計測します。
"""

import argparse
import json
import os
import statistics
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

PAYLOAD = {
    "state": "注文したスニーカーのサイズが違っており、カード明細にも二重請求があります。急ぎ対応をお願いします。",
    "model": "kev-latest",
    "questions": {
        "department": {
            "type": "choice",
            "instructions": "どの部門が対応すべきですか？",
            "criteria": {
                "returns": "交換、返品、誤配送、破損",
                "shipping": "配送状況、遅延、未着",
                "billing": "二重請求、請求書、支払いトラブル",
            },
        },
        "urgent": {
            "type": "noul",
            "instructions": "緊急の人手対応が必要ですか？",
        },
        "frustration": {
            "type": "score",
            "instructions": "顧客のフラストレーション度はどの程度ですか？",
            "criteria": ["冷静", "不満", "非常に怒っている"],
        },
    },
}



def send_single_request(url: str, api_key: str, payload_bytes: bytes) -> dict:
    req = urllib.request.Request(
        f"{url.rstrip('/')}/v1/systemone",
        data=payload_bytes,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Kev-LoadTester/1.0"
        },
        method="POST"
    )
    start_time = time.perf_counter()
    status = 0
    server_latency = None
    error_msg = None

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            status = resp.status
            body = resp.read().decode("utf-8")
            elapsed = time.perf_counter() - start_time
            try:
                res_json = json.loads(body)
                server_latency = res_json.get("latency_ms")
            except Exception:
                pass
            return {
                "status": status,
                "elapsed": elapsed,
                "server_latency": server_latency,
                "error": None
            }
    except urllib.error.HTTPError as e:
        elapsed = time.perf_counter() - start_time
        return {
            "status": e.code,
            "elapsed": elapsed,
            "server_latency": None,
            "error": str(e)
        }
    except Exception as e:
        elapsed = time.perf_counter() - start_time
        return {
            "status": -1,
            "elapsed": elapsed,
            "server_latency": None,
            "error": str(e)
        }


def run_benchmark_stage(url: str, api_key: str, concurrency: int, total_requests: int):
    payload_bytes = json.dumps(PAYLOAD).encode("utf-8")
    
    print(f"\n--- Testing Concurrency = {concurrency} (Total Requests: {total_requests}) ---")
    start_all = time.perf_counter()

    results = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(send_single_request, url, api_key, payload_bytes) for _ in range(total_requests)]
        for f in as_completed(futures):
            results.append(f.result())

    total_time = time.perf_counter() - start_all

    successes = [r for r in results if r["status"] == 200]
    errors = [r for r in results if r["status"] != 200]

    latencies = [r["elapsed"] * 1000 for r in successes]  # ms
    server_latencies = [r["server_latency"] for r in successes if r["server_latency"] is not None]

    rps = len(results) / total_time if total_time > 0 else 0

    print(f"  Completed: {len(results)} requests in {total_time:.2f}s ({rps:.2f} req/s)")
    print(f"  Success: {len(successes)}, Errors: {len(errors)}")

    if errors:
        error_types = {}
        for e in errors:
            key = f"Status {e['status']} ({e['error']})"
            error_types[key] = error_types.get(key, 0) + 1
        for k, v in error_types.items():
            print(f"    - {k}: {v}")

    if latencies:
        latencies.sort()
        p50 = latencies[int(len(latencies) * 0.50)]
        p90 = latencies[int(len(latencies) * 0.90)]
        p95 = latencies[int(len(latencies) * 0.95)]
        mean_lat = statistics.mean(latencies)
        min_lat = min(latencies)
        max_lat = max(latencies)

        print(f"  Client Latency (ms): Min={min_lat:.1f}, Avg={mean_lat:.1f}, P50={p50:.1f}, P90={p90:.1f}, P95={p95:.1f}, Max={max_lat:.1f}")
        
        if server_latencies:
            server_latencies.sort()
            s_p50 = server_latencies[int(len(server_latencies) * 0.50)]
            s_avg = statistics.mean(server_latencies)
            print(f"  Server Latency (ms): Min={min(server_latencies):.1f}, Avg={s_avg:.1f}, P50={s_p50:.1f}, Max={max(server_latencies):.1f}")

    return {
        "concurrency": concurrency,
        "total_requests": total_requests,
        "total_time": total_time,
        "rps": rps,
        "success_count": len(successes),
        "error_count": len(errors),
        "avg_latency": statistics.mean(latencies) if latencies else None,
        "p50_latency": p50 if latencies else None,
        "p95_latency": p95 if latencies else None,
        "max_latency": max_lat if latencies else None,
    }


def wait_until_ready(url: str, timeout: int = 600):
    print(f"Checking service status at {url}/health ...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            req = urllib.request.Request(f"{url.rstrip('/')}/health")
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    if data.get("status") == "ready":
                        print(f"Service is READY! (Waited {time.time() - start:.1f}s)")
                        return True
                    else:
                        print(f"  Service status: {data.get('status')}... waiting 10s")
        except Exception as e:
            print(f"  Service not responding yet ({e})... waiting 10s")
        time.sleep(10)
    print("Timed out waiting for service to be ready.")
    return False


def main():
    parser = argparse.ArgumentParser(description="Kev Service Load Test Tool")
    parser.add_argument("--url", required=True, help="Cloud Run service URL")
    parser.add_argument("--api-key", default=os.environ.get("KEV_API_KEY", ""), help="Kev API key")
    parser.add_argument("--concurrencies", default="1,4,8,16", help="Comma separated concurrency levels")
    parser.add_argument("--requests-per-stage", type=int, default=32, help="Requests to send per concurrency level")
    args = parser.parse_args()

    concurrency_list = [int(c.strip()) for c in args.concurrencies.split(",") if c.strip()]

    print(f"=== Starting Load Test for {args.url} ===")
    if not wait_until_ready(args.url):
        return

    print("\nWarming up with 1 request...")
    warmup_res = send_single_request(args.url, args.api_key, json.dumps(PAYLOAD).encode("utf-8"))
    if warmup_res["status"] != 200:
        print(f"Warmup failed with status {warmup_res['status']}: {warmup_res['error']}")
        print("Please check endpoint and API key.")
        return
    print(f"Warmup successful! Latency: {warmup_res['elapsed']*1000:.1f}ms (Server: {warmup_res['server_latency']}ms)")


    all_stats = []
    for c in concurrency_list:
        stats = run_benchmark_stage(args.url, args.api_key, c, args.requests_per_stage)
        all_stats.append(stats)
        time.sleep(2)  # 各ステージ間のクールダウン

    print("\n============================== SUMMARY ==============================")
    print(f"{'Concurrency':<12} | {'Reqs':<6} | {'RPS':<8} | {'Success':<8} | {'Avg (ms)':<10} | {'P50 (ms)':<10} | {'P95 (ms)':<10}")
    print("-" * 75)
    for s in all_stats:
        print(f"{s['concurrency']:<12} | {s['total_requests']:<6} | {s['rps']:<8.2f} | {s['success_count']}/{s['total_requests']:<6} | {s['avg_latency']:<10.1f} | {s['p50_latency']:<10.1f} | {s['p95_latency']:<10.1f}")
    print("=====================================================================")


if __name__ == "__main__":
    main()
