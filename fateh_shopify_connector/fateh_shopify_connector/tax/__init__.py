# Copyright (c) 2024, siva@enfono.com and contributors
# For license information, please see license.txt

"""
Tax handling module for Fateh Shopify Connector Connector.

This module provides clean architecture for building ERPNext tax rows from Shopify order data.

Components:
- TaxBuilder: Main orchestrator for building tax rows
- TaxDetector: Detects zero-rated items from Shopify data
- ShippingTaxHandler: Handles shipping charges and taxes
- RoundingAdjuster: Applies rounding adjustment after SO save

Usage:
    from fateh_shopify_connector.fateh_shopify_connector.tax import TaxBuilder

    builder = TaxBuilder(order, store, items)
    taxes = builder.build()
"""

from fateh_shopify_connector.fateh_shopify_connector.tax.builder import TaxBuilder
from fateh_shopify_connector.fateh_shopify_connector.tax.detector import TaxDetector
from fateh_shopify_connector.fateh_shopify_connector.tax.rounding import apply_rounding_adjustment
from fateh_shopify_connector.fateh_shopify_connector.tax.shipping import ShippingTaxHandler

__all__ = [
	"ShippingTaxHandler",
	"TaxBuilder",
	"TaxDetector",
	"apply_rounding_adjustment",
]
