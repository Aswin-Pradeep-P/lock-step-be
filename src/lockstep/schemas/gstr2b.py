from typing import Any, Literal

from pydantic import BaseModel


class Gstr2bDefectsOut(BaseModel):
    exact: int
    clerical: int
    amount_mismatch: int
    missing_in_2b: int
    itc_ineligible: int


class Gstr2bMockOut(BaseModel):
    variant: Literal["inconsistent", "corrected"]
    count: int
    defects: Gstr2bDefectsOut
    data: dict[str, Any]
