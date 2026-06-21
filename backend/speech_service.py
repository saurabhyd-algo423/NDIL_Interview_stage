# """
# speech_service.py
# =================
# Azure Speech Service helpers:
#   - Speech token fetching with 9-minute in-memory cache
#   - ICE relay token for avatar WebRTC

# Both functions are used by routes in routes_avatar.py.
# """

# import time
# import requests

# from config import SPEECH_KEY, SPEECH_REGION

# # ── Speech token cache — tokens are valid 10 min, we cache for 9 ─────────────
# _speech_token_cache: dict = {"token": None, "expires": 0}


# def get_speech_token() -> str:
#     """
#     Return a valid Azure Speech token, fetching a fresh one when the
#     cached token has expired.  Raises on auth failure or network error.
#     """
#     if not SPEECH_KEY:
#         raise ValueError("SPEECH_KEY is empty — add it to .env")

#     now = time.time()
#     if _speech_token_cache["token"] and now < _speech_token_cache["expires"]:
#         return _speech_token_cache["token"]

#     headers = {"Ocp-Apim-Subscription-Key": SPEECH_KEY}
#     for url in (
#         f"https://{SPEECH_REGION}.api.cognitive.microsoft.com/sts/v1.0/issueToken",
#         f"https://{SPEECH_REGION}.tts.speech.microsoft.com/cognitiveservices/v1/issueToken",
#     ):
#         try:
#             r = requests.post(url, headers=headers, timeout=10)
#             if r.status_code == 200:
#                 _speech_token_cache["token"]   = r.text
#                 _speech_token_cache["expires"] = now + 540   # cache for 9 min
#                 return r.text
#             if r.status_code == 401:
#                 raise PermissionError(
#                     f"Azure Speech 401. SPEECH_REGION={SPEECH_REGION}  "
#                     f"Key={SPEECH_KEY[:6]}...{SPEECH_KEY[-4:]}\n"
#                     f"  → Check SPEECH_KEY and SPEECH_REGION in .env"
#                 )
#         except (PermissionError, ValueError):
#             raise
#         except Exception:
#             pass   # try the fallback URL

#     raise RuntimeError("Both Speech token endpoints failed.")


# def get_ice_token() -> dict:
#     """
#     Return the ICE relay token for avatar WebRTC peer connection.
#     Raises requests.HTTPError on failure.
#     """
#     url = (
#         f"https://{SPEECH_REGION}.tts.speech.microsoft.com"
#         "/cognitiveservices/avatar/relay/token/v1"
#     )
#     r = requests.get(url, headers={"Ocp-Apim-Subscription-Key": SPEECH_KEY}, timeout=10)
#     r.raise_for_status()
#     return r.json()


# import time
# import requests
# from config import SPEECH_KEY, SPEECH_REGION

# _speech_token_cache: dict = {"token": None, "expires": 0}

# def get_speech_token() -> str:
#     if not SPEECH_KEY:
#         raise ValueError("SPEECH_KEY is empty — add it to .env")
#     now = time.time()
#     if _speech_token_cache["token"] and now < _speech_token_cache["expires"]:
#         return _speech_token_cache["token"]
#     headers = {"Ocp-Apim-Subscription-Key": SPEECH_KEY}
#     for url in (
#         f"https://{SPEECH_REGION}.api.cognitive.microsoft.com/sts/v1.0/issueToken",
#         f"https://{SPEECH_REGION}.tts.speech.microsoft.com/cognitiveservices/v1/issueToken",
#     ):
#         try:
#             r = requests.post(url, headers=headers, timeout=10)
#             if r.status_code == 200:
#                 _speech_token_cache["token"]   = r.text
#                 _speech_token_cache["expires"] = now + 540  # 9 min
#                 return r.text
#             if r.status_code == 401:
#                 raise PermissionError(
#                     f"Azure Speech 401. SPEECH_REGION={SPEECH_REGION}  "
#                     f"Key={SPEECH_KEY[:6]}...{SPEECH_KEY[-4:]}\n"
#                     f"  → Check SPEECH_KEY and SPEECH_REGION in .env"
#                 )
#         except (PermissionError, ValueError):
#             raise
#         except Exception:
#             pass
#     raise RuntimeError("Both Speech token endpoints failed.")

# def get_ice_token() -> dict:
#     url = (
#         f"https://{SPEECH_REGION}.tts.speech.microsoft.com"
#         "/cognitiveservices/avatar/relay/token/v1"
#     )
#     r = requests.get(url, headers={"Ocp-Apim-Subscription-Key": SPEECH_KEY}, timeout=10)
#     r.raise_for_status()
#     return r.json()

"""
speech_service.py
=================
Azure Speech Service helpers:
  - Speech token fetching with 9-minute in-memory cache
  - ICE relay token for avatar WebRTC

Both functions are used by routes in routes_avatar.py.

TOKEN LIFECYCLE
---------------
Azure Speech tokens are valid for exactly 10 minutes from issue time.
We cache each token for 9 minutes (540 s) to avoid hammering the Azure
issueToken endpoint on every request.

get_speech_token(force=False)
  force=False  → return cached token if still valid (normal proactive refresh)
  force=True   → bypass cache, always fetch a brand-new token from Azure
                 (used by _rebuildRecognizer in interview.js after a 1006 error,
                  so a reactive rebuild never gets the same stale token back)

The route /api/getSpeechToken accepts an optional query param ?force=1
to expose force-refresh to the frontend.

The response always includes:
  { "token": "...", "region": "...", "expiresAt": <unix-ms> }
so the frontend can log/display the real expiry and schedule refreshes accurately.
"""

import time
import requests

from config import SPEECH_KEY, SPEECH_REGION

# ── Speech token cache — tokens are valid 10 min, we cache for 9 ─────────────
_speech_token_cache: dict = {"token": None, "expires": 0, "issued_at": 0}

# Azure token lifetime is 600 s; we cache for 540 s (9 min) so we always
# have at least 60 s of headroom before the SDK token actually expires.
_TOKEN_CACHE_SECS = 540   # 9 minutes


def get_speech_token(force: bool = False) -> dict:
    """
    Return a dict with keys: token, region, expiresAt (Unix ms).

    force=True  → always fetch a fresh token from Azure, bypassing the cache.
                  Use this after a reactive STT rebuild (1006 auth failure) to
                  guarantee the new recognizer gets a token that's valid for the
                  full 10 minutes, not one that's about to expire.
    force=False → return the cached token when it's still valid.

    Raises PermissionError on 401, ValueError on missing key,
    RuntimeError if both issueToken endpoints fail.
    """
    if not SPEECH_KEY:
        raise ValueError("SPEECH_KEY is empty — add it to .env")

    now = time.time()

    # Return cached token unless it has expired or caller forces a refresh
    if (
        not force
        and _speech_token_cache["token"]
        and now < _speech_token_cache["expires"]
    ):
        expires_at_ms = int(_speech_token_cache["issued_at"] * 1000) + 600_000
        return {
            "token":     _speech_token_cache["token"],
            "region":    SPEECH_REGION,
            "expiresAt": expires_at_ms,   # milliseconds, matches JS Date() format
        }

    # Fetch a brand-new token from Azure
    headers = {"Ocp-Apim-Subscription-Key": SPEECH_KEY}
    for url in (
        f"https://{SPEECH_REGION}.api.cognitive.microsoft.com/sts/v1.0/issueToken",
        f"https://{SPEECH_REGION}.tts.speech.microsoft.com/cognitiveservices/v1/issueToken",
    ):
        try:
            r = requests.post(url, headers=headers, timeout=10)
            if r.status_code == 200:
                issued_at = time.time()
                _speech_token_cache["token"]     = r.text
                _speech_token_cache["expires"]   = issued_at + _TOKEN_CACHE_SECS
                _speech_token_cache["issued_at"] = issued_at
                expires_at_ms = int(issued_at * 1000) + 600_000  # 10 min in ms
                return {
                    "token":     r.text,
                    "region":    SPEECH_REGION,
                    "expiresAt": expires_at_ms,
                }
            if r.status_code == 401:
                raise PermissionError(
                    f"Azure Speech 401. SPEECH_REGION={SPEECH_REGION}  "
                    f"Key={SPEECH_KEY[:6]}...{SPEECH_KEY[-4:]}\n"
                    f"  → Check SPEECH_KEY and SPEECH_REGION in .env"
                )
        except (PermissionError, ValueError):
            raise
        except Exception:
            pass   # try the fallback URL

    raise RuntimeError("Both Speech token endpoints failed.")


def get_ice_token() -> dict:
    """
    Return the ICE relay token for avatar WebRTC peer connection.
    Raises requests.HTTPError on failure.
    """
    url = (
        f"https://{SPEECH_REGION}.tts.speech.microsoft.com"
        "/cognitiveservices/avatar/relay/token/v1"
    )
    r = requests.get(url, headers={"Ocp-Apim-Subscription-Key": SPEECH_KEY}, timeout=10)
    r.raise_for_status()
    return r.json()