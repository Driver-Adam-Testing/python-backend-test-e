import modal

app = modal.App("autodocs")


def has_guard_duty_tag(bucket: str, key: str) -> bool:
    """
    Check if the S3 object has the 'GuardDutyMalwareScanStatus' tag with value 'NO_THREATS_FOUND' or 'UNSUPPORTED'.
    """
    import os

    import boto3

    s3_client = boto3.client(
        "s3",
        region_name=os.environ["AWS_REGION"],
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
        endpoint_url=os.environ.get("AWS_S3_ENDPOINT_URL"),
    )
    tags = s3_client.get_object_tagging(Bucket=bucket, Key=key)
    """
    supported_tags = ["NO_THREATS_FOUND", "UNSUPPORTED"]
    the 'UNSUPPORTED' tag is a misnomer because GuardDuty tags file as UNSUPPORTED
    if they have too many files ( > 1000) or file is too large but we can still process it.
    """
    supported_tags = ["NO_THREATS_FOUND", "UNSUPPORTED"]
    return (
        len(
            [
                tag
                for tag in tags["TagSet"]
                if tag["Key"] == "GuardDutyMalwareScanStatus"
                and tag["Value"] in supported_tags
            ]
        )
        == 1
    )


def wait_for_guard_duty_tag(
    bucket: str, key: str, timeout: int = 60, interval: int = 5
) -> bool:
    """
    Polls the S3 object for the 'GuardDutyMalwareScanStatus' tag with value 'NO_THREATS_FOUND' or 'UNSUPPORTED'.
    until the tag is found or the timeout is reached.
    """
    import time

    start_time = time.time()
    print(
        f"Starting to poll for 'NO_THREATS_FOUND' or 'UNSUPPORTED' tag on object '{key}' in bucket '{bucket}'."
    )
    print(f"Timeout set to {timeout} seconds, checking every {interval} seconds.")

    while (time.time() - start_time) < timeout:
        if has_guard_duty_tag(bucket, key):
            print(
                f"Tag 'NO_THREATS_FOUND' or 'UNSUPPORTED' found for object '{key}' in bucket '{bucket}'."
            )
            return True
        print(f"Tag not found yet. Waiting {interval} seconds before retrying...")
        time.sleep(interval)
    print(
        f"Timeout reached. Tag 'NO_THREATS_FOUND' or 'UNSUPPORTED' not found for object '{key}' in bucket '{bucket}'."
    )
    return False
