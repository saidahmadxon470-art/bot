import asyncio
import base64
import hashlib
import json
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx
import imagehash
import pytesseract
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field
from rapidfuzz.fuzz import partial_ratio, ratio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.models import Submission, Task, VerificationResult


class VisionAssessment(BaseModel):
    model_config = ConfigDict(extra="ignore")

    has_task_evidence: bool
    text_similarity: float = Field(ge=0, le=1)
    suspicious: bool
    confidence: float = Field(ge=0, le=1)
    decision: Literal["VERIFIED", "REJECTED", "UNCERTAIN"]
    reason: str


@dataclass
class VerificationDecision:
    status: str
    reason: str
    ocr_text: str
    ai_result: dict
    similarity: float
    fraud_score: int
    perceptual_hash: str


def _read_image(path: Path) -> tuple[str, str]:
    with Image.open(path) as image:
        if image.width * image.height > 40_000_000:
            raise ValueError("Image dimensions exceed the verification limit")
        image.verify()
    with Image.open(path) as image:
        image = image.convert("RGB")
        return str(imagehash.phash(image)), pytesseract.image_to_string(image, timeout=20)


async def _vision_assessment(path: Path, task: Task, settings: Settings, ocr_text: str) -> VisionAssessment | None:
    api_key = settings.ai_api_key.get_secret_value()
    if not api_key:
        return None
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    media_type = mimetypes.guess_type(path.name)[0] or "image/png"
    prompt = (
        "Analyze this screenshot as evidence that an assigned team task was completed. "
        "Do not infer facts that are not visible. Compare the visible content with these task instructions: "
        f"{task.advertisement_text!r}. OCR text: {ocr_text[:6000]!r}. "
        "If a second image is supplied, it is the admin's reference task image. "
        "Do not require a Telegram channel, group, post, or Telegram UI. "
        "Return only JSON with keys has_task_evidence, "
        "text_similarity (0..1), suspicious, confidence (0..1), decision (VERIFIED/REJECTED/UNCERTAIN), reason."
    )
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=45) as client:
                response = await client.post(
                    f"{settings.ai_base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": settings.ai_model,
                        "response_format": {"type": "json_object"},
                        "messages": [{"role": "user", "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{encoded}"}},
                            *([{"type": "text", "text": "Admin reference task image:"}, {
                                "type": "image_url",
                                "image_url": {"url": f"data:{mimetypes.guess_type(task.advertisement_image_path)[0] or 'image/png'};base64,{base64.b64encode(Path(task.advertisement_image_path).read_bytes()).decode('ascii')}"},
                            }] if task.advertisement_image_path and Path(task.advertisement_image_path).is_file() else []),
                        ]}],
                    },
                )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                return VisionAssessment.model_validate_json(content)
        except (httpx.HTTPError, KeyError, ValueError):
            if attempt == 2:
                return None
            await asyncio.sleep(2**attempt)
    return None


def _normalized(text: str) -> str:
    return " ".join(text.casefold().split())


async def verify_submission(
    session: AsyncSession, submission: Submission, task: Task, settings: Settings
) -> VerificationDecision:
    path = Path(submission.image_path)
    try:
        perceptual_hash, ocr_text = await asyncio.to_thread(_read_image, path)
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError, pytesseract.TesseractNotFoundError, RuntimeError):
        return VerificationDecision("MANUAL_REVIEW", "OCR yoki image validation muvaffaqiyatsiz", "", {}, 0, 45, "")

    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    submission.sha256 = sha256
    submission.perceptual_hash = perceptual_hash
    previous = list((await session.scalars(
        select(Submission).options(selectinload(Submission.verification)).where(
            Submission.id != submission.id
        ).order_by(Submission.created_at.desc()).limit(500)
    )).all())
    exact_duplicate = any(item.sha256 == sha256 for item in previous)
    candidate_hash = imagehash.hex_to_hash(perceptual_hash)
    near_duplicate = any(
        item.perceptual_hash and candidate_hash - imagehash.hex_to_hash(item.perceptual_hash) <= 4
        for item in previous
    )
    matching_ocr = any(
        item.verification and _normalized(item.verification.ocr_text)
        and ratio(_normalized(ocr_text), _normalized(item.verification.ocr_text)) >= 92
        for item in previous
    )
    fraud_score = 90 if exact_duplicate else 65 if near_duplicate else 45 if matching_ocr else 0

    vision = await _vision_assessment(path, task, settings, ocr_text)
    expected_text = _normalized(task.advertisement_text)
    similarity = partial_ratio(expected_text, _normalized(ocr_text)) / 100 if ocr_text.strip() else 0
    ai_result = vision.model_dump() if vision else {}

    if exact_duplicate:
        status, reason = "MANUAL_REVIEW", "Screenshot avval boshqa submissionda ishlatilgan"
    elif not ocr_text.strip() or vision is None:
        status, reason = "MANUAL_REVIEW", "OCR/AI natijasi yetarli emas yoki AI provider sozlanmagan"
    elif vision.suspicious or near_duplicate or matching_ocr or vision.confidence < settings.manual_review_confidence:
        status, reason = "MANUAL_REVIEW", "Shubhali yoki ishonch darajasi past submission"
    elif not vision.has_task_evidence or vision.decision == "REJECTED":
        status, reason = "REJECTED", vision.reason or "Screenshotda topshiriq bajarilgani ko‘rinmadi"
    elif task.matching_mode.value == "EXACT_TEXT" and similarity < task.similarity_threshold:
        status, reason = "REJECTED", "Screenshotdagi matn topshiriq matniga mos emas"
    elif task.matching_mode.value == "FLEXIBLE_TEXT" and max(similarity, vision.text_similarity) < task.similarity_threshold:
        status, reason = "REJECTED", "Screenshotdagi mazmun topshiriqqa mos emas"
    elif vision.decision == "VERIFIED":
        status, reason = "APPROVED", vision.reason
    else:
        status, reason = "MANUAL_REVIEW", vision.reason or "Avtomatik tasdiq uchun dalil yetarli emas"

    if fraud_score >= 70 and status == "APPROVED":
        status, reason = "REJECTED" if settings.high_fraud_policy == "reject" else "MANUAL_REVIEW", "Yuqori duplicate/fraud xavfi"
    return VerificationDecision(status, reason, ocr_text, ai_result, similarity, fraud_score, perceptual_hash)


def save_verification_result(submission_id: int, decision: VerificationDecision) -> VerificationResult:
    return VerificationResult(
        submission_id=submission_id,
        ocr_text=decision.ocr_text,
        ai_result=json.dumps(decision.ai_result, ensure_ascii=True),
        text_similarity=decision.similarity,
        fraud_score=decision.fraud_score,
        decision_reason=decision.reason,
    )
