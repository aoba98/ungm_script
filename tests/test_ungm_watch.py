from __future__ import annotations

import unittest
from datetime import date

from ungm_watch import Notice, apply_filters, passes_final_hard_filters


def make_notice(description: str) -> Notice:
    return Notice(
        notice_id="12345",
        title="Supply of school stationery kits",
        organization="UNICEF",
        country="Kenya",
        published_raw="05-May-26",
        deadline_raw="30-May-26",
        opportunity_type="Invitation to bid",
        reference="School supplies",
        url="https://www.ungm.org/Public/Notice/12345",
        description=description,
    )


class LocalSupplierFilterTests(unittest.TestCase):
    def test_legacy_filter_rejects_mandatory_local_supplier_requirements(self) -> None:
        notice = make_notice("Bidders must be registered local suppliers in Kenya.")

        keep, reason = apply_filters(notice, date(2026, 5, 6))

        self.assertFalse(keep)
        self.assertIn("mandatory local supplier", reason)

    def test_hard_filter_rejects_mandatory_local_supplier_requirements_before_ai(self) -> None:
        notice = make_notice("Only local suppliers are eligible to submit bids.")

        keep, reason = passes_final_hard_filters(notice, date(2026, 5, 6), set())

        self.assertFalse(keep)
        self.assertIn("mandatory local supplier", reason)


if __name__ == "__main__":
    unittest.main()
