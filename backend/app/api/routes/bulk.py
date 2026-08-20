import asyncio
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.api.routes.screening import load_ranked_results, persist_result
from app.core.config import get_settings
from app.database.database import get_db
from app.models.job import Job
from app.models.resume import Resume
from app.schemas.screening import ScreeningResultsResponse
from app.services.bulk_processor import process_bulk_upload
from app.services.eligibility_service import NOT_EVALUATED_FIELDS
from app.utils.file_validation import ALLOWED_EXTENSIONS, FileValidationError, sanitize_filename, validate_upload

router = APIRouter()


@router.post("/screen/bulk", response_model=ScreeningResultsResponse)
async def bulk_screen(
    job_id: str = Form(...),
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    settings = get_settings()
    parsed_files = []
    validation_failures = []

    for upload in files:
        filename = sanitize_filename(upload.filename or "")
        content = await upload.read()
        try:
            ext = validate_upload(
                filename, content, upload.content_type, settings.max_upload_size_bytes, ALLOWED_EXTENSIONS
            )
        except FileValidationError as exc:
            validation_failures.append(
                {
                    "candidate_id": None,
                    "candidate_name": "",
                    "filename": filename,
                    "status": "failed",
                    "failure_reason": exc.message,
                    "score": None,
                    "skills": None,
                    "ranking": None,
                    **NOT_EVALUATED_FIELDS,
                }
            )
            continue
        parsed_files.append({"filename": filename, "content": content, "extension": ext})

    try:
        outcome = await asyncio.wait_for(
            asyncio.to_thread(process_bulk_upload, job, parsed_files),
            timeout=45.0,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Bulk screening timed out. Try fewer resumes at once.")

    for parsed in outcome["parsed_resumes"]:
        resume = Resume(
            id=parsed["id"],
            filename=parsed["filename"],
            candidate_name=parsed["data"]["name"],
            raw_text=parsed["data"]["raw_text"],
            structured_data=parsed["data"],
        )
        db.merge(resume)
    db.commit()

    for result in outcome["results"]:
        persist_result(db, job_id, result)

    for failure in validation_failures:
        failure["candidate_id"] = str(uuid.uuid4())
        persist_result(db, job_id, failure)

    ranked = load_ranked_results(db, job_id)
    return {"job_id": job_id, "results": ranked}
