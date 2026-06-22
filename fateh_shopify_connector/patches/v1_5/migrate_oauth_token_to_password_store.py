# Copyright (c) 2024, siva@enfono.com and contributors
# For license information, please see license.txt

import frappe


def execute():
	"""Migrate OAuth access tokens from plain DB column to Frappe's encrypted __Auth table.

	Previously, oauth.callback() used db_set("access_token", ...) which stored the token
	in the plain column. get_decrypted_password() reads from __Auth, so the token was
	invisible at runtime. This patch moves any plain-column tokens into __Auth.
	"""
	from frappe.utils.password import update_password

	# Read directly from DB — frappe.get_all masks Password fields
	stores = frappe.db.sql(
		"""
		SELECT name, access_token
		FROM `tabShopify Store`
		WHERE access_token IS NOT NULL
		  AND access_token != ''
		  AND access_token NOT LIKE '**%'
		""",
		as_dict=True,
	)

	migrated = 0
	for store in stores:
		plain_token = store.get("access_token", "").strip()
		if not plain_token:
			continue

		update_password("Shopify Store", store.name, "access_token", plain_token)
		# Clear plain column so the token isn't exposed in DB dumps
		frappe.db.sql(
			"UPDATE `tabShopify Store` SET access_token = '' WHERE name = %s",
			store.name,
		)
		migrated += 1

	if migrated:
		frappe.db.commit()  # nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- patch: explicit commit after bulk migration
		print(f"Migrated {migrated} Shopify Store access token(s) to encrypted password store.")
	else:
		print("No plain-column access tokens found to migrate.")
