"""
blob_storage.py  —  Azure Blob Storage helper for AI Interview App
===================================================================

Uploads interview recordings (audio/video) and transcription JSON
to Azure Blob Storage, organised into per-candidate sub-folders.

Both files for a session always land in the SAME folder, keyed on
resume_id.  The folder is wiped and recreated fresh on every final
save so the evaluation phase sees exactly one clean, up-to-date set
of files with matching timestamps.

Blob structure (candidate-wise folder, same timestamp for both files):
  {resume_id}/recording_{YYYYMMDD_HHMMSS}.webm      <- video/audio
  {resume_id}/transcription_{YYYYMMDD_HHMMSS}.json  <- transcript

On final save the caller (_auto_save_session in main.py):
  1. Calls check_candidate_folder_exists(resume_id)
  2. If folder exists -> calls delete_candidate_folder(resume_id)
  3. Generates a fresh timestamp shared by both files
  4. Uploads transcription via upload_transcription() with new path
  5. The final recording upload picks up the same fresh timestamp
     from state["final_folder_ts"]

Background / emergency saves overwrite the existing transcription
blob in place (fixed_blob_name) without touching the folder.

REQUIRED .env KEYS
------------------
  AZURE_BLOB_CONNECTION_STRING = DefaultEndpointsProtocol=https;AccountName=...
     OR both of:
  AZURE_BLOB_ACCOUNT_NAME      = <storage account name>
  AZURE_BLOB_ACCOUNT_KEY       = <storage account key>

  AZURE_INTERVIEW_CONTAINER    = interview-files   (optional, defaults shown)
"""

import json
import os
import base64
import datetime
from dotenv import load_dotenv
from azure.storage.blob import BlobBlock

# Lazy import - only fail at upload time, not at app start
try:
    from azure.storage.blob import BlobServiceClient
    _BLOB_AVAILABLE = True
except ImportError:
    _BLOB_AVAILABLE = False
    print("[BlobStorage] azure-storage-blob not installed. "
          "Run: pip install azure-storage-blob")


# def _s(k, d=""):
    # return os.environ.get(k, d).strip().strip('"').strip("'")
load_dotenv()

BLOB_CONNECTION_STRING = os.getenv("AZURE_BLOB_CONNECTION_STRING")
BLOB_ACCOUNT_NAME      = os.getenv("AZURE_BLOB_ACCOUNT_NAME")
BLOB_ACCOUNT_KEY       = os.getenv("AZURE_BLOB_ACCOUNT_KEY")
BLOB_CONTAINER_NAME    = os.getenv("AZURE_INTERVIEW_CONTAINER", "interview-files")


def _get_blob_service_client():
    """
    Returns a BlobServiceClient using either a connection string or
    account name + key from environment variables.
    Raises EnvironmentError if neither is configured.
    """
    if not _BLOB_AVAILABLE:
        raise ImportError(
            "azure-storage-blob is not installed. "
            "Run: pip install azure-storage-blob"
        )

    if BLOB_CONNECTION_STRING:
        return BlobServiceClient.from_connection_string(BLOB_CONNECTION_STRING)

    if BLOB_ACCOUNT_NAME and BLOB_ACCOUNT_KEY:
        account_url = f"https://{BLOB_ACCOUNT_NAME}.blob.core.windows.net"
        from azure.storage.blob import BlobServiceClient as _BSC
        from azure.core.credentials import AzureNamedKeyCredential
        credential = AzureNamedKeyCredential(BLOB_ACCOUNT_NAME, BLOB_ACCOUNT_KEY)
        return _BSC(account_url=account_url, credential=credential)

    raise EnvironmentError(
        "Azure Blob Storage is not configured. Add one of these to .env:\n"
        "  AZURE_BLOB_CONNECTION_STRING=DefaultEndpointsProtocol=https;...\n"
        "  OR both AZURE_BLOB_ACCOUNT_NAME and AZURE_BLOB_ACCOUNT_KEY"
    )


def _ensure_container(client):
    """Create the container if it does not exist yet (idempotent)."""
    try:
        container_client = client.get_container_client(BLOB_CONTAINER_NAME)
        container_client.create_container()
        print(f"[BlobStorage] Created container '{BLOB_CONTAINER_NAME}'")
    except Exception as e:
        if "ContainerAlreadyExists" in str(e) or "409" in str(e):
            pass   # already exists - fine
        else:
            raise


# =============================================================================
# FOLDER CHECK & DELETE
# =============================================================================

def check_candidate_folder_exists(resume_id: str) -> bool:
    """
    Check whether a candidate's folder already exists in blob storage.

    Azure has no real folders - a "folder" is just a blob-name prefix
    like '{safe_resume_id}/'.  This function lists blobs with that prefix
    (limit 1) and returns True if at least one blob is found.

    This is called by _auto_save_session() in main.py before every final
    save so we know whether to delete old files first.

    Args:
        resume_id: raw resume ID string (sanitised internally)

    Returns:
        True  - folder exists (one or more blobs with this prefix)
        False - folder does not exist, or listing failed
    """
    try:
        client    = _get_blob_service_client()
        container = client.get_container_client(BLOB_CONTAINER_NAME)
        safe_id   = _safe_blob_name(resume_id)
        prefix    = f"{safe_id}/"

        # list_blobs is lazy - one item is enough to confirm existence
        blobs  = container.list_blobs(name_starts_with=prefix)
        first  = next(iter(blobs), None)
        exists = first is not None
        print(f"[BlobStorage] Folder check '{prefix}' -> exists={exists}")
        return exists
    except Exception as e:
        print(f"[BlobStorage] check_candidate_folder_exists error: {e}")
        return False


def delete_candidate_folder(resume_id: str) -> dict:
    """
    Delete every blob that belongs to a candidate's folder.

    Azure Blob Storage has no real folders - a "folder" is just a shared
    blob-name prefix.  This function lists every blob whose name starts
    with "{safe_resume_id}/" and deletes them all.

    Called automatically by _auto_save_session() in main.py when
    save_reason="final" so that:
      1. All partial/background-checkpoint files from the current interview
         (recording_..._bg1.webm, recording_..._bg2.webm, etc.) are removed.
      2. The folder is left empty before the definitive final files are written.
      3. The evaluation phase always receives exactly ONE clean folder
         containing only the files from the most recent completed interview.

    Args:
        resume_id: raw resume ID string (sanitised internally)

    Returns:
        {
            "deleted": <int>,             <- number of blobs deleted
            "folder":  "<safe_id>/",
            "blobs":   ["<blob_name>", ...]
        }
    """
    client    = _get_blob_service_client()
    container = client.get_container_client(BLOB_CONTAINER_NAME)
    safe_id   = _safe_blob_name(resume_id)
    prefix    = f"{safe_id}/"

    deleted_names = []
    try:
        blobs = list(container.list_blobs(name_starts_with=prefix))
    except Exception as e:
        print(f"[BlobStorage] delete_candidate_folder: list failed - {e}")
        return {"deleted": 0, "folder": prefix, "blobs": []}

    for blob in blobs:
        try:
            container.delete_blob(blob.name)
            deleted_names.append(blob.name)
            print(f"[BlobStorage] Deleted  {blob.name}")
        except Exception as e:
            print(f"[BlobStorage] Could not delete {blob.name}: {e}")

    print(f"[BlobStorage] Folder '{prefix}' cleared - "
          f"{len(deleted_names)} blob(s) removed")
    return {
        "deleted": len(deleted_names),
        "folder":  prefix,
        "blobs":   deleted_names,
    }


# =============================================================================
# UPLOAD: TRANSCRIPTION
# =============================================================================

def upload_transcription(resume_id: str, transcription_data: dict,
                         fixed_blob_name: str = None) -> dict:
    """
    Upload the interview transcription JSON to Azure Blob Storage.

    Both files always sit in the same folder, keyed on resume_id:

        {resume_id}/transcription_{YYYYMMDD_HHMMSS}.json  <- this function
        {resume_id}/recording_{YYYYMMDD_HHMMSS}.webm      <- upload_recording()

    fixed_blob_name behaviour:
      Provided -> use that path exactly with overwrite=True.
                  Every background/emergency save rewrites the same
                  canonical file without creating extra copies.
      Omitted  -> auto-generate {resume_id}/transcription_{now}.json

    The folder existence check and deletion on final save are handled
    by _auto_save_session() in main.py BEFORE this function is called,
    so the folder is always clean before the final files are written.

    Returns:
        {
            "blob_name": "{resume_id}/transcription_....json",
            "blob_url":  "https://....blob.core.windows.net/...",
            "container": "interview-files"
        }
    """
    import json

    client = _get_blob_service_client()
    _ensure_container(client)

    safe_id = _safe_blob_name(resume_id)

    if fixed_blob_name:
        # Overwrite the same canonical file on every checkpoint
        blob_name = fixed_blob_name
    else:
        # Auto-generate a timestamped path (first save or fallback)
        ts        = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        blob_name = f"{safe_id}/transcription_{ts}.json"

    data_bytes = json.dumps(
        transcription_data, indent=2, ensure_ascii=False
    ).encode("utf-8")

    blob_client = client.get_blob_client(
        container=BLOB_CONTAINER_NAME,
        blob=blob_name,
    )
    blob_client.upload_blob(
        data_bytes,
        overwrite=True,
        content_settings=_json_content_settings(),
    )

    blob_url = blob_client.url
    print(f"[BlobStorage] Transcription uploaded -> {blob_url}")

    return {
        "blob_name": blob_name,
        "blob_url":  blob_url,
        "container": BLOB_CONTAINER_NAME,
    }

def get_latest_transcription_blob(resume_id):
    """Downloads the most recent JSON transcript for the candidate."""
    try:
        blob_service_client = BlobServiceClient.from_connection_string(BLOB_CONNECTION_STRING)
        container_client = blob_service_client.get_container_client(BLOB_CONTAINER_NAME) # or your container name
        prefix = f"{resume_id}/"
        blobs = list(container_client.list_blobs(name_starts_with=prefix))
        
        # Filter for JSON transcripts only
        json_blobs = [b for b in blobs if b.name.endswith('.json')]
        if not json_blobs: return None
        
        # Get the newest one
        latest = sorted(json_blobs, key=lambda x: x.name, reverse=True)[0]
        blob_client = container_client.get_blob_client(latest.name)
        return json.loads(blob_client.download_blob().readall())
    except Exception as e:
        print(f"Blob Fetch Error: {e}")
        return None
# =============================================================================
# UPLOAD: RECORDING
# =============================================================================

def upload_recording(resume_id: str, recording_bytes: bytes,
                     content_type: str = "video/webm",
                     timestamp: str = None) -> dict:
    """
    Upload a video/audio recording to Azure Blob Storage.

    Both the recording and the transcription for the same session use the
    same resume_id folder and the same timestamp string so they are always
    co-located and easily matched:

        {resume_id}/recording_{YYYYMMDD_HHMMSS}.webm
        {resume_id}/transcription_{YYYYMMDD_HHMMSS}.json

    Background chunk uploads (bg0, bg1, ...) accumulate in the folder
    during the interview.  On final save _auto_save_session() wipes the
    entire folder first, then the final recording and transcription are
    uploaded using the fresh completion timestamp.

    Args:
        resume_id:       raw resume ID string (folder name key)
        recording_bytes: raw bytes of the recording file
        content_type:    MIME type - e.g. "video/webm", "video/mp4"
        timestamp:       pre-generated string (YYYYmmdd_HHMMSS);
                         auto-generated from now() if not provided

    Returns:
        {
            "blob_name": "{resume_id}/recording_....webm",
            "blob_url":  "https://....blob.core.windows.net/...",
            "container": "interview-files"
        }
    """
    client = _get_blob_service_client()
    _ensure_container(client)

    ts      = timestamp or datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_id = _safe_blob_name(resume_id)
    ext     = _ext_for_mime(content_type)

    # Both recording and transcription use the same {resume_id}/ prefix
    blob_name = f"{safe_id}/recording_{ts}{ext}"

    blob_client = client.get_blob_client(
        container=BLOB_CONTAINER_NAME,
        blob=blob_name,
    )
    # Upload in 4MB blocks — each block has its own timeout
    # avoids single large PUT request that causes write timeouts
    CHUNK_SIZE = 4 * 1024 * 1024  # 4MB
    block_list = []
    idx = 0
    chunk_num = 0

    while idx < len(recording_bytes):
        chunk    = recording_bytes[idx: idx + CHUNK_SIZE]
        block_id = base64.b64encode(f"{chunk_num:06d}".encode()).decode()
        blob_client.stage_block(block_id, chunk, timeout=120)
        block_list.append(BlobBlock(block_id=block_id))
        idx       += CHUNK_SIZE
        chunk_num += 1

    blob_client.commit_block_list(
        block_list,
        content_settings=_media_content_settings(content_type),
    )

    blob_url = blob_client.url
    print(f"[BlobStorage] Recording uploaded "
          f"({len(recording_bytes):,} bytes) -> {blob_url}")

    return {
        "blob_name": blob_name,
        "blob_url":  blob_url,
        "container": BLOB_CONTAINER_NAME,
    }




def upload_pdf(resume_id: str, pdf_path: str,
               timestamp: str = None) -> dict:
    """
    Upload an evaluation PDF report to Azure Blob Storage, into the same
    candidate folder that holds the transcription and recording:

        {resume_id}/evaluation_{YYYYMMDD_HHMMSS}.pdf

    Args:
        resume_id: raw resume ID string (used as the folder name)
        pdf_path:  local path to the generated PDF file
        timestamp: optional pre-generated YYYYMMDD_HHMMSS string;
                   auto-generated from now() if not provided

    Returns:
        {
            "blob_name": "{resume_id}/evaluation_....pdf",
            "blob_url":  "https://....blob.core.windows.net/...",
            "container": "interview-files"
        }
    """
    client = _get_blob_service_client()
    _ensure_container(client)

    ts      = timestamp or datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_id = _safe_blob_name(resume_id)
    blob_name = f"{safe_id}/evaluation_{ts}.pdf"

    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()

    from azure.storage.blob import ContentSettings
    blob_client = client.get_blob_client(
        container=BLOB_CONTAINER_NAME,
        blob=blob_name,
    )
    blob_client.upload_blob(
        pdf_bytes,
        overwrite=True,
        content_settings=ContentSettings(content_type="application/pdf"),
    )

    blob_url = blob_client.url
    print(f"[BlobStorage] Evaluation PDF uploaded "
          f"({len(pdf_bytes):,} bytes) -> {blob_url}")

    return {
        "blob_name": blob_name,
        "blob_url":  blob_url,
        "container": BLOB_CONTAINER_NAME,
    }


# =============================================================================
# HELPERS
# =============================================================================

def _safe_blob_name(name: str) -> str:
    """Strip characters that are invalid in blob names / folder prefixes."""
    import re
    return re.sub(r"[^A-Za-z0-9_\-.]", "_", name)


def _json_content_settings():
    from azure.storage.blob import ContentSettings
    return ContentSettings(content_type="application/json")


def _media_content_settings(content_type: str):
    from azure.storage.blob import ContentSettings
    return ContentSettings(content_type=content_type)


def _ext_for_mime(content_type: str) -> str:
    mapping = {
        "video/webm":  ".webm",
        "audio/webm":  ".webm",
        "video/mp4":   ".mp4",
        "audio/mp4":   ".mp4",
        "audio/ogg":   ".ogg",
        "audio/wav":   ".wav",
    }
    return mapping.get(content_type.lower().split(";")[0].strip(), ".bin")


def blob_storage_configured() -> bool:
    """Returns True if Blob Storage credentials are present in the environment."""
    return bool(BLOB_CONNECTION_STRING or (BLOB_ACCOUNT_NAME and BLOB_ACCOUNT_KEY))