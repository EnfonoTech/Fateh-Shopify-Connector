# Copyright (c) 2024, siva@enfono.com and contributors
# For license information, please see license.txt

import json

import frappe


def create_shopify_log(
	method=None,
	status="Queued",
	request_data=None,
	response_data=None,
	message=None,
	exception=None,
	shopify_store=None,
	reference_doctype=None,
	reference_name=None,
):
	"""Create a Fateh Shopify Log entry."""
	if isinstance(request_data, (dict, list)):
		request_data = json.dumps(request_data, indent=2)
	if isinstance(response_data, (dict, list)):
		response_data = json.dumps(response_data, indent=2)

	if exception and not message:
		message = str(exception)

	log = frappe.get_doc(
		{
			"doctype": "Fateh Shopify Log",
			"status": status,
			"method": method,
			"shopify_store": shopify_store,
			"request_data": request_data,
			"response_data": response_data,
			"message": message,
			"traceback": frappe.get_traceback() if exception else None,
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
		}
	)
	log.flags.ignore_permissions = True
	log.insert(ignore_permissions=True)
	frappe.db.commit()
	return log
