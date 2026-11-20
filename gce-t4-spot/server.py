"""Production API server for Kev-4B on Google Compute Engine (GCE) N1 + Tesla T4 Spot VM.

Features:
- Specifically tuned for Tesla T4 (Turing, sm_75): FP16 precision, LoRA merged in fp32.
- GCE Spot VM preemption listener: polls metadata server, marks /ready as 503 to drain
  load balancer traffic, and finishes in-flight requests cleanly within the 30s grace period.
- High-concurrency async endpoint with state-prefix cache for fast multi-question inference.
- Endpoints:
  - POST /v1/systemone (TypeSafe System One API: Choice, Noul, Score)
  - GET  /v1/models (TypeSafe model metadata & Kev operational stats)
  - GET  /v1/status (GPU VRAM, latency, cache hit rate, Spot VM status)
  - GET  /healthz (Liveness probe: returns 200 if process is alive)
  - GET  /ready (Readiness probe: returns 200 if model loaded and not preempted)
"""

import argparse
import functools
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
import uvicorn

import torch
from fastapi import HTTPException

import kev.checkpoint as kev_checkpoint
from kev.api import SystemOneRequest
from kev.checkpoint import Checkpoint, LoadOptions
from kev.serve import Server, app as base_app

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("kev.gce")

METADATA_PREEMPTED_URL = "http://metadata.google.internal/computeMetadata/v1/instance/preempted"
METADATA_HEADERS = {"Metadata-Flavor": "Google"}


class SpotPreemptionMonitor(threading.Thread):
    """Monitors GCE metadata server for Spot VM preemption notice.
    
    When Google initiates preemption, the instance has 30 seconds before being shut down.
    This monitor detects the notice within 2-3 seconds, sets app.state.is_preempted = True,
    causing /ready to return 503 immediately so Google Cloud Load Balancer stops routing new traffic.
    """

    def __init__(self, check_interval_sec: float = 3.0):
        super().__init__(name="spot-preemption-monitor", daemon=True)
        self.check_interval = check_interval_sec
        self.running = True
        self.preempted = False

    def run(self):
        logger.info("Spot VM preemption monitor started.")
        while self.running and not self.preempted:
            if self._check_preempted():
                self.preempted = True
                base_app.state.is_preempted = True
                logger.warning(
                    "*** [GCE SPOT] PREEMPTION NOTICE DETECTED! ***\n"
                    "GCE will terminate this instance in ~30 seconds.\n"
                    "Marking /ready as 503 Service Unavailable to drain traffic from load balancer..."
                )
                self._drain_server()
                break
            time.sleep(self.check_interval)

    def _check_preempted(self) -> bool:
        try:
            req = urllib.request.Request(METADATA_PREEMPTED_URL, headers=METADATA_HEADERS)
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                if resp.status == 200:
                    text = resp.read().decode("utf-8").strip().upper()
                    return text == "TRUE"
        except (urllib.error.URLError, TimeoutError, OSError):
            # Not on GCE or metadata server temporarily unreachable; normal during local testing
            pass
        return False

    def _drain_server(self):
        s: Server = getattr(base_app.state, "server", None)
        if s is not None:
            logger.info("Waiting for queued and in-flight requests to finish...")
            try:
                s.wait_idle()
                logger.info("All in-flight requests finished successfully.")
            except Exception as e:
                logger.warning(f"Error while waiting for idle: {e}")

    def stop(self):
        self.running = False


class IdleShutdownMonitor(threading.Thread):
    """最後の推論呼び出しから idle_sec 秒経ったら VM を停止する。

    停止すると GPU 課金も止まる。kev ユーザーは poweroff できないため、
    startup.sh が入れた sudoers 設定(systemctl poweroff のみ許可)経由で実行する。
    """

    def __init__(self, idle_sec: float, check_interval_sec: float = 60.0):
        super().__init__(name="idle-shutdown-monitor", daemon=True)
        self.idle_sec = idle_sec
        self.check_interval = check_interval_sec

    def run(self):
        logger.info(f"Idle shutdown monitor started (idle limit: {self.idle_sec:.0f} s).")
        while True:
            time.sleep(self.check_interval)
            idle = time.monotonic() - base_app.state.last_call
            if idle < self.idle_sec:
                continue
            logger.warning(f"No inference calls for {idle:.0f} s. Powering off the VM...")
            # 失敗(sudoers 未設定など)しても次の周期で再試行する
            result = subprocess.run(["sudo", "-n", "/usr/bin/systemctl", "poweroff"], capture_output=True, text=True)
            if result.returncode != 0:
                logger.error(f"poweroff failed: {result.stderr.strip()}")


# 最後の推論呼び出し時刻。/ready や /v1/status のポーリングでは更新しない(更新するとアイドル停止しなくなる)。
# 認証失敗など 4xx/5xx の呼び出しでも更新しない。
base_app.state.last_call = time.monotonic()


@base_app.middleware("http")
async def track_last_call(request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/v1/systemone") and resp.status_code < 400:
        base_app.state.last_call = time.monotonic()
    return resp


# upstream の kev.serve には無い、ヘルスチェック用エンドポイント
@base_app.get("/healthz")
def healthz():
    """Liveness probe: 200 OK if the web server process is responsive."""
    return {"status": "ok"}


@base_app.get("/ready")
def ready():
    """Readiness probe: 200 OK if the model is loaded and not preempted."""
    s: Server = getattr(base_app.state, "server", None)
    if s is None or s.stopping.is_set():
        raise HTTPException(503, "model not ready or server stopping")
    if getattr(base_app.state, "is_preempted", False):
        raise HTTPException(503, "server preempted")
    return {"status": "ready", "model": s.checkpoint.requested}


# Extend base_app with operational and telemetry endpoints
@base_app.get("/v1/status")
def status():
    """Operational telemetry: GPU VRAM, queue length, cache stats, and Spot VM preemption state."""
    s: Server = getattr(base_app.state, "server", None)
    if s is None:
        raise HTTPException(503, "Server is not initialized yet")

    vram_stats = {}
    if torch.cuda.is_available():
        dev_idx = torch.cuda.current_device()
        props = torch.cuda.get_device_properties(dev_idx)
        allocated_gb = torch.cuda.memory_allocated(dev_idx) / (1024**3)
        reserved_gb = torch.cuda.memory_reserved(dev_idx) / (1024**3)
        total_gb = props.total_memory / (1024**3)
        vram_stats = {
            "device_name": props.name,
            "allocated_gb": round(allocated_gb, 2),
            "reserved_gb": round(reserved_gb, 2),
            "total_gb": round(total_gb, 2),
            "utilization_pct": round((allocated_gb / total_gb) * 100, 1),
        }

    pc = s.prefix_cache
    total_cache_ops = pc.hits + pc.misses
    hit_rate = (pc.hits / total_cache_ops) if total_cache_ops > 0 else 0.0

    return {
        "status": "preempted" if getattr(base_app.state, "is_preempted", False) else "healthy",
        "model": s.checkpoint.requested,
        "dtype": str(s.model.dtype),
        "device": s.device,
        "gpu_vram": vram_stats,
        "spot_vm": {
            "is_preempted": getattr(base_app.state, "is_preempted", False),
        },
        "queue": {
            "pending_requests": s.queue.qsize(),
            "total_batches_processed": s.batches,
            "total_requests_processed": s.batched_requests,
        },
        "prefix_cache": {
            "cached_states": len(pc.entries),
            "max_states": pc.size,
            "hits": pc.hits,
            "misses": pc.misses,
            "hit_rate_pct": round(hit_rate * 100, 1),
            "oom_retries": pc.oom_retries,
        },
    }


def warmup_model(s: Server):
    """Executes a small sample inference to warm up PyTorch and CUDA allocations."""
    logger.info("Warming up model on GPU...")
    start_t = time.perf_counter()
    sample_req = SystemOneRequest(
        state="System healthcheck warmup message.",
        model="kev-latest",
        questions={
            "warmup": {
                "type": "noul",
                "instructions": "Is this a test?"
            }
        }
    )
    resp = s.answer(sample_req)
    dur_ms = (time.perf_counter() - start_t) * 1000
    logger.info(f"Model warm-up completed in {dur_ms:.1f} ms. Latency reported: {resp['latency_ms']} ms.")


def main():
    parser = argparse.ArgumentParser(description="Kev-4B Production API Server (GCE T4 Spot VM)")
    parser.add_argument("--run", default="jaredpalmer/kev-4b", help="Model checkpoint to load (default: jaredpalmer/kev-4b)")
    parser.add_argument("--host", default="0.0.0.0", help="Network host interface to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8008, help="Port to listen on (default: 8008)")
    parser.add_argument("--device", default="cuda", help="Inference device (default: cuda)")
    parser.add_argument("--dtype", default="fp16", choices=["fp16", "fp32", "bf16"], help="Model precision (default: fp16 for T4)")
    parser.add_argument("--prefix_cache", type=int, default=8, help="State-prefix cache size (default: 8 states)")
    parser.add_argument("--cuda_graphs", type=int, default=0, help="Enable CUDA graphs (0 or 1, default: 0)")
    parser.add_argument("--no_preemption_monitor", action="store_true", help="Disable GCE Spot preemption polling")
    parser.add_argument("--idle_shutdown_min", type=float, default=0, help="最後の推論呼び出しからこの分数で VM を停止する (0 で無効、デフォルト: 0)")
    args = parser.parse_args()

    # Enforce environment settings for Tesla T4
    os.environ["KEV_DTYPE"] = args.dtype
    os.environ["KEV_CUDA_GRAPHS"] = str(args.cuda_graphs)
    os.environ["KEV_PREFIX_CACHE"] = str(args.prefix_cache)

    base_app.state.is_preempted = False

    logger.info(f"Initializing Kev-4B API Server for GCE N1 + Tesla T4...")
    logger.info(f"Model: {args.run} | Device: {args.device} | Dtype: {args.dtype} | Bind: {args.host}:{args.port}")

    # Verify CUDA availability on T4
    if args.device == "cuda" and not torch.cuda.is_available():
        logger.error("CUDA is not available! Please verify NVIDIA drivers and nvidia-smi.")
        sys.exit(1)

    if torch.cuda.is_available():
        gpu_name = torch.cuda.get_device_name(0)
        gpu_mem = torch.cuda.get_device_properties(0).total_memory / (1024**3)
        logger.info(f"Detected GPU: {gpu_name} ({gpu_mem:.2f} GB VRAM)")

    # Configure LoadOptions: FP16 and merge=True are critical for Tesla T4
    dtype_map = {"fp16": torch.float16, "fp32": torch.float32, "bf16": torch.bfloat16}
    opts = LoadOptions(
        dtype=dtype_map.get(args.dtype, torch.float16),
        merge=True,
        cuda_graphs=bool(args.cuda_graphs),
        backend="torch",
    )

    # 重みを bf16 から fp16 へ CPU で変換するとロードが約11秒遅い(transformers の変換スレッドは最大4本)。
    # direct_load で GPU へ直接読む。出力が現行経路とビット単位で一致することを T4 で確認済み(4 state x 3 問、差 0)。
    # ponytail: kev.checkpoint の DecisionModel 参照を差し替えている。upstream が呼び出しを変えたら壊れるので、
    # 起動時のログで確認すること。kev 側に公式オプションが入ったらそちらに移す。
    kev_checkpoint.DecisionModel = functools.partial(kev_checkpoint.DecisionModel, direct_load=True)

    t0 = time.time()
    logger.info(f"Loading checkpoint '{args.run}'...")
    ck = Checkpoint(args.run)
    tok, model = ck.load(args.device, opts)
    load_time = time.time() - t0
    logger.info(f"Model loaded successfully in {load_time:.2f} s. Memory allocated: {torch.cuda.memory_allocated() / (1024**3):.2f} GB")

    server_instance = Server(ck, tok, model, args.device)
    base_app.state.server = server_instance

    # Warm up model to populate PyTorch & CUDA caches
    warmup_model(server_instance)

    # Start GCE Spot VM preemption monitor
    if not args.no_preemption_monitor:
        SpotPreemptionMonitor(check_interval_sec=3.0).start()

    # SIGTERM/SIGINT は uvicorn が処理し、Server.close は kev.serve が atexit に登録済み

    # アイドル時間はウォームアップ完了(ready)の時点から数え始める
    base_app.state.last_call = time.monotonic()
    if args.idle_shutdown_min > 0:
        IdleShutdownMonitor(idle_sec=args.idle_shutdown_min * 60).start()

    logger.info(f"Server is ready! Listening at http://{args.host}:{args.port}")
    logger.info("Endpoints:")
    logger.info(f"  - Inference:   POST http://{args.host}:{args.port}/v1/systemone")
    logger.info(f"  - Models:      GET  http://{args.host}:{args.port}/v1/models")
    logger.info(f"  - Telemetry:   GET  http://{args.host}:{args.port}/v1/status")
    logger.info(f"  - Liveness:    GET  http://{args.host}:{args.port}/healthz")
    logger.info(f"  - Readiness:   GET  http://{args.host}:{args.port}/ready")

    uvicorn.run(base_app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
