from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from lockstep.api.deps import get_current_user
from lockstep.models.user import User
from lockstep.schemas.gstr2b import Gstr2bMockOut
from lockstep.services.mock_gstr2b import get_gstr2b_mock, get_gstr2b_payload

router = APIRouter(prefix="/gstr2b", tags=["gstr2b"])


@router.get("", response_model=Gstr2bMockOut)
async def get_gstr2b(
    variant: Literal["inconsistent", "corrected"] = Query("corrected"),
    format: Literal["full", "gstn"] = Query("full"),
    current_user: User = Depends(get_current_user),
):
    """Stub GSTR-2B in GSTN envelope shape. `corrected` is the sandbox sample
    as-filed; `inconsistent` seeds missing invoices, typos, tax diffs and ITC
    blocks. Swap this for a live GSP later — the envelope does not change.
    """
    del current_user
    if format == "gstn":
        return JSONResponse(get_gstr2b_payload(variant))
    return Gstr2bMockOut.model_validate(get_gstr2b_mock(variant))
