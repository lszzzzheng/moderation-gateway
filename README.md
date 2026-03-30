# moderation-gateway

Standalone moderation gateway for Discourse + Aliyun multimodal moderation.

This service exposes:
- `GET /healthz`
- `POST /moderate`

It supports two scenes:
- `scene=post` -> `post_text_image_detection`
- `scene=profile` -> `profile_text_image_detection`

When a request has no images:
- post text falls back to `MODERATION_TEXT_SERVICE` (default `ugc_moderation_byllm`)
- profile text falls back to `MODERATION_PROFILE_TEXT_SERVICE` (default `nickname_detection_pro`)

## Files

- `.env.example`: environment variables template
- `docker-compose.yml`: recommended deployment entry
- `Dockerfile`: container image build
- `gateway/app.py`: Flask app

## Quick Start

```bash
cp .env.example .env
docker compose up -d --build
```

Then verify:

```bash
curl -sS http://127.0.0.1:8080/healthz
```

## Example Requests

Post moderation:

```bash
curl -sS -X POST 'http://127.0.0.1:8080/moderate' \
  -H 'Content-Type: application/json' \
  -d '{"scene":"post","title":"test","text":"hello","images":[]}'
```

Profile moderation:

```bash
curl -sS -X POST 'http://127.0.0.1:8080/moderate' \
  -H 'Content-Type: application/json' \
  -d '{"scene":"profile","text":"nickname","images":["https://example.com/avatar.jpg"]}'
```

## Decision Mapping

- `risk_level=none` -> `PASS`
- `risk_level=low|medium` -> `REVIEW`
- `risk_level=high` -> `REJECT`
- remote failure -> `REVIEW` if `MODERATION_STRICT_FAIL_SAFE=true`
