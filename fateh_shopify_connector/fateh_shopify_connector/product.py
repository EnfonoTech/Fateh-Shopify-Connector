# Copyright (c) 2024, siva@enfono.com and contributors
# For license information, please see license.txt

import base64
import hashlib
import json
import os
import time
from typing import TYPE_CHECKING, Any

import frappe
from frappe import _
from frappe.utils import now_datetime

from fateh_shopify_connector.fateh_shopify_connector.connection import DEFAULT_API_VERSION
from fateh_shopify_connector.fateh_shopify_connector.utils import (
	create_shopify_log,
	get_eligible_stores_for_item,
	get_item_shopify_store_row,
)
from fateh_shopify_connector.utils.logger import get_logger

if TYPE_CHECKING:
	from fateh_shopify_connector.fateh_shopify_connector.doctype.shopify_store.shopify_store import ShopifyStore

# Fields that go on the product level
PRODUCT_STANDARD_FIELDS = ["body_html", "vendor", "product_type", "tags", "handle"]

# Fields that go on the variant level
VARIANT_STANDARD_FIELDS = ["price", "compare_at_price", "sku", "barcode", "weight", "weight_unit"]

# Shopify REST API: leaky bucket 40 calls, refills at 2/s
_SHOPIFY_RATE_LIMIT_MAX_RETRIES = 5
_SHOPIFY_RATE_LIMIT_DEFAULT_WAIT = 5.0  # seconds to wait when no Retry-After header


def _shopify_call_with_retry(fn, *args, **kwargs):
	"""Call a Shopify REST API function, retrying on 429 rate-limit responses."""
	logger = get_logger()
	for attempt in range(_SHOPIFY_RATE_LIMIT_MAX_RETRIES):
		try:
			return fn(*args, **kwargs)
		except Exception as exc:
			# pyactiveresource wraps 429 as ClientError; check the response code
			response = getattr(exc, "response", None)
			code = getattr(response, "code", None)
			if code == 429:
				# Honour Retry-After header if present, else use default
				headers = getattr(response, "headers", {}) or {}
				retry_after = float(headers.get("retry-after") or _SHOPIFY_RATE_LIMIT_DEFAULT_WAIT)
				wait = retry_after + 1.0  # add 1s buffer
				logger.warning(
					"Shopify 429 rate-limit on attempt %d/%d — waiting %.1fs",
					attempt + 1,
					_SHOPIFY_RATE_LIMIT_MAX_RETRIES,
					wait,
				)
				time.sleep(wait)
			else:
				raise
	# Final attempt (no except — let it propagate)
	return fn(*args, **kwargs)


def sync_item_to_shopify(doc, method=None):
	"""
	Doc event handler - sync item to all eligible stores.

	Called on Item on_update and after_insert events.

	Args:
		doc: Item document
		method: Event method name (on_update, after_insert)
	"""
	# Skip if in test mode or import
	if frappe.flags.in_test or frappe.flags.in_import:
		return

	# Skip during bulk SKU mapping to avoid N unnecessary enqueued sync jobs
	if getattr(frappe.flags, "in_sku_mapping", False):
		return

	# Skip if item is disabled
	if doc.disabled:
		return

	# Get eligible stores for this item
	eligible_stores = get_eligible_stores_for_item(doc)

	if not eligible_stores:
		return

	for store in eligible_stores:
		# Check if store has update_shopify_on_item_update enabled
		if method == "on_update" and not store.update_shopify_on_item_update:
			continue

		# Enqueue sync job for each store
		frappe.enqueue(
			"fateh_shopify_connector.fateh_shopify_connector.product.sync_item_to_store",
			queue="short",
			timeout=300,
			item_code=doc.name,
			store_name=store.name,
		)


def sync_item_price_to_shopify(doc, method=None):
	"""
	Doc event handler - sync item when its price changes.

	Called on Item Price on_update and after_insert events.

	Args:
		doc: Item Price document
		method: Event method name
	"""
	# Skip if in test mode or import
	if frappe.flags.in_test or frappe.flags.in_import:
		return

	# Only sync selling prices
	if not doc.selling:
		return

	item_code = doc.item_code
	price_list = doc.price_list

	# Find stores that use this price list
	stores = frappe.get_all(
		"Shopify Store",
		filters={
			"enabled": 1,
			"enable_item_sync": 1,
			"update_shopify_on_item_update": 1,
			"price_list": price_list,
		},
		pluck="name",
	)

	if not stores:
		return

	# TODO: This logic only considers explicit "Item Shopify Store" rows with enabled=1
	# and ignores stores where the item is auto-eligible via store filters. Update to
	# compute eligible stores the same way as sync_item_to_shopify by calling
	# get_eligible_stores_for_item(item_code) (or merging that result with any explicit
	# enabled rows) and iterate over that unified list to enqueue sync_item_to_store
	# for each eligible store so filter-based eligibilities are included.
	# Check if item is linked to any of these stores
	for store_name in stores:
		# Check if item has this store enabled
		has_store = frappe.db.exists(
			"Item Shopify Store", {"parent": item_code, "shopify_store": store_name, "enabled": 1}
		)

		if has_store:
			frappe.enqueue(
				"fateh_shopify_connector.fateh_shopify_connector.product.sync_item_to_store",
				queue="short",
				timeout=300,
				item_code=item_code,
				store_name=store_name,
			)


@frappe.whitelist()
def _is_item_eligible_for_store_sql(item_code: str, store) -> bool:
	"""
	Check if a single item is eligible for a store using SQL (same logic as bulk sync).

	Priority:
	1. Explicit Item Shopify Store row with enabled=1 → eligible
	2. Explicit Item Shopify Store row with enabled=0 → not eligible
	3. Store item_filters match via SQL → eligible
	4. No filters configured → not eligible
	"""
	# Check explicit override row
	explicit = frappe.db.get_value(
		"Item Shopify Store",
		{"parent": item_code, "shopify_store": store.name},
		"enabled",
	)
	if explicit is not None:
		return bool(explicit)

	# No explicit row — check via store filters (same SQL approach as _collect_eligible_items)
	if not store.item_filters:
		return False

	frappe_filters = {"name": item_code, "disabled": 0}
	for filter_row in store.item_filters:
		field = filter_row.erpnext_field
		ftype = filter_row.filter_type
		value = filter_row.field_value or ""

		if ftype == "Field Equals":
			frappe_filters[field] = value
		elif ftype == "Field In":
			values = [v.strip() for v in value.split(",") if v.strip()]
			if values:
				frappe_filters[field] = ["in", values]
		elif ftype == "Field Not In":
			values = [v.strip() for v in value.split(",") if v.strip()]
			if values:
				frappe_filters[field] = ["not in", values]
		elif ftype in ("Field Has Value", "Field Not Empty"):
			frappe_filters[field] = ["is", "set"]

	return bool(frappe.get_all("Item", filters=frappe_filters, pluck="name", limit=1))


@frappe.whitelist()
def manual_sync_item_to_shopify(item_code: str):
	"""
	Manually trigger sync of item to all eligible Shopify stores.

	Checks both explicit Item Shopify Store rows AND store item_filters (same
	SQL logic as Sync All Items) so filter-matched items don't need a row first.
	"""
	stores = frappe.get_all(
		"Shopify Store", filters={"enabled": 1, "enable_item_sync": 1}, pluck="name"
	)
	if not stores:
		frappe.throw(_("No Shopify stores with item sync enabled"))

	eligible_store_names = []
	for store_name in stores:
		store = frappe.get_doc("Shopify Store", store_name)
		if _is_item_eligible_for_store_sql(item_code, store):
			eligible_store_names.append(store_name)

	if not eligible_store_names:
		frappe.throw(
			_("Item {0} does not match any store's eligibility filters. Add it manually via Item → Shopify Stores table, or check the store's Item Eligibility Filters.").format(item_code)
		)

	for store_name in eligible_store_names:
		frappe.enqueue(
			"fateh_shopify_connector.fateh_shopify_connector.product.sync_item_to_store",
			queue="short",
			timeout=300,
			item_code=item_code,
			store_name=store_name,
			force=True,
		)

	return {"success": True, "queued_count": len(eligible_store_names)}


def sync_item_to_store(item_code: str, store_name: str, force: bool = False):
	"""
	Sync single item to a single Shopify store.

	Args:
		item_code: ERPNext Item code
		store_name: Shopify Store name
		force: If True, skip change detection and force sync
	"""
	logger = get_logger()
	logger.info("Syncing item %s to store %s", item_code, store_name)
	item = frappe.get_doc("Item", item_code)
	store = frappe.get_doc("Shopify Store", store_name)

	if not store.enabled or not store.enable_item_sync:
		logger.error("Store %s is not enabled or item sync is not enabled", store_name)
		return

	# Initialize API versions
	_init_shopify_api_versions()

	# Get auth details
	api_version = store.api_version or DEFAULT_API_VERSION
	access_token = frappe.db.get_value("Shopify Store", store.name, "access_token")

	if not access_token:
		logger.error("Access token not configured for store %s", store_name)
		frappe.log_error(
			title=f"Shopify Sync Error - {store_name}",
			message=f"Access token not configured for store {store_name}",
		)
		return

	# Get or create Item Shopify Store row
	store_row = get_item_shopify_store_row(item, store)

	# Check for changes using hash (skip if force=True)
	current_hash = compute_sync_hash(item, store)
	if not force and store_row and store_row.last_sync_hash == current_hash:
		# No changes, skip sync
		logger.info("No changes, skipping sync for item %s", item_code)
		return

	try:
		from shopify.session import Session

		with Session.temp(store.shop_domain, api_version, access_token):
			# Build product payload
			product_data, variant_data, metafields_data, category_value, collections_field = (
				build_product_payload(item, store)
			)

			# Add category to product data if specified
			if category_value:
				product_data["product_category"] = {"product_taxonomy_node_id": str(category_value)}

			shopify_product_id = store_row.shopify_product_id if store_row else None
			shopify_variant_id = store_row.shopify_variant_id if store_row else None

			# If product_id is missing, search Shopify by SKU before creating
			if not shopify_product_id:
				existing = _find_shopify_product_by_sku(item_code)
				if existing:
					shopify_product_id, shopify_variant_id = existing
					logger.info(
						"Found existing Shopify product %s for SKU %s — will update",
						shopify_product_id,
						item_code,
					)

			if shopify_product_id:
				logger.info(
					"Updating existing product %s, item %s, store %s",
					shopify_product_id,
					item_code,
					store_name,
				)
				product = _update_shopify_product(
					shopify_product_id,
					shopify_variant_id,
					product_data,
					variant_data,
					metafields_data,
				)
			else:
				logger.info("Creating new product for item %s, store %s", item_code, store_name)
				product = _create_shopify_product(product_data, variant_data, metafields_data)

			# Sync collections if mapping configured
			if collections_field:
				logger.info(
					"Syncing collections for product %s, item %s, store %s", product.id, item_code, store_name
				)
				_sync_product_collections(str(product.id), item, store, collections_field)

			# Sync image if enabled
			image_hash = None
			if store.enable_image_sync:
				logger.info(
					"Syncing image for product %s, item %s, store %s", product.id, item_code, store_name
				)
				image_hash = _sync_item_image_to_shopify(item, store, product.id, store_row, force)

			# Update Item Shopify Store row
			_update_item_shopify_store_row(item, store, product, current_hash, image_hash)

			create_shopify_log(
				status="Success",
				method="sync_item_to_store",
				shopify_store=store_name,
				message=f"Synced item {item_code} to Shopify product {product.id}",
				reference_doctype="Item",
				reference_name=item_code,
			)

	except Exception as e:
		logger.error("Failed to sync item %s to store %s: %s", item_code, store_name, str(e), exc_info=True)
		create_shopify_log(
			status="Error",
			method="sync_item_to_store",
			shopify_store=store_name,
			exception=frappe.get_traceback(),
			message=f"Failed to sync item {item_code}",
			reference_doctype="Item",
			reference_name=item_code,
		)
		frappe.db.commit()
		raise


def _find_shopify_product_by_sku(sku: str) -> tuple[str, str] | None:
	"""Search Shopify for a product variant matching the given SKU.

	Must be called inside an active Session.temp() context.

	Returns:
		(product_id, variant_id) strings if found, None otherwise.
	"""
	from shopify.resources import Variant

	logger = get_logger()
	try:
		variants = _shopify_call_with_retry(Variant.find, sku=sku, limit=1)
		if variants:
			v = variants[0]
			return str(v.product_id), str(v.id)
	except Exception:
		logger.warning("SKU lookup failed for %s", sku, exc_info=True)
	return None


def _init_shopify_api_versions():
	"""Initialize Shopify API versions and set a sensible socket timeout."""
	from shopify.api_version import ApiVersion
	from shopify.base import ShopifyResource

	if not ApiVersion.versions:
		ApiVersion.fetch_known_versions()

	ShopifyResource.timeout = 60  # seconds per API call


def _create_shopify_product(
	product_data: dict[str, Any], variant_data: dict[str, Any], metafields_data: list[dict[str, Any]]
) -> Any:
	"""
	Create a new product in Shopify.

	Args:
		product_data: Product level fields
		variant_data: Variant level fields
		metafields_data: List of metafield definitions

	Returns:
		Created Product resource
	"""
	# Create product with product-level data only (no variants)
	# Shopify auto-creates a default variant when product is saved
	from shopify.resources import Metafield, Product

	logger = get_logger()
	product = Product()
	for key, value in product_data.items():
		setattr(product, key, value)

	if not _shopify_call_with_retry(product.save):
		raise Exception(f"Failed to create product: {product.errors.full_messages()}")

	# Update default variant with variant-level data (sku, price, inventory_management, etc.)
	if variant_data and product.variants:
		default_variant = product.variants[0]
		for key, value in variant_data.items():
			setattr(default_variant, key, value)
		if not _shopify_call_with_retry(default_variant.save):
			logger.error("Failed to update default variant: %s", default_variant.errors.full_messages())
			raise Exception(f"Failed to update variant: {default_variant.errors.full_messages()}")

	# Create metafields if any
	if metafields_data and product.id:
		for mf_data in metafields_data:
			metafield = Metafield(
				{
					"namespace": mf_data["namespace"],
					"key": mf_data["key"],
					"value": mf_data["value"],
					"type": mf_data["type"],
					"owner_resource": "product",
					"owner_id": product.id,
				}
			)
			if not metafield.save():
				logger.warning(
					"Failed to create metafield %s.%s for product %s: %s",
					mf_data["namespace"],
					mf_data["key"],
					product.id,
					metafield.errors.full_messages(),
				)

	return product


def _update_shopify_product(
	product_id: str,
	variant_id: str | None,
	product_data: dict[str, Any],
	variant_data: dict[str, Any],
	metafields_data: list[dict[str, Any]],
) -> Any:
	"""
	Update an existing product in Shopify.

	Args:
		product_id: Shopify product ID
		variant_id: Shopify variant ID (optional)
		product_data: Product level fields
		variant_data: Variant level fields
		metafields_data: List of metafield definitions

	Returns:
		Updated Product resource
	"""
	from shopify.resources import Metafield, Product, Variant

	logger = get_logger()
	product = _shopify_call_with_retry(Product.find, product_id)

	# Update product fields
	for key, value in product_data.items():
		setattr(product, key, value)

	if not _shopify_call_with_retry(product.save):
		logger.error("Failed to update product: %s", product.errors.full_messages())
		raise Exception(f"Failed to update product: {product.errors.full_messages()}")

	# Update variant if we have variant data
	if variant_data and variant_id:
		variant = _shopify_call_with_retry(Variant.find, variant_id, product_id=product_id)
		for key, value in variant_data.items():
			setattr(variant, key, value)
		if not _shopify_call_with_retry(variant.save):
			logger.error("Failed to update variant: %s", variant.errors.full_messages())
			raise Exception(f"Failed to update variant: {variant.errors.full_messages()}")
	elif variant_data and product.variants:
		# Update first variant if no specific variant ID
		variant = product.variants[0]
		for key, value in variant_data.items():
			setattr(variant, key, value)
		if not _shopify_call_with_retry(variant.save):
			logger.error("Failed to update variant: %s", variant.errors.full_messages())
			raise Exception(f"Failed to update variant: {variant.errors.full_messages()}")

	# Update metafields
	if metafields_data:
		existing_metafields = _shopify_call_with_retry(
			Metafield.find, resource="products", resource_id=product_id
		)
		existing_map = {(mf.namespace, mf.key): mf for mf in existing_metafields}

		for mf_data in metafields_data:
			key_tuple = (mf_data["namespace"], mf_data["key"])
			if key_tuple in existing_map:
				# Update existing metafield
				mf = existing_map[key_tuple]
				mf.value = mf_data["value"]
				if not mf.save():
					logger.warning(
						"Failed to update metafield %s.%s for product %s: %s",
						mf_data["namespace"],
						mf_data["key"],
						product.id,
						mf.errors.full_messages(),
					)
			else:
				# Create new metafield
				metafield = Metafield(
					{
						"namespace": mf_data["namespace"],
						"key": mf_data["key"],
						"value": mf_data["value"],
						"type": mf_data["type"],
						"owner_resource": "product",
						"owner_id": product_id,
					}
				)
				if not metafield.save():
					logger.warning(
						"Failed to create metafield %s.%s for product %s: %s",
						mf_data["namespace"],
						mf_data["key"],
						product.id,
						metafield.errors.full_messages(),
					)

	return product


def _update_item_shopify_store_row(item, store, product, sync_hash: str, image_hash: str | None = None):
	"""
	Update or create Item Shopify Store child row with sync details.

	Args:
		item: Item document
		store: Shopify Store document
		product: Shopify Product resource
		sync_hash: Current sync hash
		image_hash: Current image hash (optional)
	"""
	logger = get_logger()
	store_row = get_item_shopify_store_row(item, store)

	# Get first variant ID and cached inventory_item_id
	variant_id = None
	inventory_item_id = None
	if product.variants:
		first_variant = product.variants[0]
		variant_id = str(first_variant.id)
		inv_item_id = getattr(first_variant, "inventory_item_id", None)
		if inv_item_id:
			inventory_item_id = str(inv_item_id)

	update_data = {
		"shopify_product_id": str(product.id),
		"shopify_variant_id": variant_id,
		"last_sync_at": now_datetime(),
		"last_sync_hash": sync_hash,
	}

	# Cache inventory_item_id so the GraphQL inventory sync can skip the REST
	# variant lookup. Only overwrite when we actually received a value from
	# Shopify, so we don't wipe an existing cached id.
	if inventory_item_id:
		update_data["shopify_inventory_item_id"] = inventory_item_id

	# Only update image hash if provided
	if image_hash is not None:
		update_data["last_image_hash"] = image_hash

	if store_row:
		# Update existing row
		logger.info(
			"Updating existing Item Shopify Store row %s, item %s, store %s",
			store_row.name,
			item.name,
			store.name,
		)
		frappe.db.set_value(
			"Item Shopify Store",
			store_row.name,
			update_data,
			update_modified=False,
		)
	else:
		# Create new row directly (no parent save, no on_update hook)
		logger.info("Creating new row on Item Shopify Store for item %s, store %s", item.name, store.name)
		row = item.append(
			"shopify_stores",
			{
				"shopify_store": store.name,
				"enabled": 1,
				**update_data,
			},
		)
		row.db_insert()


def build_product_payload(item, store) -> tuple:
	"""
	Build Shopify product JSON from Item using store's field mapping.

	Args:
		item: Item document
		store: Shopify Store document

	Returns:
		Tuple of (product_data, variant_data, metafields_data, category_value, collections_field)
	"""
	description = getattr(item, "description", None) or ""
	product_data = {
		"title": item.item_name or item.name,
		"body_html": description,
	}
	variant_data = {
		"sku": item.item_code,
	}
	metafields_data = []
	category_value = None
	collections_field = None

	# Always include price from the store's price list (auto, no mapping required)
	price = get_item_price(item, store)
	if price is not None:
		variant_data["price"] = str(price)

	# Enable inventory tracking for stock items
	if item.is_stock_item:
		variant_data["inventory_management"] = "shopify"

	# Process field mappings (explicit overrides auto price if user maps price manually)
	for field_map in store.item_field_map:
		erpnext_field = field_map.erpnext_field
		field_type = field_map.shopify_field_type

		# Get value from item
		value = _get_field_value(item, store, erpnext_field, field_map.default_value)

		if field_type == "Standard Field":
			shopify_field = field_map.shopify_standard_field

			# Handle special fields that require separate processing
			if shopify_field == "category":
				if value:
					category_value = value
			elif shopify_field == "collections":
				# Store field name for collection sync (processed separately)
				collections_field = erpnext_field
			elif value is not None:
				if shopify_field in PRODUCT_STANDARD_FIELDS:
					product_data[shopify_field] = value
				elif shopify_field in VARIANT_STANDARD_FIELDS:
					variant_data[shopify_field] = value

		elif field_type == "Metafield" and value is not None:
			metafields_data.append(
				{
					"namespace": field_map.metafield_namespace,
					"key": field_map.metafield_key,
					"value": str(value),
					"type": field_map.metafield_type or "single_line_text_field",
				}
			)

	return product_data, variant_data, metafields_data, category_value, collections_field


def _get_field_value(item, store, field_name: str, default_value: str | None = None):
	"""
	Get field value from item, with special handling for certain fields.

	Args:
		item: Item document
		store: Shopify Store document
		field_name: Field name to get
		default_value: Default value if field is empty

	Returns:
		Field value or default
	"""
	# Special handling for price - get from Item Price
	if field_name in ("standard_rate", "price", "valuation_rate"):
		return get_item_price(item, store) or default_value

	# Get from item directly
	value = getattr(item, field_name, None)

	if value is None or value == "":
		return default_value

	return value


def get_item_price(item, store) -> float | None:
	"""
	Get item price from store's configured price list.

	Args:
		item: Item document
		store: Shopify Store document

	Returns:
		Price rate or None
	"""
	if not store.price_list:
		# Fallback to item's standard_rate
		return item.standard_rate if hasattr(item, "standard_rate") else None

	price = frappe.db.get_value(
		"Item Price",
		{
			"item_code": item.name,
			"price_list": store.price_list,
			"selling": 1,
		},
		"price_list_rate",
	)

	return price


def _get_collection_values(item, field_name: str) -> list[str]:
	"""
	Get collection values from item field, handling different field types.

	Supports:
	- Table MultiSelect: Reads from child table
	- Link/Data/Select: Single value
	- Small Text: Comma-separated values

	Args:
		item: Item document
		field_name: Field name on Item to read

	Returns:
		List of collection names/values
	"""
	meta = frappe.get_meta("Item")
	field = meta.get_field(field_name)

	if not field:
		return []

	if field.fieldtype == "Table MultiSelect":
		# Get the link field from child table
		child_meta = frappe.get_meta(field.options)
		link_field = None
		for f in child_meta.fields:
			if f.fieldtype == "Link":
				link_field = f.fieldname
				break

		if not link_field:
			return []

		values = []
		for row in getattr(item, field_name, []) or []:
			val = getattr(row, link_field, None)
			if val:
				values.append(val)
		return values

	elif field.fieldtype in ("Link", "Data", "Select"):
		value = getattr(item, field_name, None)
		return [value] if value else []

	elif field.fieldtype in ("Small Text", "Text"):
		value = getattr(item, field_name, None)
		if value:
			return [v.strip() for v in value.split(",") if v.strip()]
		return []

	return []


def _create_shopify_collection_and_mapping(store, collection_name: str) -> str | None:
	"""
	Create a new collection on Shopify and add the mapping entry to the store.

	Args:
		store: Shopify Store document
		collection_name: Name for the new collection

	Returns:
		Shopify collection ID if successful, None otherwise
	"""
	try:
		from shopify.resources import CustomCollection

		# Create collection on Shopify
		collection = CustomCollection()
		collection.title = collection_name
		if not _shopify_call_with_retry(collection.save):
			frappe.log_error(
				title=f"Collection Creation Error - {store.name}",
				message=f"Failed to create collection '{collection_name}': {collection.errors.full_messages()}",
			)
			return None

		collection_id = str(collection.id)

		# Add mapping entry to store's collection_mapping table
		store.reload()
		store.append(
			"collection_mapping",
			{
				"field_value": collection_name,
				"shopify_collection_id": collection_id,
				"shopify_collection_title": collection_name,
			},
		)
		store.flags.ignore_validate = True
		store.flags.ignore_mandatory = True
		store.save(ignore_permissions=True)
		frappe.db.commit()

		create_shopify_log(
			status="Success",
			method="_create_shopify_collection_and_mapping",
			shopify_store=store.name,
			message=f"Auto-created Shopify collection '{collection_name}' (ID: {collection_id})",
			reference_doctype="Shopify Store",
			reference_name=store.name,
		)

		return collection_id

	except Exception as e:
		frappe.log_error(
			title=f"Collection Creation Error - {store.name}",
			message=f"Failed to create collection '{collection_name}': {e}\n{frappe.get_traceback()}",
		)
		return None


def _sync_product_collections(product_id: str, item, store, collections_field: str):
	"""
	Sync item to Shopify collections based on field values and collection mapping.

	Uses Shopify SDK Collect resource for all API operations.
	Adds product to new collections and removes from collections no longer assigned.
	Auto-creates missing collections on Shopify and adds mapping entries.

	Args:
		product_id: Shopify product ID
		item: Item document
		store: Shopify Store document
		collections_field: Name of the field on Item containing collection values
	"""
	if not collections_field:
		return

	# Get collection values from item (handles Table MultiSelect, Link, Data, etc.)
	collection_values = _get_collection_values(item, collections_field)

	if not collection_values:
		return

	# Build lookup from collection mapping table
	# Key: field_value, Value: shopify_collection_id
	collection_lookup = {}
	for mapping in store.collection_mapping or []:
		collection_id = mapping.shopify_collection_id
		# Extract numeric ID from GID format if needed
		if collection_id and collection_id.startswith("gid://"):
			collection_id = collection_id.split("/")[-1]
		if collection_id:
			collection_lookup[mapping.field_value] = collection_id

	# Find target Shopify collection IDs based on item's collection values
	target_collection_ids = set()
	for value in collection_values:
		if value in collection_lookup:
			target_collection_ids.add(collection_lookup[value])
		elif store.auto_create_collections:
			# Auto-create missing collections only if enabled on store
			new_collection_id = _create_shopify_collection_and_mapping(store, value)
			if new_collection_id:
				target_collection_ids.add(new_collection_id)
				# Update local lookup for this sync cycle
				collection_lookup[value] = new_collection_id

	# Get current product-collection relationships using SDK
	from shopify.resources import Collect

	try:
		current_collects = _shopify_call_with_retry(Collect.find, product_id=product_id)
	except Exception:
		current_collects = []

	current_collection_ids = {str(c.collection_id) for c in current_collects}

	# ADD to new collections
	for collection_id in target_collection_ids - current_collection_ids:
		try:
			collect = Collect()
			collect.product_id = int(product_id)
			collect.collection_id = int(collection_id)
			if not _shopify_call_with_retry(collect.save):
				frappe.log_error(
					title=f"Collection Sync Error - {store.name}",
					message=f"Failed to add product {product_id} to collection {collection_id}: {collect.errors.full_messages()}",
				)
		except Exception as e:
			# Log but don't fail - collection might be a smart collection (403 error)
			frappe.log_error(
				title=f"Collection Sync Warning - {store.name}",
				message=f"Could not add product {product_id} to collection {collection_id}: {e}",
			)

	# REMOVE from collections no longer in item
	for collection_id in current_collection_ids - target_collection_ids:
		for collect in current_collects:
			if str(collect.collection_id) == collection_id:
				try:
					_shopify_call_with_retry(collect.destroy)
				except Exception as e:
					frappe.log_error(
						title=f"Collection Remove Warning - {store.name}",
						message=f"Could not remove product {product_id} from collection {collection_id}: {e}",
					)


def compute_sync_hash(item, store) -> str:
	"""
	Compute hash of item fields for change detection.

	Includes mapped fields and image (if image sync is enabled).

	Args:
		item: Item document
		store: Shopify Store document

	Returns:
		MD5 hash string
	"""
	# Collect all mapped field values
	hash_data = {"item_name": item.item_name}

	for field_map in store.item_field_map:
		field_name = field_map.erpnext_field
		value = _get_field_value(item, store, field_name, field_map.default_value)
		hash_data[field_name] = str(value) if value is not None else ""

	# Include image in hash if image sync is enabled
	if store.enable_image_sync and item.image:
		file_path = _get_item_image_path(item)
		if file_path:
			hash_data["_image_hash"] = _compute_image_hash(file_path)
		else:
			hash_data["_image_hash"] = ""
	elif store.enable_image_sync:
		# No image set
		hash_data["_image_hash"] = ""

	# Include collection values in hash if collections mapping exists
	for field_map in store.item_field_map:
		if (
			field_map.shopify_field_type == "Standard Field"
			and field_map.shopify_standard_field == "collections"
		):
			collection_values = _get_collection_values(item, field_map.erpnext_field)
			hash_data["_collections"] = ",".join(sorted(collection_values))
			break

	# Create deterministic hash
	hash_str = json.dumps(hash_data, sort_keys=True)
	return hashlib.md5(hash_str.encode()).hexdigest()


def _get_item_image_path(item) -> str | None:
	"""
	Get the file path for item's primary image.

	Args:
		item: Item document

	Returns:
		Absolute file path or None
	"""
	if not item.image:
		return None

	# Item.image contains URL like /files/item-image.jpg or /private/files/item-image.jpg
	image_url = item.image

	# Determine if public or private file
	if image_url.startswith("/private/files/"):
		file_path = frappe.get_site_path("private", "files", image_url.replace("/private/files/", ""))
	elif image_url.startswith("/files/"):
		file_path = frappe.get_site_path("public", "files", image_url.replace("/files/", ""))
	else:
		# Could be full URL or other format
		return None

	if os.path.exists(file_path):
		return file_path

	return None


def _compute_image_hash(file_path: str) -> str:
	"""
	Compute MD5 hash of image file content.

	Args:
		file_path: Absolute path to image file

	Returns:
		MD5 hash string
	"""
	hash_md5 = hashlib.md5()
	with (
		open(file_path, "rb") as f
	):  # nosemgrep: frappe-semgrep-rules.rules.security.frappe-security-file-traversal -- path from frappe.get_site_path(), not user input
		for chunk in iter(lambda: f.read(4096), b""):
			hash_md5.update(chunk)
	return hash_md5.hexdigest()


def _get_image_data_and_hash(item) -> tuple[str | None, str | None, str | None]:
	"""
	Get base64 encoded image data and hash for an item.

	Args:
		item: Item document

	Returns:
		Tuple of (base64_data, image_hash, filename) or (None, None, None)
	"""
	file_path = _get_item_image_path(item)
	if not file_path:
		return None, None, None

	try:
		image_hash = _compute_image_hash(file_path)

		with (
			open(file_path, "rb") as f
		):  # nosemgrep: frappe-semgrep-rules.rules.security.frappe-security-file-traversal -- path from frappe.get_site_path(), not user input
			image_data = base64.b64encode(f.read()).decode("utf-8")

		filename = os.path.basename(file_path)
		return image_data, image_hash, filename
	except Exception:
		frappe.log_error(
			title="Image Read Error",
			message=f"Failed to read image file: {file_path}\n{frappe.get_traceback()}",
		)
		return None, None, None


def _sync_product_image(product_id: str, image_data: str, filename: str) -> bool:
	"""Upload image to Shopify product, replacing any existing images."""
	from shopify.resources import Image

	logger = get_logger()

	# Delete all existing images first (ignore 404 — may have been removed on Shopify)
	try:
		existing_images = _shopify_call_with_retry(Image.find, product_id=product_id)
		for img in existing_images:
			try:
				_shopify_call_with_retry(img.destroy)
			except Exception:
				logger.warning("Could not delete image %s on product %s — skipping", img.id, product_id)
	except Exception:
		logger.warning("Could not fetch existing images for product %s — skipping cleanup", product_id)

	# Upload new image
	image = Image()
	image.product_id = product_id
	image.attachment = image_data
	image.filename = filename

	if not _shopify_call_with_retry(image.save):
		logger.error("Failed to upload image for product %s: %s", product_id, image.errors.full_messages())
		raise Exception(f"Failed to upload image for product {product_id}: {image.errors.full_messages()}")

	return True


def _sync_item_image_to_shopify(item, store, product_id: str, store_row, force: bool = False) -> str | None:
	"""
	Sync item image to Shopify product if image has changed.

	Args:
		item: Item document
		store: Shopify Store document
		product_id: Shopify product ID
		store_row: Item Shopify Store row (or None)
		force: If True, skip change detection and force sync

	Returns:
		New image hash if synced, None otherwise
	"""
	image_data, image_hash, filename = _get_image_data_and_hash(item)

	if not image_data:
		return None

	# Check if image has changed (skip if force=True)
	last_image_hash = store_row.last_image_hash if store_row else None
	if not force and last_image_hash == image_hash:
		# Image unchanged, return existing hash
		return image_hash

	try:
		_sync_product_image(str(product_id), image_data, filename)
		return image_hash
	except Exception:
		frappe.log_error(
			title=f"Shopify Image Sync Error - {store.name}",
			message=f"Failed to sync image for item {item.name}\n{frappe.get_traceback()}",
		)
		return None


def _collect_eligible_items(store) -> list[str]:
	"""Collect all item codes eligible for this store using efficient DB queries."""
	# Items explicitly linked with enabled=1
	explicit_items = set(
		frappe.get_all(
			"Item Shopify Store",
			filters={"shopify_store": store.name, "enabled": 1},
			pluck="parent",
		)
	)

	if not store.item_filters:
		return list(explicit_items)

	# Build frappe filters from store's item_filters rows (one DB query per set of conditions)
	frappe_filters = {"disabled": 0}
	for filter_row in store.item_filters:
		field = filter_row.erpnext_field
		ftype = filter_row.filter_type
		value = filter_row.field_value or ""

		if ftype == "Field Equals":
			frappe_filters[field] = value
		elif ftype == "Field In":
			values = [v.strip() for v in value.split(",") if v.strip()]
			if values:
				frappe_filters[field] = ["in", values]
		elif ftype == "Field Not In":
			values = [v.strip() for v in value.split(",") if v.strip()]
			if values:
				frappe_filters[field] = ["not in", values]
		elif ftype in ("Field Has Value", "Field Not Empty"):
			frappe_filters[field] = ["is", "set"]

	filter_items = set(frappe.get_all("Item", filters=frappe_filters, pluck="name"))
	return list(explicit_items | filter_items)


def sync_items_to_store(store_name: str, initiating_user: str | None = None):
	"""
	Collect eligible items via DB query and enqueue a single orchestrator job.

	Called from manual "Sync All Items" button.
	"""
	logger = get_logger()
	store: ShopifyStore = frappe.get_doc("Shopify Store", store_name)

	if not store.enabled or not store.enable_item_sync:
		frappe.throw(_("Item sync is not enabled for this store"))

	items_to_sync = _collect_eligible_items(store)

	if not items_to_sync:
		frappe.msgprint(_("No items to sync for this store. Check Item Eligibility Filters."))
		return 0

	user = initiating_user or frappe.session.user

	# Publish the total count immediately so the dialog shows real numbers
	frappe.publish_realtime(
		"shopify_item_sync_progress",
		{"store": store_name, "total": len(items_to_sync), "done": 0, "errors": 0, "current_item": "", "status": "queued"},
		user=user,
	)

	frappe.enqueue(
		"fateh_shopify_connector.fateh_shopify_connector.product._sync_all_items_orchestrator",
		queue="long",
		timeout=7200,
		# Unique job_id per run — no silent deduplication drops
		job_id=f"sync_all_items_{store_name}_{frappe.generate_hash(length=6)}",
		store_name=store_name,
		item_codes=items_to_sync,
		initiating_user=user,
	)

	logger.info("Queued orchestrator for %s items → store %s", len(items_to_sync), store_name)
	return len(items_to_sync)


def _sync_all_items_orchestrator(
	store_name: str, item_codes: list, initiating_user: str | None = None
):
	"""
	Background orchestrator: syncs items one by one and publishes real-time progress.

	Publishes `shopify_item_sync_progress` events so the browser dialog updates live.
	"""
	logger = get_logger()
	total = len(item_codes)
	done = 0
	errors = 0

	def _publish(current_item="", status="running"):
		frappe.publish_realtime(
			"shopify_item_sync_progress",
			{
				"store": store_name,
				"total": total,
				"done": done,
				"errors": errors,
				"current_item": current_item,
				"status": status,
			},
			user=initiating_user,
		)

	_publish(status="running")

	# Throttle: each item sync makes ~4-6 REST calls; stay well under 2 calls/second sustained.
	# 1.5s between items ≈ 3 calls/s burst window, well within the 40-call leaky bucket.
	_INTER_ITEM_DELAY = 1.5

	for item_code in item_codes:
		_publish(current_item=item_code)
		try:
			sync_item_to_store(item_code, store_name)
			done += 1
		except Exception:
			errors += 1
			done += 1
			logger.error("Failed to sync item %s to %s", item_code, store_name, exc_info=True)
			frappe.log_error(
				message=frappe.get_traceback(),
				title=f"Item Sync Error — {item_code} → {store_name}",
			)
			frappe.db.commit()  # nosemgrep -- persist error log; continue with next item
		time.sleep(_INTER_ITEM_DELAY)

	_publish(status="done")

	summary = (
		f"Sync complete for {store_name}.<br>"
		f"Total: {total} | Synced: {done - errors} | Errors: {errors}"
	)
	from fateh_shopify_connector.fateh_shopify_connector.utils import create_shopify_log

	create_shopify_log(
		status="Success" if errors == 0 else "Warning",
		method="sync_all_items",
		shopify_store=store_name,
		message=summary,
	)
	logger.info("Sync orchestrator complete: store=%s total=%d errors=%d", store_name, total, errors)
