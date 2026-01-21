import hashlib
import os
import re
import shutil
import time
import zipfile
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import Enum
from functools import cache
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID

import requests
from boto3 import resource
from botocore.client import ClientError
from database.models import Version
from sqlmodel import Session

TOP_LANGUAGE_BLACKLIST = [
    "TOML",
    "YAML",
    "JSON",
    "XML",
    "Markdown",
    "Text",
    "HTML",
    "CSV",
    "reStructuredText",
    "AsciiDoc",
    "Haml",
    "INI",
    "Makefile",
]


class AccessTokenError(Exception): ...


class RunInProgressError(Exception):
    pass


def set_codebase_status(version_id: UUID, status: Enum) -> None:
    from database.db import engine

    with Session(engine) as session, session.begin():
        version = session.get(Version, version_id)
        version.status = status
        session.add(version)


@cache
def load_extension_and_name_mapping() -> dict:
    from collections import defaultdict

    import yaml

    with open("/packages/shared/shared/inspector/onboarding/languages.yml") as f:
        language_dict = yaml.safe_load(f)
    extension_map = defaultdict(list)
    name_map = defaultdict(list)
    for lang in language_dict:
        if language_dict[lang].get("extensions"):
            for ext in language_dict[lang]["extensions"]:
                extension_map[ext].append(lang)
        if language_dict[lang].get("filenames"):
            for name in language_dict[lang]["filenames"]:
                name_map[name].append(lang)
    return extension_map, name_map


def get_file_type_from_extension(extension: str) -> str | None:
    extension_map = load_extension_and_name_mapping()[0]
    file_type = extension_map.get(extension)

    if extension == ".h":
        return "Header"
    elif file_type and len(file_type) == 1:
        return file_type[0]
    return None


def get_file_type_from_filename(filename: str) -> str | None:
    name_map = load_extension_and_name_mapping()[1]
    file_type = name_map.get(filename)

    if file_type and len(file_type) == 1:
        return file_type[0]
    return None


def create_base_storage_url(org_id: str) -> str:
    hashed_org_id = hashlib.sha256(org_id.encode()).hexdigest()[:63]
    return f"https://{hashed_org_id}.s3.amazonaws.com"


def create_bucket_if_dne(bucket_name: str) -> None:
    try:
        # TODO: handle this cleanly? AWS_S3_ENDPOINT_URL returns None if DNE, which reverts to
        # default boto3 behavior
        s3_resource = resource("s3", endpoint_url=os.environ.get("AWS_S3_ENDPOINT_URL"))
        s3_resource.meta.client.head_bucket(Bucket=bucket_name)
    except ClientError:
        # Bucket does not exist
        s3_resource.create_bucket(Bucket=bucket_name)
        print(f"Created bucket: {bucket_name}")


def download_file_from_s3(
    bucket_name: str, org_id: str, file_name: str, download_destination: Path
) -> None:
    s3_file_prefix = "codebases"
    s3_resource = resource("s3", endpoint_url=os.environ.get("AWS_S3_ENDPOINT_URL"))
    # TODO: if S3 is reorged, bucket is consistent?
    s3_bucket = s3_resource.Bucket(bucket_name)
    s3_path_to_file = Path(s3_file_prefix) / org_id / file_name
    download_destination = Path(file_name)

    try:
        s3_bucket.download_file(str(s3_path_to_file), download_destination)
    except Exception as e:
        raise e


def download_file_from_presigned_url(
    presigned_url: str, download_destination: Path
) -> None:
    with requests.get(presigned_url, stream=True) as r:
        r.raise_for_status()
        with open(download_destination, "wb") as w_file:
            for chunk in r.iter_content(chunk_size=8192):
                w_file.write(chunk)


def get_root_nodes_in_archive(zip_file: zipfile.ZipFile) -> list[tuple[str, bool]]:
    return [
        (parts[0], info.is_dir())
        for info in zip_file.infolist()
        if (parts := Path(info.filename).parts)
        and parts[0] != "__MACOSX"
        and len(parts) == 1
    ]


def _wanted_members(zf: zipfile.ZipFile) -> list[str]:
    return [m for m in zf.namelist() if not m.startswith("__MACOSX")]


def unpack_archive(archive_path: Path, extraction_root: Path) -> Path:
    with zipfile.ZipFile(archive_path) as zf:
        root_nodes = get_root_nodes_in_archive(zf)

        single_root_dir = len(root_nodes) == 1 and root_nodes[0][1]
        if single_root_dir:
            target_dir = extraction_root / root_nodes[0][0]
            zf.extractall(path=extraction_root, members=_wanted_members(zf))
        else:
            target_dir = extraction_root / archive_path.stem
            target_dir.mkdir(exist_ok=False)
            zf.extractall(path=target_dir, members=_wanted_members(zf))

    return target_dir


def clean_extracted_codebase(
    codebase_root: Path,
    extra_filters: Iterable[str] | None = None,
) -> None:
    patterns: list[str] = ["**/__MACOSX/**", "**/.DS_Store"]
    if extra_filters:
        patterns.extend(extra_filters)

    for pattern in patterns:
        for path in codebase_root.glob(pattern):
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)


def finalize_codebase_path(
    extracted_path: Path,
    override_name: str | None = None,
) -> Path:
    if override_name:
        final_path = extracted_path.parent / override_name
        os.rename(extracted_path, final_path)
        return final_path
    return extracted_path


def unpack_archive_to_finalized_path(
    archive_path: Path,
    extraction_root: Path,
    override_codebase_name: str | None = None,
    extra_filters: Iterable[str] | None = None,
) -> Path:
    """
    Unpack *archive_path*, clean macOS junk (plus *extra_filters*), optionally
    rename the result, and return the final directory on disk.
    """
    extracted = unpack_archive(archive_path, extraction_root)
    clean_extracted_codebase(extracted, extra_filters=extra_filters)
    return finalize_codebase_path(extracted, override_name=override_codebase_name)


def upload_file_to_s3(
    bucket_name: str, destination_root: Path, local_path: Path
) -> Path:
    s3_resource = resource("s3", endpoint_url=os.environ.get("AWS_S3_ENDPOINT_URL"))
    # TODO: if S3 is reorged, bucket is consistent?
    s3_bucket = s3_resource.Bucket(bucket_name)
    if local_path.exists():
        s3_destination_path = destination_root / local_path
        s3_bucket.upload_file(local_path, str(s3_destination_path))
    return s3_destination_path


def evaluate_file_size_processable(filepath: Path) -> bool:
    is_proc = True
    file_size = os.path.getsize(filepath)
    min_size = 0
    max_size = 1000000000  # TODO: what's a more sensible default?

    if file_size < min_size or file_size > max_size:
        is_proc = False

    return is_proc


def get_non_ascii_file_encoding(file_bytes: bytes) -> str:
    import chardet

    chunk_size = 2500
    num_chunks = 40
    min_confidence = 0.7
    found_encoding = None

    byte_chunks = [
        file_bytes[i : i + chunk_size] for i in range(0, len(file_bytes), chunk_size)
    ][:num_chunks]
    for chunk in byte_chunks:
        pred_enc = chardet.detect(chunk)
        if (
            pred_enc["encoding"] is not None
            and pred_enc["encoding"] != "ascii"
            and pred_enc["confidence"] > min_confidence
        ):
            try:
                file_bytes.decode(pred_enc["encoding"])
                found_encoding = pred_enc["encoding"]
            except UnicodeDecodeError:
                print(f"Chardet predicted encoding {pred_enc['encoding']} failed")
            break
    return found_encoding


def evaluate_file_binary(filepath: Path) -> bool:
    import chardet

    is_binary = False
    chunk_size = 2500
    min_confidence = 0.7

    # Allow ASCII bytes for tab, new line, carriage return, form feed, vert tab and separators
    disallowed_bytes = [
        b"\x00",
        b"\x01",
        b"\x02",
        b"\x03",
        b"\x04",
        b"\x05",
        b"\x06",
        b"\x07",
        b"\x08",
        b"\x0e",
        b"\x0f",
        b"\x10",
        b"\x11",
        b"\x12",
        b"\x13",
        b"\x14",
        b"\x15",
        b"\x16",
        b"\x17",
        b"\x18",
        b"\x19",
        b"\x1a",
        b"\x1b",
    ]

    with open(filepath, "rb") as r_file:
        file_bytes = r_file.read()

        if any(test_byte in file_bytes for test_byte in disallowed_bytes):
            # If the file contains any of the control code bytes:
            # We have high confidence it is NOT one of the most used 8-bit encodings.
            # Still need to test for UTF-16 (possibly UTF-32 if we want to support)

            # This decodes random garbage as well, but helpful as a backup to chardet
            utf16_decodes = False
            try:
                file_bytes.decode("utf-16")  # Little-endian
                utf16_decodes = True
            except UnicodeDecodeError:
                try:
                    file_bytes.decode("utf-16be")  # Big-endian
                    utf16_decodes = True
                except UnicodeDecodeError:
                    pass

            if utf16_decodes:
                pred_enc = chardet.detect(file_bytes[:chunk_size])

                if (
                    pred_enc["encoding"] in ["UTF-16", "utf-16be"]
                    and pred_enc["confidence"] > min_confidence
                ):
                    is_binary = False
                else:
                    is_binary = True
            else:
                is_binary = True
        else:
            # Lack confidence still as NUL check isn't perfect
            # If file lacks ascii control code bytes, but decodes as utf-8 -> Confident
            utf8_decodes = False
            try:
                file_bytes.decode("utf-8")
                utf8_decodes = True
            except UnicodeDecodeError:
                pass

            if utf8_decodes:
                is_binary = False
            else:
                # Last effort - use chardet
                file_encoding = get_non_ascii_file_encoding(file_bytes)
                is_binary = file_encoding is None

    return is_binary


def evaluate_file_hex(filepath: Path) -> bool:
    hex_perc_threshold = 0.99
    regex_test_str = r"[a-fA-F0-9\n ]"
    is_hex = False
    with open(filepath) as r_file:
        file_str = r_file.read()

        if len(file_str) > 0:
            hex_char_count = len(re.findall(regex_test_str, file_str))
            if hex_char_count / len(file_str) > hex_perc_threshold:
                is_hex = True
    return is_hex


def is_on_blacklist(filepath: Path) -> bool:
    blacklist_dirs = [
        ".git",
        "driver_docs",
    ]
    blacklist_file_exts = [
        ".svg",
        ".hex",
        ".bin",
        ".BIN",
        ".dat",
        ".DAT",
        ".exe",
        ".o",
        ".a",
        ".so",
        ".dll",
        ".dylib",
        ".cdylib",
        ".axf",
        ".elf",
    ]
    blacklist_file_names = [
        ".DS_Store",
        ".driverignore",
    ]
    is_blacklisted = False

    if any(dir in filepath.parts for dir in blacklist_dirs):
        is_blacklisted = True

    if filepath.is_file() and filepath.suffix in blacklist_file_exts:
        is_blacklisted = True

    if filepath.is_file() and filepath.name in blacklist_file_names:
        is_blacklisted = True

    return is_blacklisted


def reencode_file(filepath: Path) -> None:
    is_utf8 = False
    decoded_str = None

    with open(filepath, "rb") as r_file:
        file_bytes = r_file.read()

        try:
            file_bytes.decode("utf-8")
            is_utf8 = True
        except UnicodeDecodeError:
            is_utf8 = False

        if not is_utf8:
            file_encoding = get_non_ascii_file_encoding(file_bytes)
            if file_encoding:
                try:
                    decoded_str = file_bytes.decode(file_encoding)
                except UnicodeDecodeError as e:
                    print(
                        f"Error: {e} decoding {filepath} \
                          with predicted encoding: {file_encoding}"
                    )
                    # TODO: force UTF-8 encoding with ignored characters?
            else:
                print(f"Chardet returned None for {filepath}")

    if decoded_str is not None:
        with open(filepath, "w", encoding="utf-8") as w_file:
            w_file.write(decoded_str)
        print(f"Updated {filepath} to UTF-8")


def process_and_upload_file(
    s3_client: any,
    org_hashed_id: str,
    primary_asset_id: str,
    version_id: str,
    local_path: Path,
    download_dir: Path,
    db_node_paths: set[str],
) -> Path | None:
    trimmed_path = local_path.relative_to(download_dir)
    if str(trimmed_path) in db_node_paths:
        reencode_file(local_path)
        s3_key = f"{primary_asset_id}/{version_id}/{trimmed_path}"
        s3_client.upload_file(local_path, org_hashed_id, s3_key)
        print(f"uploading {trimmed_path} to s3 at {s3_key}")
        return local_path
    return None


def process_and_upload_all_files_in_parallel(
    s3_client: any,
    org_hashed_id: str,
    primary_asset_id: str,
    version_id: str,
    extracted_path: Path,
    download_dir: Path,
    db_node_paths: set[str],
    max_workers: int = 8,
) -> list[Path]:
    file_paths: list[Path] = []
    files_to_process: list[Path] = []

    for root, _, files in os.walk(extracted_path):
        for filename in files:
            local_path = Path(root) / filename
            files_to_process.append(local_path)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        for local_path in files_to_process:
            futures.append(
                executor.submit(
                    process_and_upload_file,
                    s3_client,
                    org_hashed_id,
                    primary_asset_id,
                    version_id,
                    local_path,
                    download_dir,
                    db_node_paths,
                )
            )

        for future in as_completed(futures):
            result = future.result()
            if result is not None:
                file_paths.append(result)
    return file_paths


def analyze_text_file(filepath: Path) -> dict:
    is_hex = evaluate_file_hex(filepath)
    with open(filepath) as f:
        sloc = sum(1 for _ in f)
    return {
        "size": os.path.getsize(filepath),
        "sloc": sloc,
        "extension": filepath.suffix,
        "is_binary": False,
        "is_hex": is_hex,
    }


def analyze_binary_file(filepath: Path) -> dict:
    return {
        "size": os.path.getsize(filepath),
        "sloc": 0,
        "extension": filepath.suffix,
        "is_binary": True,
        "is_hex": False,
    }


def run_file_stats_and_reencode(
    local_path: Path,
    codebase_root: Path,
) -> dict:
    # Evaluate file-processability before reencoding
    # due to file encoding nastiness w/ binary files
    file_size_processable = evaluate_file_size_processable(local_path)
    is_binary = evaluate_file_binary(local_path)
    is_blacklisted = is_on_blacklist(local_path)

    file_stats = {}

    if not is_binary:
        reencode_file(local_path)
        # TODO use concrete type for the stats data structure since we need to mirror it in the API. Currently fraile to changes
        file_stats = analyze_text_file(local_path)

        default_process_state = False
        if not file_stats["is_hex"] and file_size_processable and not is_blacklisted:
            default_process_state = True
        file_stats["is_analyzable"] = default_process_state
    else:
        file_stats = analyze_binary_file(local_path)
        file_stats["is_analyzable"] = False
    file_stats["is_blacklisted"] = is_blacklisted
    file_stats["is_ignored"] = False  # if we make it here now, it is not ignored

    if file_stats["is_analyzable"]:
        file_type = get_file_type_from_extension(file_stats["extension"])
        if not file_type:
            file_type = get_file_type_from_filename(local_path.name)
        if not file_type:
            file_type = "Other"
        file_stats["language"] = file_type
    else:
        file_stats["language"] = "N/A"

    return file_stats


def generate_get_presigned_url(bucket: str, key: str, expires: int = 3600) -> str:
    import boto3

    s3_client = boto3.client(
        "s3",
        region_name=os.environ["AWS_REGION"],
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
    )
    return s3_client.generate_presigned_url(
        ClientMethod="get_object",
        Params={
            "Bucket": bucket,
            "Key": key,
        },
        ExpiresIn=expires,
    )


def parse_presigned_url(url: str) -> tuple[str, str]:
    from urllib.parse import unquote_plus

    parsed_url = urlparse(url)
    host = parsed_url.netloc
    path = parsed_url.path.lstrip("/")  # Remove leading slash

    # Extract bucket from the domain
    if ".s3." in host:  # AWS domain-style: bucket.s3.region.amazonaws.com
        bucket = host.split(".s3.")[0]
    elif (
        host.startswith(("s3-", "s3.")) or ":" in host or host in ("minio", "localhost")
    ):  # AWS path-style: s3.region.amazonaws.com/bucket
        bucket = path.split("/")[0]
        path = "/".join(path.split("/")[1:])
    else:
        raise ValueError("Invalid S3 URL format")
    key = unquote_plus(path)
    if not bucket or not key:
        raise ValueError("Invalid S3 URL: missing bucket or key")
    return bucket, key


def has_guard_duty_tag(bucket: str, key: str) -> bool:
    """
    Check if the S3 object has the 'GuardDutyMalwareScanStatus' tag with value 'NO_THREATS_FOUND' or 'UNSUPPORTED'.
    """
    import boto3

    s3_client = boto3.client(
        "s3",
        region_name=os.environ["AWS_REGION"],
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
    )
    tags = s3_client.get_object_tagging(Bucket=bucket, Key=key)
    """
    supported_tags = ["NO_THREATS_FOUND", "UNSUPPORTED"]
    the 'UNSUPPORTED' tag is a misnomer because GuardDuty tags file as UNSUPPORTED
    if they have too many files ( > 1000) or file is too large but we can still process it.
    """
    # TODO: add support for UNSUPPORTED tag in GuardDuty
    supported_tags = ["NO_THREATS_FOUND", "UNSUPPORTED"]
    # supported_tags = ["NO_THREATS_FOUND"]
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


def delete_file_from_s3(bucket: str, key: str) -> None:
    """
    Delete a file from S3.
    NOTE: this should be in shared but shared package does not have access to settings need to instantiate boto3 client
    """
    import boto3

    s3_client = boto3.client(
        "s3",
        region_name=os.environ["AWS_REGION"],
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
    )
    s3_client.delete_object(Bucket=bucket, Key=key)


def upload_to_s3_with_metadata(
    zip_content: bytes, metadata: dict, upload_key: str
) -> bool:
    import boto3

    s3_client = boto3.client("s3")
    try:
        s3_client.put_object(
            Bucket=os.environ["DROPZONE_BUCKET_NAME"],
            Key=upload_key,
            Body=zip_content,
            ContentType="application/zip",
            Metadata=metadata,
        )
    except Exception as e:
        print(e)
        raise Exception(
            f"Failed uploading codebase version {metadata['version_id']} to {upload_key}."
        ) from e


def calculate_directory_stats(
    all_directories: list[Path],
    codebase_stats: dict[Path, dict],
    temp_dir: Path,
) -> list[tuple[dict | None, str | None]]:
    from collections import defaultdict

    from shared.usage.utils import bytes_to_sloc

    # Pre-filter valid directories using list comprehension
    valid_dirs = {
        str(directory)
        for directory in all_directories
        if not is_on_blacklist(Path(directory))
    }

    def create_empty_stats() -> dict[str, int | defaultdict]:
        return {
            "analyzable_bytes": 0,
            "analyzable_files": 0,
            "total_bytes": 0,
            "total_files": 0,
            "analyzable_files_by_type": defaultdict(int),
            "analyzable_bytes_by_type": defaultdict(int),
            "analyzable_files_by_extension": defaultdict(int),
            "analyzable_bytes_by_extension": defaultdict(int),
        }

    dir_stats = {directory: create_empty_stats() for directory in valid_dirs}

    for file_path, file_stats in codebase_stats.items():
        file_parents = [
            str(parent)
            for parent in file_path.parents
            if parent != temp_dir and str(parent) in valid_dirs
        ]

        # Update stats for all parent directories
        for directory in file_parents:
            stats = dir_stats[directory]
            stats["total_bytes"] += file_stats["size"]
            stats["total_files"] += 1

            if (
                file_stats["is_analyzable"]
                and not file_stats["is_blacklisted"]
                and not file_stats.get("is_ignored", False)
            ):
                file_type = file_stats.get("language") or "Other"
                file_extension = file_path.suffix

                stats["analyzable_files_by_type"][file_type] += 1
                stats["analyzable_bytes_by_type"][file_type] += file_stats["size"]
                stats["analyzable_files_by_extension"][file_extension] += 1
                stats["analyzable_bytes_by_extension"][file_extension] += file_stats[
                    "size"
                ]
                stats["analyzable_bytes"] += file_stats["size"]
                stats["analyzable_files"] += 1

    # Convert to expected output format
    results = []
    for directory in all_directories:
        directory = str(directory)
        if directory in dir_stats:
            stats = dir_stats[directory]
            allowed_top_languages = {
                lang: byte_count
                for lang, byte_count in stats["analyzable_bytes_by_type"].items()
                if lang not in TOP_LANGUAGE_BLACKLIST
            }
            if len(allowed_top_languages) == 0:
                # If no allowed languages exist, allow blacklisted types
                allowed_top_languages = stats["analyzable_bytes_by_type"]
            final_stats = {
                "analyzable_bytes": stats["analyzable_bytes"],
                "analyzable_files": stats["analyzable_files"],
                "analyzable_sloc": bytes_to_sloc(stats["analyzable_bytes"]),
                "total_bytes": stats["total_bytes"],
                "total_files": stats["total_files"],
                "total_sloc": bytes_to_sloc(stats["total_bytes"]),
                "analyzable_files_by_type": dict(stats["analyzable_files_by_type"]),
                "analyzable_bytes_by_type": dict(stats["analyzable_bytes_by_type"]),
                "analyzable_sloc_by_type": {
                    t: bytes_to_sloc(b)
                    for t, b in stats["analyzable_bytes_by_type"].items()
                },
                "analyzable_files_by_extension": dict(
                    stats["analyzable_files_by_extension"]
                ),
                "analyzable_bytes_by_extension": dict(
                    stats["analyzable_bytes_by_extension"]
                ),
                "analyzable_sloc_by_extension": {
                    e: bytes_to_sloc(b)
                    for e, b in stats["analyzable_bytes_by_extension"].items()
                },
                "top_language": max(
                    allowed_top_languages,
                    key=allowed_top_languages.get,
                    default=None,
                ),
            }

            directory_path = Path(directory).relative_to(temp_dir)
            relative_path = (
                str(directory_path)
                if str(directory_path).endswith("/")
                else f"{directory_path}/"
            )
            results.append((final_stats, relative_path))

    return results
