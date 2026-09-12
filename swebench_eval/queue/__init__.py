"""Queue and artifact-storage client module.

Thin wrappers around boto3 SQS and S3, pointed at the local ElasticMQ
and MinIO services in Docker Compose.  Phase 5 swaps the endpoint URL
to real AWS — no code change.
"""

from swebench_eval.queue.client import (
    change_message_visibility,
    delete_message,
    get_queue_url,
    get_s3_client,
    get_sqs_client,
    receive_message,
    send_message,
    upload_artifact,
)

__all__ = [
    "change_message_visibility",
    "delete_message",
    "get_queue_url",
    "get_s3_client",
    "get_sqs_client",
    "receive_message",
    "send_message",
    "upload_artifact",
]
