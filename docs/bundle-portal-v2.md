# Campus Bundle Portal setup

Campus customer and store checkout now recognise the same provider IDs Nagonu syncs:

| Service | Provider IDs |
| --- | --- |
| MTN Normal / MTN Express | `bundleportal_mtn`, `bundleportal_mtn2`, `bundleportal_mtn3` |
| AT iShare | `bundleportal_ishare` |
| Telecel / Vodafone | `bundleportal_telecel` |

Keep each service ON/API. Choose its provider in Nagonu's Campus Services screen or sync a provider change from Nagonu's main Services screen (matching service IDs). Campus's own service editor also supports these choices. Bigtime is not mapped to iShare.

Set `BUNDLE_PORTAL_KEY` on the Campus host. The existing `BUNDLEPORTAL_API_KEY` environment variable is accepted as a fallback; embedded key fallback has been removed. New routes always use `https://api.bundleportal.com/v2`.

## Shared Bundle Portal account with Nagonu

Deploy the updated Nagonu callback and Campus code together. Keep the existing registered webhook at `https://nagonu.site/webhooks/bundleportal`. Nagonu now searches Campus orders for the new `BPC_` references after checking its own orders. Keep the existing webhook secret on Nagonu. Do not call `set_webhook` again for Campus on that shared account: it may replace the Nagonu registration and rotate the secret.

## Separate Campus Bundle Portal account

Register `https://campusdata.store/webhooks/bundleportal` on the separate account and set its returned secret as `BUNDLE_PORTAL_WEBHOOK_SECRET` on Campus. Use that account's key for `BUNDLE_PORTAL_KEY`. Restart the app after configuration. This callback only updates Campus orders.

## Behaviour and checks

New orders retain their route and `BPC_` reference. Catalogue and recipient checks precede submission. Uncertain results and transient failures remain for review; admin POST `/admin/orders/<mongo_id>/items/<index>/bundleportal-retry` reuses the original reference. Confirmed failures require the existing admin refund workflow. Campus's existing provider-wallet debit is preserved for every new provider ID. No status polling is used for these routes.

The legacy generic `bundleportal` choice remains for compatibility. Switch services to the explicit new routes to use the verified v2 preflight and webhook workflow. Existing generic-provider orders are not migrated automatically.

Install `mongomock` for tests, then from the Campus directory run:

```text
python -m unittest discover -s tests -p test_bundle_portal_v2.py -v
python -m unittest discover -s tests -p test_bundle_portal_callbacks.py -v
```

These are mocked tests, including execution of the actual customer/store routing branches. Live provider acceptance and delivery still require deployment and a live order. Bundle Portal does not retry missed webhooks; reconcile missed callbacks in its dashboard.
