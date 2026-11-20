"""Cloud Run 上の Kev サービスの疎通および認証検証スクリプト"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request


def test_endpoint(base_url: str, api_key: str):
    """Kev サービスのエンドポイントに対して認証と推論をテストする"""
    endpoint = f"{base_url.rstrip('/')}/v1/systemone"
    print(f"Testing endpoint: {endpoint}")

    test_payload = {
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
    data = json.dumps(test_payload).encode("utf-8")

    # 1. 未認証アクセスのテスト（拒絶されることの確認）
    print("\n[Test 1] 未認証リクエストのテスト（401 Unauthorized が期待値）...")
    req_no_auth = urllib.request.Request(
        endpoint, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(req_no_auth)
        print("[FAIL] 未認証リクエストが通ってしまいました。")
        return False
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print("[PASS] 期待通り 401 Unauthorized で拒否されました。")
        else:
            print(f"[WARN] 想定外のHTTPステータス: {e.code}")

    # 2. 正常な API キーでのリクエストテスト
    print(f"\n[Test 2] 正しい API キーでの推論リクエスト送信...")
    req_auth = urllib.request.Request(
        endpoint,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )

    t0 = time.time()
    try:
        with urllib.request.urlopen(req_auth, timeout=300) as resp:
            elapsed = time.time() - t0
            body = json.loads(resp.read().decode("utf-8"))
            print(f"[PASS] 成功! HTTP Status: {resp.status} (応答時間: {elapsed:.2f}s)")
            print("\n--- レスポンス内容 ---")
            print(json.dumps(body, indent=2, ensure_ascii=False))
            return True
    except urllib.error.HTTPError as e:
        print(f"[FAIL] HTTP エラー: {e.code} - {e.read().decode('utf-8')}")
        return False
    except Exception as e:
        print(f"[FAIL] リクエスト失敗: {e}")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="Cloud Run サービスのベース URL")
    parser.add_argument(
        "--api-key", default=os.environ.get("KEV_API_KEY", ""), help="認証用 API キー"
    )
    args = parser.parse_args()

    success = test_endpoint(args.url, args.api_key)
    sys.exit(0 if success else 1)
