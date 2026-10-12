"""Classify provider error bodies locally; never retain their sensitive text."""

import re


def provider_error_category(payload, status=None):
    # HTTP codes and explicitly structured MCP error codes take precedence.
    if isinstance(payload, dict):
        error = payload.get("error", payload)
        if isinstance(error, dict):
            code = error.get("status_code", error.get("code"))
            if str(code).isdigit():
                status = int(code)
    text = str(payload).lower()[:8000]
    if status in {432, 433} or re.search(r'quota.{0,30}(exhaust|exceed)|(?:credit|usage|plan).{0,30}(exhaust|exceed|limit)|insufficient.{0,20}credit', text):
        return "quota_exhausted"
    if re.search(r'account.{0,30}(suspend|restrict|block)|forbidden', text):
        return "account_restricted"
    if status in {401, 403} or re.search(r'invalid.{0,20}(api.?key|credential)|unauthorized|authentication', text):
        return "authentication"
    if re.search(r'context.{0,40}(length|window|exceed)|maximum.{0,20}context', text):
        return "context_overflow"
    if status in {400, 404, 405, 422, 501} or re.search(r'invalid.{0,20}(parameter|request)|unsupported.{0,20}(operation|tool)', text):
        return "invalid_request"
    if status == 408:
        return "timeout"
    if status == 429 or re.search(r'rate.?limit|too many requests', text):
        return "rate_limit"
    if re.search(r'temporar|unavailable|try again|internal server|bad gateway|gateway timeout', text):
        return "provider_unavailable"
    if re.search(r'time.?out|timed out', text):
        return "timeout"
    return None
