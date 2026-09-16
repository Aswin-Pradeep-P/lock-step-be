"""POST /api/tally/connect and POST /api/tally/purchase-register."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse

from lockstep.schemas.fe_types import (
    TallyConnectRequest,
    TallyConnectResponse,
    TallyPurchaseRequest,
    TallyPurchaseResponse,
)
from lockstep.services.tally_client import (
    build_company_list_xml,
    build_purchase_voucher_xml,
    parse_company_list,
    parse_purchase_vouchers,
    post_to_tally,
)

router = APIRouter(prefix="/tally", tags=["fe-tally"])


@router.post("/connect")
async def tally_connect(payload: TallyConnectRequest) -> JSONResponse:
    try:
        xml = build_company_list_xml()
        response = await post_to_tally(payload.host, payload.port, xml)
        companies = parse_company_list(response)

        if not companies:
            result = TallyConnectResponse(
                connected=True,
                companies=[],
                warning="Connected to TallyPrime but no companies found. Please open a company in TallyPrime.",
            )
        else:
            result = TallyConnectResponse(connected=True, companies=companies)

        return JSONResponse(content=result.to_camel_dict())

    except (ConnectionError, ValueError) as e:
        result = TallyConnectResponse(connected=False, companies=[], error=str(e))
        return JSONResponse(content=result.to_camel_dict())


@router.post("/purchase-register")
async def tally_purchase_register(payload: TallyPurchaseRequest) -> JSONResponse:
    if not payload.company or not payload.from_date or not payload.to_date:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Missing required fields: company, fromDate, toDate",
        )

    try:
        xml = build_purchase_voucher_xml(payload.company, payload.from_date, payload.to_date)
        response = await post_to_tally(payload.host, payload.port, xml)
        records = parse_purchase_vouchers(response)

        result = TallyPurchaseResponse(
            records=records,
            count=len(records),
            company=payload.company,
            period={"fromDate": payload.from_date, "toDate": payload.to_date},
        )
        return JSONResponse(content=result.to_camel_dict())

    except ConnectionError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    except ValueError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
