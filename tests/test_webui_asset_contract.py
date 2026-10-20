from __future__ import annotations

import unittest
from uuid import UUID

from webui_asset_contract import (
    ASSET_EXPORT_REQUEST_FORMAT,
    AssetExportRequest,
    parse_asset_export_request,
)


REQUEST_ID = "00000000-0000-4000-8000-000000000001"


class AssetExportContractTests(unittest.TestCase):
    def test_weapon_request_requires_and_serializes_supported_variant(self) -> None:
        request = parse_asset_export_request({
            "format": ASSET_EXPORT_REQUEST_FORMAT,
            "request_id": REQUEST_ID,
            "kind": "weapon",
            "catalog_id": "weapons-v1",
            "item_ids": ["claym_0004", "sword_0006"],
            "variant": "refined_candidate",
        })

        self.assertEqual(request, AssetExportRequest(
            ASSET_EXPORT_REQUEST_FORMAT,
            UUID(REQUEST_ID),
            "weapon",
            "weapons-v1",
            ("claym_0004", "sword_0006"),
            "refined_candidate",
        ))
        self.assertEqual(request.item_ids, ("claym_0004", "sword_0006"))
        self.assertEqual(request.as_json(), {
            "format": ASSET_EXPORT_REQUEST_FORMAT,
            "request_id": REQUEST_ID,
            "kind": "weapon",
            "catalog_id": "weapons-v1",
            "item_ids": ["claym_0004", "sword_0006"],
            "variant": "refined_candidate",
        })

    def test_weapon_base_variant_is_supported(self) -> None:
        request = parse_asset_export_request({
            "format": ASSET_EXPORT_REQUEST_FORMAT,
            "request_id": REQUEST_ID,
            "kind": "weapon",
            "catalog_id": "weapons-v1",
            "item_ids": ["claym_0004"],
            "variant": "base",
        })

        self.assertEqual(request.variant, "base")

    def test_request_id_accepts_uuid_case_and_serializes_canonically(self) -> None:
        request = parse_asset_export_request({
            "format": ASSET_EXPORT_REQUEST_FORMAT,
            "request_id": REQUEST_ID.upper(),
            "kind": "illustration",
            "catalog_id": "illustrations-v1",
            "item_ids": ["chr-0001"],
        })

        self.assertEqual(request.request_id, UUID(REQUEST_ID))
        self.assertEqual(request.as_json()["request_id"], REQUEST_ID)

    def test_non_weapon_requests_omit_variant_and_strip_unknown_fields(self) -> None:
        for kind in ("illustration", "display_prop"):
            with self.subTest(kind=kind):
                request = parse_asset_export_request({
                    "format": ASSET_EXPORT_REQUEST_FORMAT,
                    "request_id": REQUEST_ID,
                    "kind": kind,
                    "catalog_id": "catalog-v1",
                    "item_ids": ("item-1",),
                    "extra": {"ignored": True},
                })

                self.assertIsNone(request.variant)
                self.assertEqual(request.as_json(), {
                    "format": ASSET_EXPORT_REQUEST_FORMAT,
                    "request_id": REQUEST_ID,
                    "kind": kind,
                    "catalog_id": "catalog-v1",
                    "item_ids": ["item-1"],
                })

    def test_weapon_variant_is_required_and_limited(self) -> None:
        payload = {
            "format": ASSET_EXPORT_REQUEST_FORMAT,
            "request_id": REQUEST_ID,
            "kind": "weapon",
            "catalog_id": "catalog-v1",
            "item_ids": ["item-1"],
        }
        invalid_requests = (
            payload,
            {**payload, "variant": "preview"},
            {**payload, "variant": None},
        )
        for invalid in invalid_requests:
            with self.subTest(variant=invalid.get("variant")), self.assertRaises(ValueError):
                parse_asset_export_request(invalid)

    def test_non_weapon_requests_reject_any_variant_field(self) -> None:
        for kind in ("illustration", "display_prop"):
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "variant is not supported"):
                parse_asset_export_request({
                    "format": ASSET_EXPORT_REQUEST_FORMAT,
                    "request_id": REQUEST_ID,
                    "kind": kind,
                    "catalog_id": "catalog-v1",
                    "item_ids": ["item-1"],
                    "variant": "base",
                })

    def test_required_fields_and_values_are_strict(self) -> None:
        invalid_requests = (
            None,
            [],
            {
                "format": ASSET_EXPORT_REQUEST_FORMAT,
                "request_id": REQUEST_ID,
                "kind": "creature",
                "catalog_id": "catalog-v1",
                "item_ids": ["item-1"],
            },
            {
                "format": ASSET_EXPORT_REQUEST_FORMAT,
                "request_id": REQUEST_ID,
                "kind": "weapon",
                "catalog_id": "",
                "item_ids": ["item-1"],
                "variant": "base",
            },
            {
                "format": ASSET_EXPORT_REQUEST_FORMAT,
                "request_id": REQUEST_ID,
                "kind": 3,
                "catalog_id": "catalog-v1",
                "item_ids": ["item-1"],
                "variant": "base",
            },
            {
                "format": ASSET_EXPORT_REQUEST_FORMAT,
                "request_id": REQUEST_ID,
                "kind": "weapon",
                "catalog_id": "catalog-v1",
                "item_ids": ["item-1"],
                "variant": 4,
            },
            {
                "format": "AssetExportRequest/2",
                "request_id": REQUEST_ID,
                "kind": "illustration",
                "catalog_id": "catalog-v1",
                "item_ids": ["item-1"],
            },
            {
                "format": ASSET_EXPORT_REQUEST_FORMAT,
                "request_id": "not-a-uuid",
                "kind": "illustration",
                "catalog_id": "catalog-v1",
                "item_ids": ["item-1"],
            },
            {
                "format": ASSET_EXPORT_REQUEST_FORMAT,
                "request_id": 17,
                "kind": "illustration",
                "catalog_id": "catalog-v1",
                "item_ids": ["item-1"],
            },
        )
        for invalid in invalid_requests:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_asset_export_request(invalid)

    def test_item_ids_must_be_non_empty_unique_strings(self) -> None:
        base = {
            "format": ASSET_EXPORT_REQUEST_FORMAT,
            "request_id": REQUEST_ID,
            "kind": "illustration",
            "catalog_id": "catalog-v1",
        }
        invalid_values = ([], "item-1", ["item-1", "item-1"], ["item-1", 2], [""])
        for item_ids in invalid_values:
            with self.subTest(item_ids=item_ids), self.assertRaises(ValueError):
                parse_asset_export_request({**base, "item_ids": item_ids})

    def test_missing_fields_and_non_string_keys_are_rejected(self) -> None:
        valid_request = {
            "format": ASSET_EXPORT_REQUEST_FORMAT,
            "request_id": REQUEST_ID,
            "kind": "illustration",
            "catalog_id": "catalog-v1",
            "item_ids": ["item-1"],
        }
        invalid_requests = tuple(
            {key: value for key, value in valid_request.items() if key != missing}
            for missing in ("format", "request_id", "kind", "catalog_id", "item_ids")
        ) + ({**valid_request, 9: "extra"},)
        for invalid in invalid_requests:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_asset_export_request(invalid)


if __name__ == "__main__":
    unittest.main()
