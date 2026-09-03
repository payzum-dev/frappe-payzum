# Changelog

## 0.2.0 — 2026-09-04

- The payment notification now verifies the invoice's `price_amount` and
  `price_currency` against the Integration Request before fulfilling it, and
  fails closed when either field is missing or unreadable. A signed
  notification for a different amount or currency acknowledges the delivery
  but never completes the payment.

## 0.1.0 — 2026-09-02

- Initial release: Payzum payment gateway for Frappe/ERPNext — hosted checkout
  redirect, signature-verified IPN (HMAC-SHA-512 over the raw bytes),
  idempotent fulfilment.
