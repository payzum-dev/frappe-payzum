# Copyright (c) 2026, Payzum and contributors
# License: MIT. See license.txt

"""
# Integrating Payzum

Create a payment service in ERPNext (or call the controller directly):

	from payments.utils import get_payment_gateway_controller

	controller = get_payment_gateway_controller("Payzum")
	controller.validate_transaction_currency(currency)
	url = controller.get_payment_url(**payment_details)  # redirect the buyer here

The buyer pays on Payzum's hosted checkout page (picking the crypto asset and
network there), and the order is fulfilled from the signed payment notification
(IPN) — never from the buyer's browser return. A closed tab must not lose a
paid order.
"""

import json
import re
from contextlib import suppress
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.model.document import Document
from frappe.utils import call_hook_method, cint, cstr, flt, get_url

from payments.utils import create_payment_gateway
from payzum import ApiError, PaymentStatus, Payzum, PayzumError, SignatureError

api_path = "/api/method/payzum_payments.payzum_payments.doctype.payzum_settings.payzum_settings"

# price_currency is free-form on the API (fiat like USD/EUR or a crypto ticker),
# constrained only in shape. A currency the rate provider cannot convert fails
# the create call with CURRENCY_NOT_SUPPORTED, surfaced as a readable message.
CURRENCY_RE = re.compile(r"[A-Za-z0-9]{2,8}")


class PayzumSettings(Document):
	def validate(self):
		create_payment_gateway("Payzum")
		call_hook_method("payment_gateway_enabled", gateway="Payzum")
		if not self.flags.ignore_mandatory:
			self.validate_payzum_credentials()

	def validate_payzum_credentials(self):
		try:
			self.client().payments.list(limit=1)
		except ApiError as exc:
			frappe.throw(
				_("Payzum rejected the API key: {0}. The sandbox environment needs its own key.").format(
					exc
				)
			)
		except PayzumError as exc:
			frappe.throw(_("Could not reach the Payzum API: {0}").format(exc))

	def client(self) -> Payzum:
		api_key = cstr(self.get_password(fieldname="api_key", raise_exception=False))
		if cint(self.use_sandbox):
			return Payzum.sandbox(api_key)
		return Payzum(api_key)

	def validate_transaction_currency(self, currency):
		if not CURRENCY_RE.fullmatch(cstr(currency)):
			frappe.throw(
				_(
					"Please select another payment method. Payzum cannot charge in currency '{0}'"
				).format(currency)
			)

	def get_payment_url(self, **kwargs):
		"""Create a hosted-checkout invoice and return its URL."""
		integration_request = create_request_log(kwargs, service_name="Payzum")

		try:
			invoice = self.client().payments.create(
				price_amount=cstr(flt(kwargs.get("amount"), 2)),
				price_currency=cstr(kwargs.get("currency")).lower(),
				pay_currency=cstr(self.pay_currency).strip().lower() or "all",
				order_id=integration_request.name,
				order_description=cstr(kwargs.get("description") or kwargs.get("title"))[:2000]
				or None,
				ipn_callback_url=get_url(f"{api_path}.ipn_handler"),
				success_url=get_url(f"{api_path}.finish?token={integration_request.name}"),
				cancel_url=get_url(
					f"{api_path}.finish?token={integration_request.name}&cancelled=1"
				),
				# The API does not enforce order_id uniqueness; without this key a
				# retried create could mint a second real invoice.
				idempotency_key=integration_request.name,
			)
		except PayzumError as exc:
			integration_request.db_set("status", "Failed")
			integration_request.db_set("error", cstr(exc))
			frappe.log_error(frappe.get_traceback(), "Payzum invoice creation failed")
			frappe.throw(_("Could not create the Payzum invoice: {0}").format(exc))

		# Older docs used `id`; keep the fallback when reading the payment id.
		payment_id = invoice.get("payment_id") or invoice.get("id")
		invoice_url = invoice.get("invoice_url")
		if not invoice_url:
			# invoice_url is null when the gateway has no checkout base configured
			# for the merchant.
			integration_request.db_set("status", "Failed")
			frappe.throw(
				_(
					"Payzum did not return a checkout URL (invoice {0}). Contact Payzum support."
				).format(payment_id)
			)

		integration_request.db_set("output", json.dumps({"payment_id": payment_id}))
		return invoice_url


@frappe.whitelist(allow_guest=True)
def ipn_handler(**kwargs):
	"""Signed payment notification (IPN) — the only place an order is fulfilled.

	Payzum retries a delivery up to five times ~30s apart whatever the response
	code, and re-delivers settled invoices, so this handler is idempotent and
	the rejection paths stay cheap and side-effect-free.
	"""
	raw_body = frappe.request.get_data() or b""
	secret = cstr(
		frappe.get_doc("Payzum Settings").get_password(
			fieldname="webhook_secret", raise_exception=False
		)
	)

	try:
		# Signature (HMAC-SHA-512 over the raw bytes) and the replay window are
		# checked before any field is readable.
		payload = Payzum.webhooks(secret).verify_payment_ipn(raw_body, frappe.request.headers)
	except (SignatureError, PayzumError):
		frappe.local.response.http_status_code = 401
		return "invalid signature"

	order_id = cstr(payload.get("order_id"))
	if not order_id or not frappe.db.exists("Integration Request", order_id):
		# Not one of ours — e.g. a dashboard "send test webhook" with a
		# synthetic order_id.
		frappe.local.response.http_status_code = 404
		return "unknown order"

	try:
		status = PaymentStatus.from_merchant(cstr(payload.get("payment_status")))
	except PayzumError:
		# A value from a future API version: acknowledge rather than 500.
		frappe.log_error(
			f"Unknown Payzum payment_status {payload.get('payment_status')!r} "
			f"for Integration Request {order_id}",
			"Payzum IPN ignored",
		)
		return "ignored"

	# Row lock so a manual replay racing a scheduled retry serializes here.
	current = frappe.db.get_value("Integration Request", order_id, "status", for_update=True)
	if current == "Completed":
		# A redelivered terminal event must never downgrade a settled order.
		return "already processed"

	if status.is_paid():
		if not _finalize_success(order_id):
			# Persist the failure but answer 5xx so a Payzum retry gets another
			# attempt at fulfilment (e.g. a transient error in the reference
			# doctype's own hook).
			frappe.local.response.http_status_code = 500
			return "error"
		return "ok"

	if status.is_terminal():  # expired or failed
		frappe.db.set_value("Integration Request", order_id, "status", "Failed")
		return "ok"

	# waiting / partially_paid: no state change. A partial payment is underpaid
	# and observable only by polling; it must not fulfil anything.
	return "ignored"


def _finalize_success(order_id) -> bool:
	request = frappe.get_doc("Integration Request", order_id)
	data = frappe._dict(json.loads(request.data))

	try:
		if data.reference_doctype and data.reference_docname:
			frappe.get_doc(data.reference_doctype, data.reference_docname).run_method(
				"on_payment_authorized", "Completed"
			)
		request.db_set("status", "Completed")
		return True
	except Exception:
		request.db_set("status", "Failed")
		frappe.log_error(frappe.get_traceback(), "Payzum payment authorization failed")
		return False


@frappe.whitelist(allow_guest=True)
def finish(token=None, cancelled=None, **kwargs):
	"""Buyer's browser return from the hosted checkout.

	Crypto confirmation is asynchronous — the buyer usually lands here before
	the invoice is final — so this never changes payment state; it only routes
	the browser. Fulfilment happens in ipn_handler.
	"""
	data = {}
	if token and frappe.db.exists("Integration Request", cstr(token)):
		with suppress(Exception):
			data = json.loads(
				frappe.db.get_value("Integration Request", cstr(token), "data") or "{}"
			)

	redirect_url = "payment-failed" if cint(cancelled) else "payment-success"
	params = {}
	if data.get("redirect_to"):
		params["redirect_to"] = data["redirect_to"]
	if data.get("redirect_message"):
		params["redirect_message"] = data["redirect_message"]
	if params:
		redirect_url += "?" + urlencode(params)

	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = redirect_url
