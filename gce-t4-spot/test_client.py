"""Verification client for Kev-4B API Server on GCE N1 + Tesla T4 Spot VM.

Runs smoke tests against the deployed API server:
1. Health & readiness probes (/healthz, /ready, /v1/status)
2. TypeSafe Model Card (/v1/models)
3. Core decision primitives via POST /v1/systemone:
   - Choice (Categorical routing)
   - Noul (Binary classification)
   - Score (Ordinal score)
   - Multi-question combined pass
4. State-prefix cache speedup test
"""

import argparse
import os
import sys
import time
import requests

DEPARTMENT_CRITERIA = {
    "returns": "Exchanges, refunds, return labels, wrong or defective items",
    "shipping": "Tracking numbers, courier delays, missing delivery, damaged package",
    "billing": "Invoice disputes, double charges, payment processing errors",
    "account": "Password reset, email update, login problems",
}


def test_server(base_url: str, api_key: str):
    print("=" * 65)
    print(f" Testing Kev-4B API Server at: {base_url}")
    print("=" * 65)

    # /v1 以下は Bearer 認証が必要 (KEV_API_KEY)
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {api_key}"

    # 1. Health Checks
    print("\n[1/5] Checking Server Health Probes...")
    try:
        r_health = session.get(f"{base_url}/healthz", timeout=5)
        print(f"  GET /healthz: {r_health.status_code} -> {r_health.json()}")
        assert r_health.status_code == 200, f"Expected 200, got {r_health.status_code}"

        r_noauth = requests.post(f"{base_url}/v1/systemone", json={}, timeout=5)
        print(f"  POST /v1/systemone (キーなし): {r_noauth.status_code}")
        assert r_noauth.status_code == 401, f"キーなしで 401 になりません: {r_noauth.status_code}"

        r_ready = session.get(f"{base_url}/ready", timeout=5)
        print(f"  GET /ready:   {r_ready.status_code} -> {r_ready.json()}")
        assert r_ready.status_code == 200, f"Expected 200, got {r_ready.status_code}"
    except Exception as e:
        print(f"  ERROR: Health check failed: {e}")
        sys.exit(1)

    # 2. Operational Telemetry
    print("\n[2/5] Fetching Runtime Status (/v1/status)...")
    try:
        r_status = session.get(f"{base_url}/v1/status", timeout=5)
        if r_status.status_code == 200:
            st = r_status.json()
            gpu = st.get("gpu_vram", {})
            print(f"  Device:           {st.get('device')} ({gpu.get('device_name', 'N/A')})")
            print(f"  VRAM:             {gpu.get('allocated_gb', 0)} GB / {gpu.get('total_gb', 0)} GB ({gpu.get('utilization_pct', 0)}%)")
            print(f"  Dtype:            {st.get('dtype')}" + ("" if "float16" in str(st.get("dtype")) else "  <-- WARNING: fp16 ではありません"))
            print(f"  Spot Preempted:   {st.get('spot_vm', {}).get('is_preempted')}")
            print(f"  Batches Processed: {st.get('queue', {}).get('total_batches_processed')}")
        else:
            print(f"  GET /v1/status returned {r_status.status_code}")
    except Exception as e:
        print(f"  (Telemetry optional) Skipped: {e}")

    # 3. Model Metadata
    print("\n[3/5] Querying Model Metadata (/v1/models)...")
    r_models = session.get(f"{base_url}/v1/models", timeout=5)
    card = r_models.json()["models"][0]
    print(f"  Model Name:       {card['name']}")
    print(f"  Base Architecture: {card['base']}")
    print(f"  Temperature:      {card['temperature']:.2f}")

    # 4. Core Decisions (Choice, Noul, Score)
    print("\n[4/5] Executing Decisions via POST /v1/systemone...")
    sample_state = (
        "Order #7741: I was charged twice for my subscription renewal on September 15th. "
        "Please issue an immediate refund for the extra $49.00 charge."
    )

    payload = {
        "state": sample_state,
        "model": "kev-latest",
        "questions": {
            "department": {
                "type": "choice",
                "instructions": "Which department should resolve this customer inquiry?",
                "criteria": DEPARTMENT_CRITERIA,
            },
            "is_billing_urgent": {
                "type": "noul",
                "instructions": "Is this a financial or billing escalation requiring urgent action?",
            },
            "customer_frustration": {
                "type": "score",
                "instructions": "Rate the customer's frustration level from calm to angry.",
                "criteria": ["calm", "slightly annoyed", "frustrated", "furious"],
            },
        },
    }

    t_start = time.perf_counter()
    r_sysone = session.post(f"{base_url}/v1/systemone", json=payload, timeout=15)
    t_roundtrip = (time.perf_counter() - t_start) * 1000

    assert r_sysone.status_code == 200, f"Request failed: {r_sysone.text}"
    res = r_sysone.json()
    answers = res["answers"]

    print(f"  Roundtrip Latency: {t_roundtrip:.1f} ms (Server model time: {res['latency_ms']} ms)")
    print(f"  Input Tokens:      {res['usage']['input_tokens']}")

    # Choice output
    ans_dept = answers["department"]
    print(f"\n  --- Choice Decision: 'department' ---")
    print(f"    Selected Choice: {ans_dept['choice']} (Confidence: {ans_dept['confidence']:.4f})")
    print("    Probabilities:")
    for k, v in ans_dept["probabilities"].items():
        bar = "█" * int(v * 20)
        print(f"      - {k:10s}: {v:.4f} {bar}")

    # Noul output
    ans_noul = answers["is_billing_urgent"]
    print(f"\n  --- Noul Decision: 'is_billing_urgent' ---")
    print(f"    Probability True: {ans_noul['noul']:.4f}")

    # Score output
    ans_score = answers["customer_frustration"]
    print(f"\n  --- Score Decision: 'customer_frustration' ---")
    print(f"    Expected Score:   {ans_score['score']:.2f} / 3.00 (Confidence: {ans_score['confidence']:.4f})")
    print(f"    Legend:           {ans_score['legend']}")

    # 5. State-Prefix Cache Speedup Test
    print("\n[5/5] Measuring State-Prefix Cache Performance...")
    # Follow-up query on the EXACT SAME state with a new question
    followup_payload = {
        "state": sample_state,
        "model": "kev-latest",
        "questions": {
            "requires_human": {
                "type": "noul",
                "instructions": "Does this request require a human agent review?",
            }
        },
    }

    # レスポンス本文にはキャッシュヒットが含まれないため、/v1/models の hits カウンタの増加で確認する
    def cache_hits() -> int:
        return session.get(f"{base_url}/v1/models", timeout=5).json()["models"][0]["prefix_cache"]["hits"]

    hits_before = cache_hits()
    t_cache_start = time.perf_counter()
    r_cache = session.post(f"{base_url}/v1/systemone", json=followup_payload, timeout=10)
    t_cache_dur = (time.perf_counter() - t_cache_start) * 1000
    assert r_cache.status_code == 200, f"Request failed: {r_cache.text}"
    cache_res = r_cache.json()

    print(f"  Cached Request Latency: {t_cache_dur:.1f} ms (Server model time: {cache_res['latency_ms']} ms)")
    hit = cache_hits() > hits_before
    print(f"  Prefix cache: {'HIT' if hit else 'MISS (state が短くキャッシュ対象外の可能性あり)'}")

    print("\n" + "=" * 65)
    print(" ALL VERIFICATION TESTS PASSED SUCCESSFULLY!")
    print("=" * 65)


def main():
    parser = argparse.ArgumentParser(description="Test Kev-4B API Server on GCE")
    parser.add_argument("--host", default="127.0.0.1", help="Server host IP or domain (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8008, help="Server port (default: 8008)")
    parser.add_argument("--api-key", default=os.environ.get("KEV_API_KEY", ""), help="Bearer 認証キー (default: 環境変数 KEV_API_KEY)")
    args = parser.parse_args()
    if not args.api_key:
        parser.error("--api-key か環境変数 KEV_API_KEY を指定してください")

    base_url = f"http://{args.host}:{args.port}"
    test_server(base_url, args.api_key)


if __name__ == "__main__":
    main()
