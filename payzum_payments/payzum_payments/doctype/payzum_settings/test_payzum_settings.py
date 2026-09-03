# Copyright (c) 2026, Payzum and contributors
# License: MIT. See license.txt

import hashlib
import hmac
import json
import time

import frappe
from frappe.tests.utils import FrappeTestCase
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from payzum_payments.payzum_payments.doctype.payzum_settings import payzum_settings as mod

SECRET = "test-webhook-secret"


def signed_ipn(payload: dict, secret: str = SECRET, sig: str | None = None) -> Request:
	body = json.dumps(payload, sort_keys=True).encode()
	sig = sig if sig is not None else hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()
	builder = EnvironBuilder(
		method="POST",
		data=body,
		headers={"x-nowpayments-sig": sig, "content-type": "application/json"},
	)
	return Request(builder.get_environ())


def ipn_body(order_id: str, payment_status: str, **overrides) -> dict:
	# The real body is the full NpPayment shape; the handler only reads these.
	body = {
		"payment_id": "pzi_test000000000000000000",
		"payment_status": payment_status,
		"order_id": order_id,
		"event_at": int(time.time()),
		"event_id": "ipn_" + "0" * 32,
		"price_amount": "29.99",
		"price_currency": "usd",
	}
	body.update(overrides)
	return {k: v for k, v in body.items() if v is not None}


class TestPayzumSettings(FrappeTestCase):
	def setUp(self):
		settings = frappe.get_doc("Payzum Settings")
		settings.api_key = "0" * 64
		settings.webhook_secret = SECRET
		settings.flags.ignore_mandatory = True  # skip the live credentials probe
		settings.save()
		frappe.local.response = frappe._dict()

	def make_integration_request(self) -> str:
		request = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "Payzum",
				"status": "Queued",
				"data": json.dumps({"amount": 29.99, "currency": "USD"}),
			}
		).insert(ignore_permissions=True)
		return request.name

	def call_ipn(self, req: Request) -> str:
		frappe.local.request = req
		try:
			return mod.ipn_handler()
		finally:
			frappe.local.request = None

	def request_status(self, name: str) -> str:
		return frappe.db.get_value("Integration Request", name, "status")

	def test_bad_signature_is_rejected(self):
		req = signed_ipn(ipn_body("whatever", "finished"), sig="00" * 64)
		self.assertEqual(self.call_ipn(req), "invalid signature")
		self.assertEqual(frappe.local.response.http_status_code, 401)

	def test_stale_event_is_rejected(self):
		body = ipn_body("whatever", "finished")
		body["event_at"] = int(time.time()) - 3600  # outside the replay window
		self.assertEqual(self.call_ipn(signed_ipn(body)), "invalid signature")
		self.assertEqual(frappe.local.response.http_status_code, 401)

	def test_unknown_order_is_a_cheap_404(self):
		req = signed_ipn(ipn_body("NO-SUCH-REQUEST", "finished"))
		self.assertEqual(self.call_ipn(req), "unknown order")
		self.assertEqual(frappe.local.response.http_status_code, 404)

	def test_finished_completes_the_request(self):
		name = self.make_integration_request()
		self.assertEqual(self.call_ipn(signed_ipn(ipn_body(name, "finished"))), "ok")
		self.assertEqual(self.request_status(name), "Completed")

	def test_amount_mismatch_never_fulfils(self):
		name = self.make_integration_request()
		req = signed_ipn(ipn_body(name, "finished", price_amount="0.01"))
		self.assertEqual(self.call_ipn(req), "amount mismatch")
		self.assertEqual(self.request_status(name), "Queued")

	def test_missing_amount_never_fulfils(self):
		# Fail closed: a signed notification without a readable amount must not fulfil.
		name = self.make_integration_request()
		req = signed_ipn(ipn_body(name, "finished", price_amount=None))
		self.assertEqual(self.call_ipn(req), "amount mismatch")
		self.assertEqual(self.request_status(name), "Queued")

	def test_wrong_currency_never_fulfils(self):
		name = self.make_integration_request()
		req = signed_ipn(ipn_body(name, "finished", price_currency="jpy"))
		self.assertEqual(self.call_ipn(req), "amount mismatch")
		self.assertEqual(self.request_status(name), "Queued")

	def test_amount_within_half_cent_fulfils(self):
		# "29.99" recorded, "29.990" delivered — decimal comparison, not string equality.
		name = self.make_integration_request()
		req = signed_ipn(ipn_body(name, "finished", price_amount="29.990"))
		self.assertEqual(self.call_ipn(req), "ok")
		self.assertEqual(self.request_status(name), "Completed")

	def test_redelivery_of_a_settled_invoice_is_a_noop(self):
		name = self.make_integration_request()
		self.call_ipn(signed_ipn(ipn_body(name, "finished")))
		self.assertEqual(
			self.call_ipn(signed_ipn(ipn_body(name, "finished"))), "already processed"
		)
		self.assertEqual(self.request_status(name), "Completed")

	def test_expired_never_downgrades_a_settled_invoice(self):
		name = self.make_integration_request()
		self.call_ipn(signed_ipn(ipn_body(name, "finished")))
		self.assertEqual(
			self.call_ipn(signed_ipn(ipn_body(name, "expired"))), "already processed"
		)
		self.assertEqual(self.request_status(name), "Completed")

	def test_expired_fails_a_pending_invoice(self):
		name = self.make_integration_request()
		self.assertEqual(self.call_ipn(signed_ipn(ipn_body(name, "expired"))), "ok")
		self.assertEqual(self.request_status(name), "Failed")

	def test_partially_paid_fulfils_nothing(self):
		name = self.make_integration_request()
		self.assertEqual(self.call_ipn(signed_ipn(ipn_body(name, "partially_paid"))), "ignored")
		self.assertEqual(self.request_status(name), "Queued")

	def test_unknown_status_is_acknowledged_not_500(self):
		name = self.make_integration_request()
		self.assertEqual(self.call_ipn(signed_ipn(ipn_body(name, "refunded"))), "ignored")
		self.assertEqual(self.request_status(name), "Queued")

	def test_currency_validation_is_shape_only(self):
		settings = frappe.get_doc("Payzum Settings")
		settings.validate_transaction_currency("USD")  # no throw
		settings.validate_transaction_currency("usdt")  # crypto tickers pass too
		self.assertRaises(frappe.ValidationError, settings.validate_transaction_currency, "")
		self.assertRaises(
			frappe.ValidationError, settings.validate_transaction_currency, "not a currency"
		)
