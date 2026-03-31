import json
import os
import time
import uuid
from typing import Any, Dict, List

from flask import Flask, jsonify, request
from alibabacloud_green20220302 import models as green_models
from alibabacloud_green20220302.client import Client as GreenClient
from alibabacloud_tea_openapi.models import Config

app = Flask(__name__)

MAX_MULTIMODAL_TEXT_CHARS = 5000


class Settings:
    region_id = os.getenv("ALIBABA_CLOUD_REGION_ID", "cn-shanghai")
    endpoint = os.getenv("ALIBABA_CLOUD_ENDPOINT", "green-cip.cn-shanghai.aliyuncs.com")
    access_key_id = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_ID", "")
    access_key_secret = os.getenv("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "")

    service = os.getenv("MODERATION_SERVICE", "post_text_image_detection")
    profile_service = os.getenv("MODERATION_PROFILE_SERVICE", "profile_text_image_detection")
    text_service = os.getenv("MODERATION_TEXT_SERVICE", "ugc_moderation_byllm")
    profile_text_service = os.getenv("MODERATION_PROFILE_TEXT_SERVICE", "nickname_detection_pro")
    biz_type = os.getenv("MODERATION_BIZ_TYPE", "default")
    poll_interval_seconds = float(os.getenv("MODERATION_POLL_INTERVAL_SECONDS", "2"))
    poll_max_attempts = int(os.getenv("MODERATION_POLL_MAX_ATTEMPTS", "8"))
    strict_fail_safe = os.getenv("MODERATION_STRICT_FAIL_SAFE", "true").lower() == "true"


SETTINGS = Settings()


def build_client() -> GreenClient:
    if not SETTINGS.access_key_id or not SETTINGS.access_key_secret:
        raise RuntimeError("Missing ALIBABA_CLOUD_ACCESS_KEY_ID/ALIBABA_CLOUD_ACCESS_KEY_SECRET")

    config = Config(
        access_key_id=SETTINGS.access_key_id,
        access_key_secret=SETTINGS.access_key_secret,
        region_id=SETTINGS.region_id,
        endpoint=SETTINGS.endpoint,
        connect_timeout=3000,
        read_timeout=8000,
    )
    return GreenClient(config)


def normalize_images(images: List[Any]) -> List[Dict[str, str]]:
    normalized: List[Dict[str, str]] = []
    for item in images:
        if isinstance(item, str) and item.strip():
            normalized.append({"imageUrl": item.strip()})
        elif isinstance(item, dict) and item.get("imageUrl"):
            normalized.append({"imageUrl": str(item["imageUrl"]).strip()})
    return normalized


def to_service_parameters(payload: Dict[str, Any]) -> Dict[str, Any]:
    title, text = trim_multimodal_text(
        str(payload.get("title", "")).strip(),
        str(payload.get("text", "")).strip(),
    )
    images = normalize_images(payload.get("images", []))
    comments = payload.get("comments", [])

    return {
        "dataId": payload.get("data_id") or str(uuid.uuid4()),
        "bizType": payload.get("biz_type") or SETTINGS.biz_type,
        "mainData": {
            "mainTitle": title,
            "mainContent": text,
            "mainImages": images,
            "mainPostTime": payload.get("post_time") or time.strftime("%Y-%m-%d %H:%M:%S"),
        },
        "commentDatas": comments,
    }


def trim_multimodal_text(title: str, text: str) -> tuple[str, str]:
    if len(title) + len(text) <= MAX_MULTIMODAL_TEXT_CHARS:
        return title, text

    title = title[: min(len(title), MAX_MULTIMODAL_TEXT_CHARS)]
    remaining = max(MAX_MULTIMODAL_TEXT_CHARS - len(title), 0)
    return title, text[:remaining]


def text_content(payload: Dict[str, Any]) -> str:
    parts = [str(payload.get("title", "")).strip(), str(payload.get("text", "")).strip()]
    content = "\n".join([part for part in parts if part]).strip()
    return content[:MAX_MULTIMODAL_TEXT_CHARS]


def payload_text_length(payload: Dict[str, Any]) -> int:
    title = str(payload.get("title", "")).strip()
    text = str(payload.get("text", "")).strip()
    return len(title) + len(text)


def text_service_parameters(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "content": text_content(payload),
        "dataId": payload.get("data_id") or str(uuid.uuid4()),
    }


def resolve_service(payload: Dict[str, Any], images: List[Dict[str, str]]) -> str:
    scene = str(payload.get("scene", "")).lower()
    if scene == "profile" and not images:
        return SETTINGS.profile_text_service
    if scene == "profile":
        return SETTINGS.profile_service
    if scene == "post" and not images:
        return SETTINGS.text_service
    if scene == "post":
        return SETTINGS.service
    return str(payload.get("service") or SETTINGS.service)


def map_decision(risk_level: str) -> str:
    risk_level = (risk_level or "").lower()
    if risk_level == "none":
        return "PASS"
    if risk_level in {"low", "medium"}:
        return "REVIEW"
    if risk_level == "high":
        return "REJECT"
    # Unknown risk level: conservative by default
    return "REVIEW"


def limit_review_result(risk_level: str, error: str) -> Dict[str, Any]:
    return {
        "decision": "REVIEW",
        "risk_level": risk_level,
        "labels": [],
        "req_id": "",
        "service": "policy_limit",
        "error": error,
        "raw": {},
    }


def submit_and_poll(payload: Dict[str, Any]) -> Dict[str, Any]:
    client = build_client()
    images = normalize_images(payload.get("images", []))
    scene = str(payload.get("scene", "")).lower()
    text_length = payload_text_length(payload)
    if scene == "post" and text_length > MAX_MULTIMODAL_TEXT_CHARS:
        if images:
            return limit_review_result("text_too_long_for_multimodal", "text_too_long_for_multimodal")
        return limit_review_result("text_too_long", "text_too_long")

    service = resolve_service(payload, images)
    if service in {SETTINGS.text_service, SETTINGS.profile_text_service}:
        return submit_text_moderation(client, payload, service)

    service_parameters = to_service_parameters(payload)

    submit_request = green_models.MultimodalAsyncModerationRequest(
        service=service,
        service_parameters=json.dumps(service_parameters, ensure_ascii=False),
    )
    submit_response = client.multimodal_async_moderation(submit_request).body.to_map()

    req_id = submit_response.get("Data", {}).get("ReqId")
    if not req_id:
        raise RuntimeError(f"Submit failed: {submit_response}")

    query_request = green_models.DescribeMultimodalModerationResultRequest(req_id=req_id)

    result_body: Dict[str, Any] = {}
    for _ in range(SETTINGS.poll_max_attempts):
        time.sleep(SETTINGS.poll_interval_seconds)
        result_body = client.describe_multimodal_moderation_result(query_request).body.to_map()
        if result_body.get("Data"):
            break

    if not result_body.get("Data"):
        raise TimeoutError("Timed out waiting for moderation result")

    data = result_body.get("Data", {})
    risk_level = data.get("RiskLevel", "")

    return {
        "decision": map_decision(risk_level),
        "risk_level": risk_level,
        "labels": data.get("MainData", {}).get("Results", []),
        "req_id": data.get("ReqId") or req_id,
        "service": service,
        "raw": result_body,
    }


def submit_text_moderation(client: GreenClient, payload: Dict[str, Any], service: str) -> Dict[str, Any]:
    content = text_content(payload)
    if not content:
        raise ValueError("Text moderation requires non-empty title or text content")

    request = green_models.TextModerationPlusRequest(
        service=service,
        service_parameters=json.dumps(text_service_parameters(payload), ensure_ascii=False),
    )
    response = client.text_moderation_plus(request).body.to_map()
    data = response.get("Data", {})
    risk_level = data.get("RiskLevel", "")

    return {
        "decision": map_decision(risk_level),
        "risk_level": risk_level,
        "labels": data.get("Result", []),
        "req_id": response.get("RequestId", ""),
        "service": service,
        "raw": response,
    }


@app.get("/healthz")
def healthz():
    return jsonify(
        {
            "ok": True,
            "service": SETTINGS.service,
            "profile_service": SETTINGS.profile_service,
            "text_service": SETTINGS.text_service,
            "profile_text_service": SETTINGS.profile_text_service,
        }
    )


@app.post("/moderate")
def moderate():
    payload = request.get_json(silent=True) or {}
    required_missing = []
    if not payload.get("text") and not payload.get("title") and not payload.get("images"):
        required_missing.append("text/title/images")
    if required_missing:
        return jsonify({"ok": False, "error": f"Missing required field: {', '.join(required_missing)}"}), 400

    try:
        result = submit_and_poll(payload)
        return jsonify({"ok": True, **result}), 200
    except Exception as exc:  # noqa: BLE001
        # Fail-safe policy: push to manual review when remote check is unavailable.
        if SETTINGS.strict_fail_safe:
            return (
                jsonify(
                    {
                        "ok": False,
                        "decision": "REVIEW",
                        "error_type": "gateway_exception",
                        "error": str(exc),
                        "risk_level": "unknown",
                        "labels": [],
                    }
                ),
                200,
            )
        return jsonify({"ok": False, "error": str(exc)}), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
