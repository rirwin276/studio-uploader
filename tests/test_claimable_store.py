import asyncio
import importlib
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.requests import Request


os.environ.setdefault("SHOP", "example.myshopify.com")
os.environ.setdefault("API_VERSION", "2026-01")
os.environ.setdefault("CLIENT_SECRET", "test-token")

app_module = importlib.import_module("app")
provision_module = importlib.import_module("shopify_provision")


def _request(customer_id: str, secret: str = "test-secret") -> Request:
    body = json.dumps({"customer_id": customer_id}).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/storefront/test-store/join",
            "headers": [(b"x-admin-secret", secret.encode())],
        },
        receive,
    )


class StoreClaimTests(unittest.TestCase):
    def setUp(self):
        app_module._ADMIN_SECRET = "test-secret"

    def _join(self, customer_id: str):
        return asyncio.run(app_module.storefront_join("test-store", _request(customer_id)))

    def test_claimable_build_requires_admin_secret(self):
        result = asyncio.run(
            app_module.storefront_request(
                request=_request("", secret="wrong-secret"),
                customer_id="",
                customer_email="prospect@example.org",
                storefront_name="Test Store",
                storefront_handle="test-store",
                org_type=None,
                military_branch=None,
                sport_type=None,
                type_of_store_direct="soccer",
                primary_color="Charcoal",
                main_session_id=None,
                secondary_session_id=None,
                storefront_logo_file=None,
                storefront_logo_secondary=None,
                claimable=True,
            )
        )
        self.assertEqual(result.status_code, 401)

    def test_normal_build_still_requires_customer_id(self):
        result = asyncio.run(
            app_module.storefront_request(
                request=_request(""),
                customer_id="",
                customer_email="customer@example.org",
                storefront_name="Test Store",
                storefront_handle="test-store",
                org_type=None,
                military_branch=None,
                sport_type=None,
                type_of_store_direct="soccer",
                primary_color="Charcoal",
                main_session_id=None,
                secondary_session_id=None,
                storefront_logo_file=None,
                storefront_logo_secondary=None,
                claimable=False,
            )
        )
        self.assertEqual(result.status_code, 400)
        self.assertIn("customer_id is required", result.body.decode())

    def test_normal_store_still_grants_member_only(self):
        store = {
            "id": "gid://shopify/Metaobject/1",
            "fields": {
                "owner_customer_id": "101",
                "collection_gid": "gid://shopify/Collection/1",
            },
        }
        with (
            patch.object(app_module, "_get_customer_tags", return_value=[]),
            patch.object(app_module, "_get_custom_shop", return_value=store),
            patch.object(app_module, "_get_collection_claim_owner") as marker,
            patch.object(app_module, "_customer_add_tag") as add_tag,
        ):
            result = self._join("202")

        self.assertEqual(result["role"], "member")
        self.assertFalse(result["claimed_admin"])
        marker.assert_not_called()
        add_tag.assert_called_once_with(
            "gid://shopify/Customer/202",
            "storefront-member--test-store",
        )

    def test_first_claimant_gets_admin_and_member(self):
        store = {
            "id": "gid://shopify/Metaobject/1",
            "fields": {
                "owner_customer_id": "unclaimed",
                "collection_gid": "gid://shopify/Collection/1",
            },
        }
        with (
            patch.object(app_module, "_get_customer_tags", return_value=[]),
            patch.object(app_module, "_get_custom_shop", return_value=store),
            patch.object(app_module, "_get_collection_claim_owner", return_value=""),
            patch.object(app_module, "_try_create_collection_claim", return_value=True),
            patch.object(app_module, "_set_custom_shop_owner") as set_owner,
            patch.object(app_module, "_customer_add_tag") as add_tag,
        ):
            result = self._join("101")

        self.assertEqual(result["role"], "admin")
        self.assertTrue(result["claimed_admin"])
        set_owner.assert_called_once_with("gid://shopify/Metaobject/1", "101")
        self.assertEqual(
            add_tag.call_args_list,
            [
                unittest.mock.call(
                    "gid://shopify/Customer/101",
                    "storefront-admin--test-store",
                ),
                unittest.mock.call(
                    "gid://shopify/Customer/101",
                    "storefront-member--test-store",
                ),
            ],
        )

    def test_later_claimant_gets_member_only(self):
        store = {
            "id": "gid://shopify/Metaobject/1",
            "fields": {
                "owner_customer_id": "unclaimed",
                "collection_gid": "gid://shopify/Collection/1",
            },
        }
        with (
            patch.object(app_module, "_get_customer_tags", return_value=[]),
            patch.object(app_module, "_get_custom_shop", return_value=store),
            patch.object(app_module, "_get_collection_claim_owner", return_value="101"),
            patch.object(app_module, "_try_create_collection_claim") as try_claim,
            patch.object(app_module, "_set_custom_shop_owner") as set_owner,
            patch.object(app_module, "_customer_add_tag") as add_tag,
        ):
            result = self._join("202")

        self.assertEqual(result["role"], "member")
        self.assertFalse(result["claimed_admin"])
        try_claim.assert_not_called()
        set_owner.assert_called_once_with("gid://shopify/Metaobject/1", "101")
        add_tag.assert_called_once_with(
            "gid://shopify/Customer/202",
            "storefront-member--test-store",
        )

    def test_claim_owner_retry_repairs_missing_admin_tag(self):
        store = {
            "id": "gid://shopify/Metaobject/1",
            "fields": {
                "owner_customer_id": "101",
                "collection_gid": "gid://shopify/Collection/1",
            },
        }
        with (
            patch.object(app_module, "_get_customer_tags", return_value=[]),
            patch.object(app_module, "_get_custom_shop", return_value=store),
            patch.object(app_module, "_get_collection_claim_owner", return_value="101"),
            patch.object(app_module, "_customer_add_tag") as add_tag,
        ):
            result = self._join("101")

        self.assertEqual(result["role"], "admin")
        self.assertEqual(add_tag.call_count, 2)

    def test_atomic_claim_conflict_is_a_normal_loss(self):
        with patch.object(
            app_module,
            "_shopify_graphql",
            return_value={
                "metafieldsSet": {
                    "metafields": [],
                    "userErrors": [
                        {
                            "field": ["metafields", "0", "compareDigest"],
                            "message": "The metafield has been modified since it was loaded.",
                            "code": "STALE_OBJECT",
                        }
                    ],
                }
            },
        ):
            won = app_module._try_create_collection_claim(
                "gid://shopify/Collection/1",
                "101",
            )
        self.assertFalse(won)


class ClaimableProvisionTests(unittest.TestCase):
    def test_ownerless_provision_skips_customer_tags(self):
        with (
            patch.object(provision_module, "read_session_png", return_value=b"png"),
            patch.object(
                provision_module,
                "upload_png_to_shopify_files",
                return_value=("gid://shopify/MediaImage/1", "https://cdn/logo.png"),
            ),
            patch.object(provision_module, "collection_by_handle", return_value=None),
            patch.object(
                provision_module,
                "collection_create_smart",
                return_value="gid://shopify/Collection/1",
            ),
            patch.object(provision_module, "publish_to_all_publications"),
            patch.object(provision_module, "customer_add_tags") as add_tags,
            patch.object(
                provision_module,
                "metaobject_upsert_custom_shop",
                return_value="gid://shopify/Metaobject/1",
            ) as upsert,
            patch.object(
                provision_module,
                "trigger_printful_automation",
                return_value="https://automation/run",
            ),
        ):
            result = provision_module.provision(
                storefront_name="Test Store",
                storefront_handle="test-store",
                owner_customer_id="",
                main_session_id="session",
                uploads_dir=Path("/tmp"),
                type_of_store="soccer",
                primary_color="Charcoal",
            )

        add_tags.assert_not_called()
        self.assertEqual(
            upsert.call_args.kwargs["owner_customer_id_text"],
            provision_module.UNCLAIMED_OWNER_VALUE,
        )
        self.assertEqual(result["customer_tags_added"], [])


if __name__ == "__main__":
    unittest.main()
