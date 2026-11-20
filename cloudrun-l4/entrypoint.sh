#!/bin/bash
set -e

PORT="${PORT:-8080}"
MODEL="${KEV_MODEL:-jaredpalmer/kev-4b}"
PREFETCH_TO_RAM="${PREFETCH_TO_RAM:-0}"

echo "=========================================="
echo " Starting Kev Serving Container (v3-direct-control)"
echo " Model:           $MODEL"
echo " Port:            $PORT"
echo " Prefetch to RAM: $PREFETCH_TO_RAM"
echo " API Auth:        $([ -n "$KEV_API_KEY" ] && echo "ENABLED (Bearer token required)" || echo "DISABLED (Open)")"
echo "=========================================="

# /mnt/gcs がマウントされている場合
if [ -d "/mnt/gcs" ]; then
    rm -rf /models
    
    if [ "$PREFETCH_TO_RAM" = "1" ]; then
        echo "⚡ High-speed RAM prefetch enabled (copying to /dev/shm) ..."
        START_TIME=$(date +%s)
        mkdir -p /dev/shm/models
        ln -s /dev/shm/models /models
        
        if [[ "$MODEL" == *4b* ]]; then
            echo "Copying 4B models (kev-4b + qwen-4b-base) to /dev/shm ..."
            cp -r /mnt/gcs/kev-4b /dev/shm/models/
            cp -r /mnt/gcs/qwen-4b-base /dev/shm/models/
        elif [[ "$MODEL" == *9b* ]]; then
            echo "Copying 9B models to /dev/shm ..."
            cp -r /mnt/gcs/kev-9b /dev/shm/models/
            cp -r /mnt/gcs/qwen-9b-base /dev/shm/models/
        else
            echo "Copying full /mnt/gcs to /dev/shm ..."
            cp -r /mnt/gcs/* /dev/shm/models/
        fi
        
        END_TIME=$(date +%s)
        echo "⚡ Prefetch to RAM completed in $((END_TIME - START_TIME))s"
    else
        echo "⚡ Direct buffered-read stream enabled (Direct from GCS via VPC)"
        ln -s /mnt/gcs /models
    fi
fi

exec python -m kev.serve --run "$MODEL" --host 0.0.0.0 --port "$PORT"
