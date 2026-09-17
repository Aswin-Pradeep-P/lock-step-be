"""Section 17(5), CGST Act, 2017 — the blocked/ineligible ITC clauses.

Reference text only, handed to the AI classifier as grounding — it is not itself a
rule the matching engine runs (see `matching.py`: no LLM decides a status). Sourced
from the bare-act clause structure (indiankanoon.org's reproduction of Sec 17(5),
cross-checked against CBIC-based secondary summaries), current as of the 2019
amendment that split clause (a) into (a)/(aa)/(ab).
"""

from __future__ import annotations

SECTION_17_5_REFERENCE = """\
Section 17(5), CGST Act 2017 — ITC is NOT available on the following, subject to \
the stated exceptions. Cite the clause letter when you rely on one.

(a) Motor vehicles for transport of persons with approved seating capacity <= 13 \
(including the driver) — blocked, UNLESS used for: further supply of such \
vehicles; passenger transportation; or imparting driving training.
(aa) Vessels and aircraft — blocked, UNLESS used for: further supply; passenger \
or goods transportation; or imparting navigation/flying training.
(ab) General insurance, servicing, repair and maintenance for the vehicles/\
vessels/aircraft in (a)/(aa) — blocked UNLESS the underlying vehicle/vessel/\
aircraft itself qualifies for ITC under (a)/(aa), or the recipient is a \
manufacturer of such vehicles/vessels/aircraft, or is in the business of \
supplying general insurance for them.
(b) Food and beverages, outdoor catering, beauty treatment, health services, \
cosmetic and plastic surgery, leasing/renting/hiring of the vehicles/vessels/\
aircraft in (a)/(aa)/(ab), life insurance, health insurance, membership of a \
club, health and fitness centre, travel benefits extended to employees on \
vacation (e.g. leave/home travel concession) — blocked UNLESS: the government \
notifies the goods/services as obligatory for an employer to provide under any \
law in force, or the same category of inward supply is used to make an \
outward taxable supply of that same category (or as a component of a taxable \
composite/mixed supply).
(c) Works contract services for construction of an immovable property (other \
than plant and machinery) — blocked UNLESS it is an input service for further \
supply of works contract service.
(d) Goods or services received for construction of an immovable property \
(other than plant and machinery) on one's own account, including when used in \
the course or furtherance of business — blocked. "Construction" here includes \
re-construction, renovation, additions/alterations, or repairs, to the extent \
of capitalisation.
(e) Goods or services on which tax has been paid under the Composition Scheme \
(Section 10).
(f) Goods or services received by a non-resident taxable person — EXCEPT goods \
imported by them.
(g) Goods or services or both used for personal consumption.
(h) Goods lost, stolen, destroyed, written off, or disposed of by way of gift \
or free samples.
(i) Tax paid as a result of fraud, suppression, or wilful misstatement \
(Section 74), or on goods detained/seized in transit (Section 129), or on \
confiscated goods (Section 130).

"Plant and machinery" (for (c) and (d)) means apparatus, equipment and \
machinery fixed to earth by foundation or structural support that is used for \
making an outward supply, and excludes land, buildings, other civil \
structures, telecommunication towers, and pipelines laid outside the factory \
premises.
"""
