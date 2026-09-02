# Payzum Payments for Frappe / ERPNext

Accept crypto and stablecoin payments (USDC, USDT and more, multi-chain) in any
Frappe or ERPNext site. [Payzum](https://payzum.com) is **non-custodial**: funds
settle directly to your own wallet.

The app plugs into the official [frappe/payments](https://github.com/frappe/payments)
framework: it registers a **Payzum** payment gateway that any consumer of
`get_payment_gateway_controller` (Payment Requests, web forms, custom code) can
use, exactly like the built-in gateways.

- Buyers are redirected to a hosted checkout page where they pick the asset and
  network; your site charges in fiat terms (e.g. 29.99 USD). No card fields, no
  PCI surface.
- Orders are fulfilled from the **signed payment notification** (HMAC-SHA-512,
  verified by the official [`payzum`](https://pypi.org/project/payzum/) Python
  SDK), never from the buyer's browser return.
- Redelivered notifications are idempotent, and a late `expired` event can never
  downgrade an order that already settled.

## Requirements

- Frappe v15+ with the [payments](https://github.com/frappe/payments) app installed
- Python 3.10+
- A publicly reachable site URL (Payzum delivers notifications server-to-server)

## Installation

```bash
bench get-app https://github.com/payzum-dev/frappe-payzum
bench --site yoursite install-app payzum_payments
```

## Configuration

Open **Payzum Settings** (single doctype) on your site:

| Field | Value |
|---|---|
| API Key | From **Dashboard → Settings → API Keys** at [merchant.payzum.com](https://merchant.payzum.com) |
| Webhook Secret | Shown once, at merchant creation or rotation |
| Use Sandbox | Point at `staging.payzum.com` — isolated environment, **separate API keys** |
| Pay Currency | `all` (default) lets the buyer pick on the hosted page; or pin a code like `usdcmatic` |

Saving validates the API key against the live API.

Every invoice the app creates carries its own `ipn_callback_url` pointing back
at your site, so no webhook needs to be configured in the Payzum dashboard.

## Usage from code

```python
from payments.utils import get_payment_gateway_controller

controller = get_payment_gateway_controller("Payzum")
controller.validate_transaction_currency("USD")

url = controller.get_payment_url(
    amount=29.99,
    currency="USD",
    title="Payment for SO-0001",
    description="Sales Order SO-0001",
    reference_doctype="Payment Request",
    reference_docname="PR-0001",
    payer_email="buyer@example.com",
)
# redirect the buyer to `url`
```

When the invoice settles, the reference document's `on_payment_authorized("Completed")`
is called — the same contract every gateway in frappe/payments honours.

## Statuses

The gateway emits five payment statuses; only `finished` fulfils:

| Payzum status | Effect |
|---|---|
| `finished` | `on_payment_authorized("Completed")`, Integration Request → Completed |
| `expired`, `failed` | Integration Request → Failed |
| `waiting`, `partially_paid` | No change (`partially_paid` means underpaid) |

## Tests

```bash
bench --site yoursite run-tests --app payzum_payments
```

The suite exercises the notification handler with genuinely signed bodies:
signature rejection, replay-window rejection, fulfilment, idempotent
redelivery, and the never-downgrade guard.

## License

MIT
