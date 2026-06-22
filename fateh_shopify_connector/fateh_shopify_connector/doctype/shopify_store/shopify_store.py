# Copyright (c) 2024, siva@enfono.com and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from fateh_shopify_connector.fateh_shopify_connector.connection import (
	DEFAULT_API_VERSION,
	WEBHOOK_EVENT_FLAGS,
	WEBHOOK_EVENTS,
	get_access_token,
)
from fateh_shopify_connector.fateh_shopify_connector.oauth import get_callback_url
from fateh_shopify_connector.fateh_shopify_connector.utils import create_shopify_log
from fateh_shopify_connector.utils.logger import get_logger


class ShopifyStore(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from fateh_shopify_connector.fateh_shopify_connector.doctype.shopify_store_collection_mapping.shopify_store_collection_mapping import (
			ShopifyStoreCollectionMapping,
		)
		from fateh_shopify_connector.fateh_shopify_connector.doctype.shopify_store_item_field.shopify_store_item_field import (
			ShopifyStoreItemField,
		)
		from fateh_shopify_connector.fateh_shopify_connector.doctype.shopify_store_item_filter.shopify_store_item_filter import (
			ShopifyStoreItemFilter,
		)
		from fateh_shopify_connector.fateh_shopify_connector.doctype.shopify_store_payment_method_mapping.shopify_store_payment_method_mapping import (
			ShopifyStorePaymentMethodMapping,
		)
		from fateh_shopify_connector.fateh_shopify_connector.doctype.shopify_store_tax_account.shopify_store_tax_account import (
			ShopifyStoreTaxAccount,
		)
		from fateh_shopify_connector.fateh_shopify_connector.doctype.shopify_store_warehouse_mapping.shopify_store_warehouse_mapping import (
			ShopifyStoreWarehouseMapping,
		)

		access_token: DF.Data | None
		add_shipping_as_item: DF.Check
		api_version: DF.Data | None
		auth_method: DF.Data
		auto_create_collections: DF.Check
		auto_create_invoice: DF.Check
		auto_create_payment_entry: DF.Check
		auto_fulfill_remaining_qty: DF.Check
		auto_submit_sales_order: DF.Check
		callback_url: DF.Data | None
		client_id: DF.Data | None
		client_secret: DF.Password | None
		collection_mapping: DF.Table[ShopifyStoreCollectionMapping]
		company: DF.Link
		connected_user: DF.Link | None
		cost_center: DF.Link | None
		customer_group: DF.Link | None
		default_customer: DF.Link | None
		default_sales_tax_account: DF.Link | None
		default_shipping_charges_account: DF.Link | None
		delivery_note_series: DF.Literal[None]
		enable_image_sync: DF.Check
		enable_inventory_sync: DF.Check
		enable_item_sync: DF.Check
		enable_webhook_fulfillment: DF.Check
		enable_webhook_orders_cancelled: DF.Check
		enable_webhook_orders_create: DF.Check
		enable_webhook_orders_paid: DF.Check
		enabled: DF.Check
		inventory_sync_frequency: DF.Int
		inventory_sync_mode: DF.Data
		item_field_map: DF.Table[ShopifyStoreItemField]
		item_filters: DF.Table[ShopifyStoreItemFilter]
		item_group: DF.Link | None
		last_inventory_sync: DF.Datetime | None
		last_order_sync: DF.Datetime | None
		oauth_status: DF.Data | None
		payment_method_mapping: DF.Table[ShopifyStorePaymentMethodMapping]
		price_list: DF.Link | None
		sales_invoice_series: DF.Literal[None]
		sales_order_series: DF.Literal[None]
		shared_secret: DF.Data | None
		shipping_item: DF.Link | None
		shop_domain: DF.Data
		shop_domain_alias: DF.Data | None
		sync_all_order_statuses: DF.Check
		sync_orders: DF.Check
		tax_accounts: DF.Table[ShopifyStoreTaxAccount]
		update_shopify_on_item_update: DF.Check
		warehouse: DF.Link | None
		warehouse_mapping: DF.Table[ShopifyStoreWarehouseMapping]
		write_off_account: DF.Link | None

	# end: auto-generated types

	def validate(self):
		self.normalize_shop_domain()
		self.normalize_shop_domain_alias()
		self.validate_shop_domain_alias()
		self.validate_auth_method()
		self.validate_payment_method_mapping()

	def normalize_shop_domain(self):
		"""Normalize shop domain to just the domain without protocol or trailing slashes."""
		if self.shop_domain:
			domain = self.shop_domain.strip()
			for prefix in ["https://", "http://"]:
				if domain.startswith(prefix):
					domain = domain[len(prefix):]
			domain = domain.rstrip("/")
			if domain.endswith("/admin"):
				domain = domain[:-6]
			self.shop_domain = domain

	def normalize_shop_domain_alias(self):
		"""Normalize shop domain alias using same logic as shop_domain."""
		if self.get("shop_domain_alias"):
			domain = self.shop_domain_alias.strip()
			for prefix in ["https://", "http://"]:
				if domain.startswith(prefix):
					domain = domain[len(prefix):]
			domain = domain.rstrip("/")
			if domain.endswith("/admin"):
				domain = domain[:-6]
			self.shop_domain_alias = domain

	def validate_shop_domain_alias(self):
		"""Validate that shop_domain_alias is unique across all stores."""
		if not self.get("shop_domain_alias"):
			return

		if self.shop_domain_alias == self.shop_domain:
			frappe.throw(_("Shop Domain Alias cannot be the same as Shop Domain"))

		existing = frappe.db.get_value(
			"Shopify Store",
			{"shop_domain": self.shop_domain_alias, "name": ["!=", self.name]},
		)
		if existing:
			frappe.throw(_("This alias is already used as Shop Domain in store: {0}").format(existing))

		existing = frappe.db.get_value(
			"Shopify Store",
			{"shop_domain_alias": self.shop_domain_alias, "name": ["!=", self.name]},
		)
		if existing:
			frappe.throw(_("This alias is already used in store: {0}").format(existing))

	def validate_auth_method(self):
		"""Validate fields based on authentication method."""
		auth_method = self.auth_method or "OAuth"

		if auth_method == "OAuth":
			self.callback_url = get_callback_url()
			# Do NOT touch access_token here — it lives in Frappe's __Auth table,
			# managed exclusively by oauth.callback() via update_password().
			# Clearing it would wipe the token on every save.
		else:
			# Legacy (Access Token) — clear OAuth-specific fields
			self.client_id = None
			self.client_secret = None
			self.callback_url = None
			self.connected_user = None
			if self.oauth_status != "Not Connected":
				self.oauth_status = "Not Connected"

	def validate_payment_method_mapping(self):
		"""Validate that there are no duplicate Shopify gateways in payment method mapping."""
		if not self.payment_method_mapping:
			return

		seen_gateways = set()
		for row in self.payment_method_mapping:
			if row.shopify_gateway in seen_gateways:
				frappe.throw(
					_(
						"Duplicate payment method mapping for Shopify gateway '{0}'. Each gateway can only be mapped once."
					).format(row.shopify_gateway)
				)
			seen_gateways.add(row.shopify_gateway)

	def on_update(self):
		pass

	def _get_auth_details(self):
		"""Get authentication details for Shopify API session."""
		api_version = self.api_version or DEFAULT_API_VERSION
		access_token = get_access_token(self)
		return (self.shop_domain, api_version, access_token)

	def _init_shopify_api_versions(self):
		"""Initialize Shopify API versions by fetching from Shopify."""
		from shopify.api_version import ApiVersion

		if not ApiVersion.versions:
			ApiVersion.fetch_known_versions()

	@frappe.whitelist()
	def test_connection(self):
		"""Test the Shopify API connection."""
		from shopify.resources import Shop
		from shopify.session import Session

		logger = get_logger()
		try:
			self._init_shopify_api_versions()
			auth_details = self._get_auth_details()

			with Session.temp(*auth_details):
				shop = Shop.current()

				frappe.msgprint(
					_("Connection successful!")
					+ "<br><br>"
					+ _("<b>Shop Name:</b> {0}").format(shop.name)
					+ "<br>"
					+ _("<b>Domain:</b> {0}").format(shop.domain)
					+ "<br>"
					+ _("<b>Email:</b> {0}").format(shop.email)
					+ "<br>"
					+ _("<b>Currency:</b> {0}").format(shop.currency)
					+ "<br>"
					+ _("<b>Plan:</b> {0}").format(shop.plan_name),
					title=_("Shopify Connection Test"),
					indicator="green",
				)
				logger.info("Connection successful! Shopify store: %s", self.shop_domain)

		except frappe.ValidationError:
			# Auth/config errors already have a clear message — re-raise as-is
			raise
		except Exception as e:
			logger.error(
				"Connection failed for Shopify store: %s, error: %s", self.shop_domain, str(e), exc_info=True
			)
			frappe.log_error(
				message=frappe.get_traceback(),
				title=_("Shopify Connection Test Failed - {0}").format(self.shop_domain),
			)
			frappe.db.commit()
			frappe.throw(_("Connection failed: {0}").format(str(e)), title=_("Shopify Connection Error"))

	@frappe.whitelist()
	def fetch_shopify_locations(self):
		"""Fetch locations from Shopify and return them for the JS to populate the table."""
		from shopify.resources import Location
		from shopify.session import Session

		logger = get_logger()
		logger.info("Fetching locations from Shopify for store: %s", self.shop_domain)
		try:
			self._init_shopify_api_versions()
			auth_details = self._get_auth_details()

			with Session.temp(*auth_details):
				locations = Location.find()

				existing_mappings = {
					row.shopify_location_id: row.erpnext_warehouse for row in self.warehouse_mapping
				}

				locations_data = []
				for location in locations:
					location_id = str(location.id)
					locations_data.append(
						{
							"shopify_location_id": location_id,
							"shopify_location_name": location.name,
							"erpnext_warehouse": existing_mappings.get(location_id) or "",
						}
					)

				logger.info(
					"Successfully fetched %s locations from Shopify for store: %s",
					len(locations_data),
					self.shop_domain,
				)

				frappe.msgprint(
					_("Successfully fetched {0} location(s) from Shopify.").format(len(locations_data))
					+ "<br><br>"
					+ _("Please map each Shopify location to a warehouse and save the document."),
					title=_("Shopify Locations"),
					indicator="green",
				)

				return locations_data

		except Exception as e:
			logger.error(
				"Failed to fetch locations for Shopify store: %s, error: %s",
				self.shop_domain,
				str(e),
				exc_info=True,
			)
			frappe.log_error(
				message=frappe.get_traceback(),
				title=_("Fetch Shopify Locations Failed - {0}").format(self.shop_domain),
			)
			frappe.db.commit()
			frappe.throw(_("Failed to fetch locations: {0}").format(str(e)), title=_("Shopify Error"))

	@frappe.whitelist()
	def fetch_products_and_map_by_sku(self):
		"""Enqueue background job to fetch products from Shopify and auto-map by SKU."""
		frappe.enqueue(
			"fateh_shopify_connector.fateh_shopify_connector.doctype.shopify_store.shopify_store._fetch_products_and_map_by_sku_job",
			queue="long",
			timeout=1800,
			job_id=f"sku_mapping_{self.name}",
			deduplicate=True,
			store_name=self.name,
			initiating_user=frappe.session.user,
		)
		frappe.msgprint(
			_("SKU mapping has been queued for {0}. You will be notified when it completes.").format(
				self.shop_domain
			),
			title=_("Shopify SKU Mapping"),
			indicator="blue",
		)

	@frappe.whitelist()
	def sync_all_items(self):
		"""Manual trigger to sync all eligible items to this Shopify store."""
		if not self.enabled:
			frappe.throw(_("Store is not enabled"))

		if not self.enable_item_sync:
			frappe.throw(_("Item sync is not enabled for this store"))

		from fateh_shopify_connector.fateh_shopify_connector.product import sync_items_to_store

		count = sync_items_to_store(self.name, initiating_user=frappe.session.user)
		if count:
			frappe.msgprint(
				_("Syncing {0} items to Shopify. Watch the live progress in the dialog.").format(count),
				title=_("Item Sync Started"),
				indicator="blue",
			)

	@frappe.whitelist()
	def sync_inventory(self):
		"""Manual trigger to sync all inventory to this Shopify store."""
		from fateh_shopify_connector.fateh_shopify_connector.inventory import manual_inventory_sync

		manual_inventory_sync(self.name)

	@frappe.whitelist()
	def fetch_shopify_collections(self):
		"""Fetch all collections (custom and smart) from Shopify."""
		from shopify.resources import CustomCollection, SmartCollection
		from shopify.session import Session

		logger = get_logger()
		logger.info("Fetching collections from Shopify for store: %s", self.shop_domain)
		try:
			self._init_shopify_api_versions()
			auth_details = self._get_auth_details()

			with Session.temp(*auth_details):
				custom_collections = CustomCollection.find()
				smart_collections = SmartCollection.find()

				existing_mappings = {
					row.shopify_collection_id: row.field_value for row in self.collection_mapping
				}

				collections_data = []

				for collection in custom_collections:
					collection_id = str(collection.id)
					collections_data.append(
						{
							"shopify_collection_id": collection_id,
							"shopify_collection_title": collection.title,
							"field_value": existing_mappings.get(collection_id) or collection.title,
						}
					)

				for collection in smart_collections:
					collection_id = str(collection.id)
					collections_data.append(
						{
							"shopify_collection_id": collection_id,
							"shopify_collection_title": f"{collection.title} (Smart)",
							"field_value": existing_mappings.get(collection_id) or collection.title,
						}
					)

				frappe.msgprint(
					_("Successfully fetched {0} collection(s) from Shopify.").format(len(collections_data))
					+ "<br><br>"
					+ _("Review the Field Value for each collection, then save the document."),
					title=_("Shopify Collections"),
					indicator="green",
				)

				return collections_data

		except Exception as e:
			logger.error(
				"Failed to fetch collections for Shopify store: %s, error: %s",
				self.shop_domain,
				str(e),
				exc_info=True,
			)
			frappe.log_error(
				message=frappe.get_traceback(),
				title=_("Fetch Shopify Collections Failed - {0}").format(self.shop_domain),
			)
			frappe.db.commit()
			frappe.throw(_("Failed to fetch collections: {0}").format(str(e)), title=_("Shopify Error"))

	@frappe.whitelist()
	def fetch_and_sync_orders(self):
		"""Manual trigger to sync new orders from Shopify."""
		logger = get_logger()
		logger.info("Manual trigger to sync new orders from Shopify for store: %s", self.shop_domain)
		if not self.enabled:
			logger.warning("Shopify store: %s is not enabled", self.shop_domain)
			frappe.throw(_("Store is not enabled"))

		from fateh_shopify_connector.fateh_shopify_connector.order import sync_new_orders

		logger.info("Syncing new orders from Shopify for store: %s", self.shop_domain)
		result = sync_new_orders(self.name)

		message = _("Order sync completed.") + "<br><br>"
		message += _("<b>Synced:</b> {0} orders").format(result["synced"]) + "<br>"
		message += _("<b>Skipped:</b> {0} (already exist)").format(result["skipped"]) + "<br>"
		message += _("<b>Errors:</b> {0}").format(result["errors"])

		if result["errors"] > 0:
			message += "<br><br>" + _("Check Error Log for details on failed orders.")
			logger.warning(
				"Order sync completed for store: %s, errors: %s", self.shop_domain, result["errors"]
			)

		logger.info("Order sync completed for store: %s, result: %s", self.shop_domain, result)

		frappe.msgprint(
			message,
			title=_("Shopify Order Sync"),
			indicator="green" if result["errors"] == 0 else "orange",
		)

	def get_expected_webhook_topics(self) -> list[str]:
		"""Get the list of webhook topics that should be registered based on store settings."""
		return [event for event in WEBHOOK_EVENTS if getattr(self, WEBHOOK_EVENT_FLAGS.get(event, ""), True)]

	@frappe.whitelist()
	def register_webhooks(self):
		"""Register webhooks with Shopify for this store."""
		logger = get_logger()
		logger.info("Registering webhooks for Shopify store: %s", self.shop_domain)

		if not self.enabled:
			frappe.throw(_("Store is not enabled"))

		from fateh_shopify_connector.fateh_shopify_connector.connection import register_webhooks

		try:
			webhooks = register_webhooks(self)
			webhook_topics = [w.topic for w in webhooks]
			expected_topics = self.get_expected_webhook_topics()
			missing_topics = [t for t in expected_topics if t not in webhook_topics]

			if len(webhook_topics) == len(expected_topics) and not missing_topics:
				logger.info(
					"Successfully registered %s webhook(s) for store %s: %s",
					len(webhooks),
					self.shop_domain,
					webhook_topics,
				)
				frappe.msgprint(
					_("Registered {0} webhook(s) with Shopify:").format(len(webhooks))
					+ "<br><br>"
					+ "<br>".join(f"• {topic}" for topic in webhook_topics),
					title=_("Webhooks Registered"),
					indicator="green",
				)
			elif len(webhook_topics) == 0:
				logger.error(
					"Failed to register any webhooks for store %s: expected %s",
					self.shop_domain,
					expected_topics,
				)
				frappe.msgprint(
					_("Failed to register any webhooks with Shopify.")
					+ "<br><br>"
					+ _("<b>Expected:</b>")
					+ "<br>"
					+ "<br>".join(f"• {topic}" for topic in expected_topics)
					+ "<br><br>"
					+ _("Check Fateh Shopify Log for error details."),
					title=_("Webhook Registration Failed"),
					indicator="red",
				)
			else:
				logger.warning(
					"Partially registered webhooks for store %s: registered %s, missing %s",
					self.shop_domain,
					webhook_topics,
					missing_topics,
				)
				frappe.msgprint(
					_("Partially registered webhooks with Shopify.")
					+ "<br><br>"
					+ _("<b>Registered ({0}):</b>").format(len(webhook_topics))
					+ "<br>"
					+ "<br>".join(f"• {topic}" for topic in webhook_topics)
					+ "<br><br>"
					+ _("<b>Missing ({0}):</b>").format(len(missing_topics))
					+ "<br>"
					+ "<br>".join(f"• {topic}" for topic in missing_topics)
					+ "<br><br>"
					+ _("Check Fateh Shopify Log for error details."),
					title=_("Webhook Registration Incomplete"),
					indicator="orange",
				)

		except Exception as e:
			logger.error(
				"Failed to register webhooks for store %s: %s",
				self.shop_domain,
				str(e),
				exc_info=True,
			)
			frappe.log_error(
				message=frappe.get_traceback(),
				title=_("Webhook Registration Failed - {0}").format(self.shop_domain),
			)
			frappe.db.commit()
			frappe.throw(_("Failed to register webhooks: {0}").format(str(e)))

	@frappe.whitelist()
	def fetch_webhooks(self):
		"""Fetch registered webhooks from Shopify for this site."""
		from shopify.resources import Webhook
		from shopify.session import Session

		from fateh_shopify_connector.fateh_shopify_connector.connection import get_current_domain_name

		logger = get_logger()
		logger.info("Fetching webhooks from Shopify for store: %s", self.shop_domain)

		if not self.enabled:
			frappe.throw(_("Store is not enabled"))

		try:
			self._init_shopify_api_versions()
			auth_details = self._get_auth_details()

			with Session.temp(*auth_details):
				webhooks = Webhook.find()

				url = get_current_domain_name()
				site_webhooks = [
					{"topic": w.topic, "id": w.id, "address": w.address}
					for w in webhooks
					if url in w.address
				]

				logger.info(
					"Fetched %s webhook(s) for store %s",
					len(site_webhooks),
					self.shop_domain,
				)

				return site_webhooks

		except Exception as e:
			logger.error(
				"Failed to fetch webhooks for store %s: %s",
				self.shop_domain,
				str(e),
				exc_info=True,
			)
			frappe.log_error(
				message=frappe.get_traceback(),
				title=_("Fetch Webhooks Failed - {0}").format(self.shop_domain),
			)
			frappe.db.commit()
			frappe.throw(_("Failed to fetch webhooks: {0}").format(str(e)))


def _build_erpnext_sku_set() -> set[str]:
	"""Build a set of all non-disabled item_code values for O(1) SKU lookups."""
	return set(frappe.get_all("Item", filters={"disabled": 0}, pluck="item_code"))


def _fetch_products_and_map_by_sku_job(store_name: str, initiating_user: str | None = None):
	"""Background job: fetch products from Shopify and auto-map by SKU to ERPNext Items."""
	from shopify.collection import PaginatedIterator
	from shopify.resources import Product
	from shopify.session import Session

	logger = get_logger()
	logger.info("Starting SKU mapping job for store: %s", store_name)

	store = frappe.get_doc("Shopify Store", store_name)
	frappe.flags.in_sku_mapping = True

	try:
		store._init_shopify_api_versions()
		auth_details = store._get_auth_details()

		erpnext_skus = _build_erpnext_sku_set()
		logger.info(
			"Built ERPNext SKU set with %d entries for store: %s",
			len(erpnext_skus),
			store.shop_domain,
		)

		total_variants = 0
		skipped_no_sku = 0
		not_found = 0
		updated = 0
		created = 0
		errors = 0

		with Session.temp(*auth_details):
			products_iter = PaginatedIterator(Product.find(limit=250))

			for products_batch in products_iter:
				for product in products_batch:
					for variant in product.variants:
						total_variants += 1
						sku = (variant.sku or "").strip()

						if not sku:
							skipped_no_sku += 1
							continue

						if sku not in erpnext_skus:
							not_found += 1
							logger.info(
								"SKU '%s' (product %s, variant %s) not found in ERPNext",
								sku,
								product.id,
								variant.id,
							)
							continue

						try:
							action = _upsert_item_store_mapping(
								item_code=sku,
								store_name=store.name,
								product_id=str(product.id),
								variant_id=str(variant.id),
								sku=sku,
								inventory_item_id=(
									str(variant.inventory_item_id)
									if getattr(variant, "inventory_item_id", None)
									else None
								),
							)
							if action == "updated":
								updated += 1
							else:
								created += 1
							frappe.db.commit()  # nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- per-variant independent transaction
						except Exception:
							errors += 1
							frappe.db.rollback()
							logger.error("Failed to map SKU '%s'", sku, exc_info=True)
							frappe.log_error(
								message=frappe.get_traceback(),
								title=_("SKU Mapping Error - {0}").format(store.shop_domain),
							)
							frappe.db.commit()  # nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- persist error log after rollback

		matched = updated + created
		logger.info(
			"SKU mapping complete for store %s: matched=%d, updated=%d, created=%d, "
			"skipped_no_sku=%d, not_found=%d, errors=%d",
			store.shop_domain,
			matched,
			updated,
			created,
			skipped_no_sku,
			not_found,
			errors,
		)

		message = _("SKU mapping complete for {0}.").format(store.shop_domain) + "<br><br>"
		message += _("<b>Total Shopify variants scanned:</b> {0}").format(total_variants) + "<br>"
		message += _("<b>Matched to ERPNext items:</b> {0}").format(matched) + "<br>"
		message += _("<b>&nbsp;&nbsp;— Updated existing:</b> {0}").format(updated) + "<br>"
		message += _("<b>&nbsp;&nbsp;— Created new:</b> {0}").format(created) + "<br>"
		message += _("<b>Skipped (no SKU on Shopify):</b> {0}").format(skipped_no_sku) + "<br>"
		message += _("<b>Not found in ERPNext:</b> {0}").format(not_found) + "<br>"
		message += _("<b>Errors:</b> {0}").format(errors)

		if errors > 0:
			message += "<br><br>" + _("Check Error Log for details on failed mappings.")

		create_shopify_log(
			status="Success" if errors == 0 else "Warning",
			method="fetch_products_and_map_by_sku",
			shopify_store=store.name,
			message=message,
			reference_doctype="Shopify Store",
			reference_name=store.name,
		)

		if initiating_user:
			frappe.publish_realtime(
				"msgprint",
				{
					"message": message,
					"title": _("Shopify SKU Mapping"),
					"indicator": "green" if errors == 0 else "orange",
				},
				user=initiating_user,
			)

	except Exception as e:
		logger.error(
			"Failed to fetch products and map by SKU for store: %s, error: %s",
			store.shop_domain,
			str(e),
			exc_info=True,
		)
		frappe.db.rollback()
		frappe.log_error(
			message=frappe.get_traceback(),
			title=_("Fetch Products & Map by SKU Failed - {0}").format(store.shop_domain),
		)
		create_shopify_log(
			status="Error",
			method="fetch_products_and_map_by_sku",
			shopify_store=store.name,
			message=_("SKU mapping failed for {0}: {1}").format(store.shop_domain, str(e)),
			exception=frappe.get_traceback(),
			reference_doctype="Shopify Store",
			reference_name=store.name,
		)

		if initiating_user:
			frappe.publish_realtime(
				"msgprint",
				{
					"message": _("SKU mapping failed for {0}: {1}").format(store.shop_domain, str(e)),
					"title": _("Shopify SKU Mapping"),
					"indicator": "red",
				},
				user=initiating_user,
			)
	finally:
		frappe.flags.in_sku_mapping = False


def _upsert_item_store_mapping(
	item_code: str,
	store_name: str,
	product_id: str,
	variant_id: str,
	sku: str,
	inventory_item_id: str | None = None,
) -> str:
	"""Create or update an Item Shopify Store mapping row for a specific variant.

	Returns "updated" or "created".
	"""
	item = frappe.get_doc("Item", item_code)

	update_data = {
		"shopify_product_id": product_id,
		"shopify_variant_id": variant_id,
		"shopify_sku": sku,
	}
	if inventory_item_id:
		update_data["shopify_inventory_item_id"] = inventory_item_id

	exact_row = None
	blank_row = None
	for row in getattr(item, "shopify_stores", []) or []:
		if row.shopify_store != store_name:
			continue
		if row.shopify_variant_id == variant_id:
			exact_row = row
			break
		if not row.shopify_variant_id and not blank_row:
			blank_row = row

	existing_row = exact_row or blank_row
	if existing_row:
		frappe.db.set_value("Item Shopify Store", existing_row.name, update_data, update_modified=False)
		return "updated"

	row = item.append(
		"shopify_stores",
		{
			"shopify_store": store_name,
			"enabled": 1,
			**update_data,
		},
	)
	row.db_insert()
	return "created"
