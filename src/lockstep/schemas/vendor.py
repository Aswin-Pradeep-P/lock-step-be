from pydantic import BaseModel


class VendorRiskOut(BaseModel):
    vendor_id: str
    name: str
    gstin: str
    total_invoices: int
    exact_count: int
    clerical_count: int
    missing_in_2b_count: int
    at_risk_amount: float
    tier: str
