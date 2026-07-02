# TechPro CRM — Feature Parity Checklist (HTML/JS version vs. Python port)

✅ = ported and tested · ⚠️ = partially ported · ❌ = not started

## Dashboard
| Feature | Status |
|---|---|
| Sales today / invoice count | ✅ |
| Recent invoices list | ✅ |
| Low stock alert list | ✅ |
| Backup-overdue banner | ✅ |

## Point of Sale
| Feature | Status |
|---|---|
| Product grid, add to cart | ✅ |
| Cart qty/remove | ✅ |
| Editable subtotal override | ✅ |
| $ / % discount toggle | ✅ |
| Canadian tax calc by province | ✅ |
| Customer attach to sale | ✅ |
| Checkout → invoice + stock deduction | ✅ |
| Barcode/SKU scanner text input | ✅ |
| Loyalty points: earn on sale | ✅ |
| Loyalty points: redeem as discount | ✅ |
| Store credit: issue | ✅ |
| Store credit: apply at checkout | ✅ |
| Hold Cart / Recall Held Cart | ✅ |
| Cash tendered / change calculation | ✅ |

## Repairs
| Feature | Status |
|---|---|
| Repair ticket creation (auto-creates customer by phone) | ✅ |
| Kanban board view by status | ✅ |
| Table/list view | ✅ |
| Ticket numbering (#1001, #1002...) | ✅ |
| Status pipeline + advance with notes/history | ✅ |
| Cost tracking (estimated/final) | ✅ |
| "Send ready" SMS notification | ✅ |

## Inventory / Products
| Feature | Status |
|---|---|
| List, add, edit, delete | ✅ |
| Low-stock highlighting | ✅ |
| Bulk CSV import | ✅ |
| Inventory CSV export | ✅ |

## Customers
| Feature | Status |
|---|---|
| List, add | ✅ |
| Detail page: spend, points, store credit, history | ✅ |
| Issue store credit | ✅ |
| Customer CSV export | ✅ |
| Notes field edit UI | ✅ |

## Invoices / Refunds
| Feature | Status |
|---|---|
| List, detail view | ✅ |
| Refund (role-gated) | ✅ |
| Print-formatted receipt | ✅ |
| Email receipt (SMTP, server-side) | ✅ |
| SMS/WhatsApp receipt | ✅ |
| Invoices CSV export | ✅ |
| Tax report CSV export | ✅ |

## Cash Sessions
| Feature | Status |
|---|---|
| Open/close cash session, opening float | ✅ |
| Cash vs. expected variance | ✅ |
| History log | ✅ |

## Staff
| Feature | Status |
|---|---|
| List, add, edit, enable/disable | ✅ |
| PIN hashing (bcrypt) | ✅ |
| Role assignment | ✅ |
| PIN lockout / brute-force protection | ✅ |

## Settings
| Tab | Status |
|---|---|
| Shop info (name, address, phone, email) | ✅ |
| Tax / province | ✅ |
| Invoice prefix | ✅ |
| Loyalty program rules | ✅ |
| Email (SMTP) config | ✅ |
| SMS (Twilio) config | ✅ |
| Cloud/sync config | N/A — no Firebase in this architecture |
| Danger Zone (wipe data, owner-only) | ✅ |

## Reports
| Feature | Status |
|---|---|
| Revenue this month + trend vs. last month | ✅ |
| Tax collected | ✅ |
| Gross profit (revenue − product cost) | ✅ |
| Revenue by category | ✅ |
| Revenue by payment method | ✅ |
| Top products by units sold | ✅ |

## Audit Log
| Feature | Status |
|---|---|
| View log, role-gated | ✅ |
| Coverage: login/logout, sales, refunds, staff changes, settings, repairs, cash-ups, backups, notifications | ✅ |

## Data / Backup
| Feature | Status |
|---|---|
| Full JSON backup export | ✅ |
| Backup restore | ✅ — restores products/customers/repairs/settings; invoices intentionally untouched (see note) |
| Backup-overdue reminder | ✅ |
| `crm.db` itself as a simple backup | ✅ |

## Security (real, server-side — not present in the original)
| Feature | Status |
|---|---|
| Server-side PIN/role enforcement | ✅ |
| XSS protection (Jinja2 autoescaping) | ✅ |
| No client-exposed secrets | ✅ |
| Email/SMS credentials never reach the browser | ✅ |

---

## What's deliberately different from the original (not a bug — a judgment call)

1. **Data wipe vs. restore is intentionally conservative.** The original's "wipe all data" also reset settings to factory defaults. This version's wipe leaves settings, staff accounts, and the audit log alone — wiping live sales/customer data shouldn't also lock everyone out or rename your shop.
2. **Backup restore never touches invoices.** Overwriting sales history from a "restore my catalog" action is rarely what's actually wanted. If you do want a way to restore invoices too, say so and I'll add it as an explicit, separately-confirmed action.
3. **Staff PINs are excluded from backup files.** A leaked backup file shouldn't double as a leaked set of login credentials. PINs need to be re-set after a restore.
4. **No Cloudflare Worker needed for SMS/Email.** The original needed a proxy because browsers can't safely call Twilio directly. This version's Twilio/SMTP calls happen server-side, so that whole problem doesn't exist here.

## Fully ported

All items from the original checklist are now ported and tested,
including the three remaining gaps from the previous pass: CSV bulk
import for inventory, SMS receipts for invoices, and the customer notes
edit form.

---

## Update — modern invoice design, brand dropdown, logo, and Day-to-Day report (added per direct comparison against a later HTML version)

| Feature | Status |
|---|---|
| Modern "eco-elegant" invoice design (matches original's printInvoice styling) | ✅ |
| Shop logo embedded in invoice | ✅ — extracted from the reference file into `static/logo.jpg` |
| GST/HST and PST/QST registration numbers on invoice | ✅ — new Settings fields |
| Category + Brand (subcategory) dropdowns for products, dependent on each other | ✅ |
| Brand shown on POS product tiles + filter dropdown | ✅ |
| Product edit page (previously had a backend route but no UI) | ✅ |
| SKU retained per invoice line (survives product deletion) | ✅ |
| Day-to-Day / End-of-Day report with month navigation | ✅ |
| Print full month report | ✅ |
| Print single-day report | ✅ |

All tested via the same regression pattern as the rest of this build.

---

## New features added beyond the original (not in the HTML version at all)

| Feature | Status |
|---|---|
| Low-stock reorder list (per-product reorder threshold/qty, print sheet, CSV export) | ✅ |
| Daily owner digest email (revenue, repairs, low stock, cash-up status — scheduled nightly via APScheduler, plus a manual "send test now" button) | ✅ |

Bonus fix while building these: the Settings page previously had two
separate forms that both posted to `/settings` — saving one could
silently wipe fields only present in the other (e.g. saving Shop Info
would blank out your SMTP/Twilio credentials). Consolidated into one
form so this can't happen anymore.
