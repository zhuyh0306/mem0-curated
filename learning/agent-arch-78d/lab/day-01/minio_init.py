"""Create the Langfuse event bucket before the web app starts.

Runs inside the LiteLLM image because boto3 is already there; retries while MinIO
finishes booting, and treats "already owned" as success so `up` stays idempotent.
"""
import os
import time

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError


def client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("MINIO_ENDPOINT", "http://minio:9000"),
        aws_access_key_id=os.environ["MINIO_ROOT_USER"],
        aws_secret_access_key=os.environ["MINIO_ROOT_PASSWORD"],
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}),
    )


def main():
    bucket = os.environ.get("LANGFUSE_S3_EVENT_UPLOAD_BUCKET", "langfuse")
    for attempt in range(60):
        try:
            client().create_bucket(Bucket=bucket)
            print(f"bucket {bucket} created")
            return
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                print(f"bucket {bucket} already present")
                return
            print(f"attempt {attempt + 1}: {code or exc}")
        except (BotoCoreError, KeyError) as exc:
            print(f"attempt {attempt + 1}: {exc}")
        time.sleep(2)
    raise SystemExit("minio bucket was not ready in time")


if __name__ == "__main__":
    main()
