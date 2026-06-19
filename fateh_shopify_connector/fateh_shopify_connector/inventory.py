# Copyright (c) 2024, siva@enfono.com and contributors
# For license information, please see license.txt

import json

import frappe
from frappe.utils import add_to_date, now_datetime

from fateh_shopify_connector.fateh_shopify_connector.connection import shopify_session
from fateh_shopify_connector.fateh_shopify_connector.utils import create_shopify_log
from fateh_shopify_connector.utils.logger import get_logger

_BATCH_SIZE = 250

_INVENTORY_SET_MUTATION = """
mutation inventorySetQuantities($input: InventorySetQuantitiesInput!) {
  inventorySetQuantities(input: $input) {
    inventoryAdjustmentGroup { id }
    userErrors { field message }
  }
}
"""


def update_inventory_on_shopify(shopify_store=None):
	"""
	Scheduled entry point — push ERPNext stock levels to Shopify.

	Called every 10 minutes by the scheduler. When shopify_store is provided
	(manual trigger from the Shopify Store form), only that store is synced.
	Otherwise all enabled stores that are due for a sync are processed.
	"""
	logger = get_logger()

	if shopify_store:
		stores = [frappe.get_doc("Shopify Store", shopify_store)]
	else:
		names = frappe.get_all(
			"Shopify Store",
			filters={"enabled": 1, "enable_inventory_sync": 1},
			pluck="name",
		)
		stores = [frappe.get_doc("Shopify Store", n) for n in names]

	for store in stores:
		if not _is_sync_due(store):
			logger.info("Inventory sync not due for store %s, skipping", store.name)
			continue
		try:
			_sync_store(store)
		except Exception:
			logger.exception("Inventory sync failed for store %s", store.name)
			create_shopify_log(
				status="Error",
				shopify_store=store.name,
				exception=frappe.get_traceback(),
				message=f"Inventory sync failed for store {store.name}",
			)


# ── helpers ────────────────────────────────────────────────────────────────────


def _is_sync_due(store) -> bool:
	if not store.last_inventory_sync:
		return True
	frequency = store.inventory_sync_frequency or 60
	return now_datetime() >= add_to_date(store.last_inventory_sync, minutes=frequency)


def _sync_store(store):
	logger = get_logger()
	logger.info("Inventory sync start: %s (mode=%s)", store.name, store.inventory_sync_mode)

	warehouse_to_location = {
		row.erpnext_warehouse: row.shopify_location_id
		for row in (store.warehouse_mapping or [])
		if row.erpnext_warehouse and row.shopify_location_id
	}

	items = _items_to_sync(store)
	if not items:
		logger.info("No items eligible for inventory sync on store %s", store.name)
		_stamp_sync(store)
		return

	quantities = _build_quantities(items, store, warehouse_to_location)
	if not quantities:
		logger.info("No mapped warehouse quantities to push for store %s", store.name)
		_stamp_sync(store)
		return

	logger.info("Pushing %d inventory quantities for store %s", len(quantities), store.name)
	_push_batches(store, quantities)
	_stamp_sync(store)
	logger.info("Inventory sync complete: %s", store.name)


def _items_to_sync(store) -> list[dict]:
	"""Return Item Shopify Store rows that are ready to sync."""
	filters = {
		"shopify_store": store.name,
		"enabled": 1,
		"shopify_inventory_item_id": ["is", "set"],
	}

	if store.inventory_sync_mode == "Changed Bins" and store.last_inventory_sync:
		changed = frappe.get_all(
			"Bin",
			filters={"modified": [">", store.last_inventory_sync]},
			pluck="item_code",
		)
		if not changed:
			return []
		filters["parent"] = ["in", list(set(changed))]

	return frappe.get_all(
		"Item Shopify Store",
		filters=filters,
		fields=["parent as item_code", "shopify_inventory_item_id"],
	)


def _build_quantities(items: list[dict], store, warehouse_to_location: dict) -> list[dict]:
	"""Build the list of {inventoryItemId, locationId, quantity} payloads."""
	quantities = []

	for item in items:
		item_code = item["item_code"]
		inv_id = item["shopify_inventory_item_id"]

		bins = frappe.get_all(
			"Bin",
			filters={"item_code": item_code},
			fields=["warehouse", "actual_qty"],
		)
		bin_map = {b["warehouse"]: max(0, b["actual_qty"] or 0) for b in bins}

		if warehouse_to_location:
			for warehouse, location_id in warehouse_to_location.items():
				quantities.append(
					{
						"inventoryItemId": f"gid://shopify/InventoryItem/{inv_id}",
						"locationId": f"gid://shopify/Location/{location_id}",
						"quantity": int(bin_map.get(warehouse, 0)),
					}
				)
		elif store.warehouse:
			# Single default warehouse — no explicit location mapping
			total = int(sum(bin_map.values()))
			quantities.append(
				{
					"inventoryItemId": f"gid://shopify/InventoryItem/{inv_id}",
					"locationId": None,
					"quantity": total,
				}
			)

	return [q for q in quantities if q["locationId"]]


def _push_batches(store, quantities: list[dict]):
	"""Push quantities to Shopify via GraphQL in batches of _BATCH_SIZE."""
	import shopify

	logger = get_logger()

	@shopify_session(shopify_store=store)
	def _execute():
		for i in range(0, len(quantities), _BATCH_SIZE):
			batch = quantities[i : i + _BATCH_SIZE]
			variables = {
				"input": {
					"name": "available",
					"reason": "correction",
					"quantities": batch,
				}
			}

			raw = shopify.GraphQL().execute(_INVENTORY_SET_MUTATION, variables=variables)
			result = json.loads(raw) if isinstance(raw, str) else raw

			user_errors = (
				result.get("data", {}).get("inventorySetQuantities", {}).get("userErrors", [])
			)
			if user_errors:
				msg = "; ".join(f"{e['field']}: {e['message']}" for e in user_errors)
				logger.warning("GraphQL inventory errors for %s: %s", store.name, msg)
				create_shopify_log(
					status="Error",
					shopify_store=store.name,
					message=f"Inventory sync GraphQL errors: {msg}",
					response_data=result,
				)
			else:
				logger.info(
					"Pushed batch %d-%d for store %s",
					i + 1,
					i + len(batch),
					store.name,
				)

	_execute()


def _stamp_sync(store):
	frappe.db.set_value(
		"Shopify Store",
		store.name,
		"last_inventory_sync",
		now_datetime(),
		update_modified=False,
	)
	frappe.db.commit()
