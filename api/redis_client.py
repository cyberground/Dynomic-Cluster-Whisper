"""
Redis-Client fuer den Whisper-Cluster.
Verwendet Redis Lists als Job-Queue (RPUSH/BLPOP) statt Hash-Scanning.
"""
import os
import redis

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

client = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    db=0,
    decode_responses=True,
)

# Queue-Keys
QUEUE_KEY = "whisper:queue"         # Redis List — Jobs warten hier
JOB_PREFIX = "whisper:job:"         # Hash pro Job (status, path, result, ...)
