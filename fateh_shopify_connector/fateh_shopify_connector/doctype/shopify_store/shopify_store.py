# Copyright (c) 2024, siva@enfono.com and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from fateh_shopify_connector.fateh_shopify_connector.connection import (
	register_webhooks,
	shopify_session,
)
from fateh_shopify_connector.utils.logger import get_logger


class ShopifyStore(Document):
	def validate(self):
		from fateh_shopify_connector.fateh_shopify_connector.connection import (
			get_callback_url,
			normalize_shop_domain,
		)

		if self.shop_domain:
			self.shop_domain = normalize_shop_domain(self.shop_domain)

		if not self.is_new():
			self.callback_url = get_callback_url(self)

	@frappe.whitelist()
	def test_connection(self):
		import shopify

		@shopify_session(shopify_store=self)
		def _test():
			shop = shopify.Shop.current()
			frappe.msgprint(
				_("Successfully connected to {0} ({1})").format(shop.name, shop.email),
				indicator="green",
			)

		_test()

	@frappe.whitelist()
	def register_webhooks(self):
		@shopify_session(shopify_store=self)
		def _register():
			webhooks = register_webhooks(self)
			frappe.msgprint(
				_("Successfully registered {0} webhooks").format(len(webhooks)),
				indicator="green",
			)

		_register()

	@frappe.whitelist()
	def fetch_webhooks(self):
		import shopify

		@shopify_session(shopify_store=self)
		def _fetch():
			webhooks = shopify.Webhook.find()
			return [{"id": w.id, "topic": w.topic, "address": w.address} for w in webhooks]

		return _fetch()

	@frappe.whitelist()
	def fetch_shopify_locations(self):
		import shopify

		@shopify_session(shopify_store=self)
		def _fetch():
			locations = shopify.Location.find()
			return [
				{
					"shopify_location_id": str(loc.id),
					"shopify_location_name": loc.name,
					"erpnext_warehouse": "",
				}
				for loc in locations
			]

		return _fetch()

	@frappe.whitelist()
	def fetch_shopify_collections(self):
		import shopify

		@shopify_session(shopify_store=self)
		def _fetch():
			collections = shopify.CustomCollection.find()
			return [
				{
					"shopify_collection_id": str(c.id),
					"shopify_collection_title": c.title,
					"field_value": "",
				}
				for c in collections
			]

		return _fetch()

	@frappe.whitelist()
	def fetch_products_and_map_by_sku(self):
		frappe.enqueue(
			"fateh_shopify_connector.fateh_shopify_connector.product.fetch_and_map_products_by_sku",
			queue="long",
			timeout=1800,
			shopify_store=self.name,
		)
		frappe.msgprint(
			_("SKU mapping job has been queued. You will be notified when it completes."),
			indicator="blue",
		)

	@frappe.whitelist()
	def sync_all_items(self):
		frappe.enqueue(
			"fateh_shopify_connector.fateh_shopify_connector.product.sync_all_items",
			queue="long",
			timeout=3600,
			shopify_store=self.name,
		)
		frappe.msgprint(_("Item sync job has been queued."), indicator="blue")

	@frappe.whitelist()
	def sync_inventory(self):
		frappe.enqueue(
			"fateh_shopify_connector.fateh_shopify_connector.inventory.update_inventory_on_shopify",
			queue="long",
			timeout=1800,
			shopify_store=self.name,
		)
		frappe.msgprint(_("Inventory sync job has been queued."), indicator="blue")

	@frappe.whitelist()
	def fetch_and_sync_orders(self):
		frappe.enqueue(
			"fateh_shopify_connector.fateh_shopify_connector.order.fetch_and_sync_orders",
			queue="long",
			timeout=1800,
			shopify_store=self.name,
		)
		frappe.msgprint(_("Order sync job has been queued."), indicator="blue")
