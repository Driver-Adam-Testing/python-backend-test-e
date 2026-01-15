#!/usr/bin/env python3
"""
Test script for running the inspector locally.

Usage:
    cd backend

    # Upload and trigger connection workflow:
    poetry run python scripts/test_inspector_local.py ~/Downloads/code.zip --trigger

    # Delete existing codebase (by folder name in zip) before re-uploading:
    poetry run python scripts/test_inspector_local.py ~/Downloads/code.zip --trigger --delete

    # Trigger inspector for existing version:
    poetry run python scripts/test_inspector_local.py --inspector <version-id>

Prerequisites:
    docker compose -f docker-compose.yml -f docker-compose.hatchet.yml up -d
"""

import argparse
import hashlib
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# Override Docker hostnames with localhost for host-side script
if "minio:" in os.environ.get("AWS_S3_ENDPOINT_URL", ""):
    os.environ["AWS_S3_ENDPOINT_URL"] = "http://localhost:9000"
if "hatchet-engine:" in os.environ.get("HATCHET_CLIENT_HOST_PORT", ""):
    os.environ["HATCHET_CLIENT_HOST_PORT"] = "localhost:7077"
os.environ.setdefault("AWS_S3_ENDPOINT_URL", "http://localhost:9000")
os.environ.setdefault("HATCHET_CLIENT_HOST_PORT", "localhost:7077")

import boto3
from database.db import engine
from sqlmodel import Session, select


def org_id_to_hash(org_id: str) -> str:
    return hashlib.sha256(org_id.encode()).hexdigest()[:63]


def get_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["AWS_S3_ENDPOINT_URL"],
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "minioadmin"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "minioadmin"),
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )


def get_or_create_test_org(session: Session) -> str:
    from database.models import Organization

    result = session.execute(
        select(Organization).where(Organization.name == "Local Test Org")
    )
    org = result.scalar_one_or_none()
    if org:
        return org.id

    org_id = f"org_test_{uuid.uuid4().hex[:8]}"
    org = Organization(id=org_id, name="Local Test Org")
    session.add(org)
    session.commit()
    print(f"Created test org: {org.id}")
    return org.id


def delete_codebase_by_name(session: Session, name: str, org_id: str) -> bool:
    from database.models import PrimaryAsset

    result = session.execute(
        select(PrimaryAsset).where(
            PrimaryAsset.display_name == name,
            PrimaryAsset.organization_id == org_id,
        )
    )
    assets = result.scalars().all()
    if not assets:
        return False

    for asset in assets:
        print(f"Deleting existing codebase: {asset.display_name} ({asset.id})")
        session.delete(asset)
    session.commit()
    return True


def create_test_records(
    session: Session, org_id: str, codebase_name: str
) -> tuple[str, str]:
    from database.models import PrimaryAsset, Version
    from database.models_enums import (
        PrimaryAssetKind,
        PrimaryAssetProvider,
        VersionStatus,
    )

    primary_asset_id = uuid.uuid4()
    version_id = uuid.uuid4()

    asset = PrimaryAsset(
        id=primary_asset_id,
        display_name=codebase_name,
        kind=PrimaryAssetKind.CODEBASE,
        organization_id=org_id,
        provider=PrimaryAssetProvider.USER,
    )
    session.add(asset)

    version = Version(
        id=version_id,
        primary_asset_id=primary_asset_id,
        status=VersionStatus.CONNECTING,
    )
    session.add(version)
    session.commit()

    print(f"Created PrimaryAsset: {primary_asset_id}")
    print(f"Created Version: {version_id}")
    return str(primary_asset_id), str(version_id)


def upload_zip(
    s3_client, zip_path: Path, org_id: str, primary_asset_id: str, version_id: str
):
    bucket_name = org_id_to_hash(org_id)

    try:
        s3_client.head_bucket(Bucket=bucket_name)
    except s3_client.exceptions.ClientError:
        s3_client.create_bucket(Bucket=bucket_name)
        print(f"Created bucket: {bucket_name}")

    s3_key = f"{primary_asset_id}/{version_id}/{version_id}_source.zip"
    print(f"Uploading to s3://{bucket_name}/{s3_key}")
    s3_client.upload_file(
        str(zip_path), bucket_name, s3_key, ExtraArgs={"ContentType": "application/zip"}
    )
    print("Upload complete")


def generate_presigned_url(org_id: str, primary_asset_id: str, version_id: str) -> str:
    # Use minio:9000 (Docker internal) so workers can access it
    worker_s3_client = boto3.client(
        "s3",
        endpoint_url="http://minio:9000",
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "minioadmin"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "minioadmin"),
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )
    bucket_name = org_id_to_hash(org_id)
    s3_key = f"{primary_asset_id}/{version_id}/{version_id}_source.zip"
    return worker_s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket_name, "Key": s3_key},
        ExpiresIn=3600,
    )


def trigger_connection(
    presigned_url: str, codebase_name: str, org_id: str, version_id: str
):
    from hatchet_sdk import Hatchet
    from shared.interfaces.hatchet_interfaces import RunCodebaseConnectionInput

    hatchet = Hatchet()
    task = hatchet.stubs.task(
        name="run-codebase-connection-workflow",
        input_validator=RunCodebaseConnectionInput,
    )
    result = task.run_no_wait(
        RunCodebaseConnectionInput(
            presigned_url=presigned_url,
            provisional_codebase_name=codebase_name,
            org_id=org_id,
            version_id=version_id,
            provider="manual",
        )
    )
    print(f"Connection workflow triggered: {result.workflow_run_id}")


def trigger_inspector(version_id: str):
    from hatchet_sdk import Hatchet
    from shared.interfaces.hatchet_interfaces import InspectorInput

    hatchet = Hatchet()
    task = hatchet.stubs.task(name="inspector-workflow", input_validator=InspectorInput)
    result = task.run_no_wait(InspectorInput(version_id=version_id))
    print(f"Inspector workflow triggered: {result.workflow_run_id}")


def get_zip_folder_name(zip_path: Path) -> str:
    import zipfile

    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        if names:
            return names[0].split("/")[0]
    return "unknown"


def main():
    parser = argparse.ArgumentParser(description="Test inspector locally")
    parser.add_argument(
        "zip_path", type=Path, nargs="?", help="Path to codebase zip file"
    )
    parser.add_argument(
        "--trigger",
        action="store_true",
        help="Trigger connection workflow after upload",
    )
    parser.add_argument(
        "--delete", action="store_true", help="Delete existing codebase before upload"
    )
    parser.add_argument(
        "--inspector",
        metavar="VERSION_ID",
        help="Trigger inspector for existing version",
    )

    args = parser.parse_args()

    # Inspector-only mode
    if args.inspector:
        print(f"Triggering inspector for version: {args.inspector}")
        trigger_inspector(args.inspector)
        return

    # Validate zip path
    if not args.zip_path:
        parser.print_help()
        sys.exit(1)

    args.zip_path = args.zip_path.expanduser()
    if not args.zip_path.exists():
        print(f"Error: File not found: {args.zip_path}")
        sys.exit(1)

    # Get codebase name from zip
    codebase_name = get_zip_folder_name(args.zip_path)
    print(f"Codebase name (from zip): {codebase_name}")

    # Test MinIO connection
    s3_client = get_s3_client()
    try:
        s3_client.list_buckets()
        print("Connected to MinIO")
    except Exception as e:
        print(f"Failed to connect to MinIO: {e}")
        print(
            "Run: docker compose -f docker-compose.yml -f docker-compose.hatchet.yml up -d"
        )
        sys.exit(1)

    with Session(engine) as session:
        org_id = get_or_create_test_org(session)

        # Delete existing if requested
        if args.delete:
            delete_codebase_by_name(session, codebase_name, org_id)

        # Create records and upload
        primary_asset_id, version_id = create_test_records(
            session, org_id, codebase_name
        )
        upload_zip(s3_client, args.zip_path, org_id, primary_asset_id, version_id)

    # Summary
    print(f"\nVersion ID: {version_id}")
    print("Monitor: http://localhost:8080")

    # Trigger if requested
    if args.trigger:
        presigned_url = generate_presigned_url(org_id, primary_asset_id, version_id)
        trigger_connection(presigned_url, codebase_name, org_id, version_id)
        print("\nAfter connection completes, run inspector:")
        print(
            f"  poetry run python scripts/test_inspector_local.py --inspector {version_id}"
        )
    else:
        print("\nTo trigger connection workflow:")
        print(
            f"  poetry run python scripts/test_inspector_local.py {args.zip_path} --trigger"
        )


if __name__ == "__main__":
    main()
