# Edge Report (public) + multi-offer Stripe→Resend sync

Public track record and issue previews for Edge Report ($9/mo).

- Track record: `/` (index.html)
- Issue #001 preview: `/issues/001.html`
- Issue #002 preview: `/issues/002.html`
- Data: `track-record.json`

## Webhook worker (`app.py`)

Free Render web service `edge-report-stripe-sync` routes Stripe `checkout.session.completed` / subscription events by **price ID**:

| Offer | Price ID env | Action |
|---|---|---|
| Edge Report | `EDGE_REPORT_PRICE_ID` | Resend segment + welcome |
| Scout Digest | `SCOUT_DIGEST_PRICE_ID` | Resend segment + welcome |
| Noise Alter Signal Bank 001 | `NA_PRESETS_PRICE_ID` | Contact prop `na_presets_status=preorder` + confirm email |

Mutations require `EDGE_REPORT_ALLOW_SYNC=1`. Test helpers: `POST /test/dry-route` (no send), `POST /test/send-sample` (bot inbox only).
