# Database Guide

This document explains what each table represents, how the tables relate, and
what the important business terms mean. It exists for two audiences: a human
reading the codebase, and (optionally) a retrieval step that could feed this
text to the LLM before SQL generation — the content below is written to stand
on its own for either.

The authoritative schema is always the live PostgreSQL catalog (see
`app/database/schema_inspector.py`); this guide is the narrative version of
the same facts, kept in sync with `app/database/schema.sql`.

## Tables

### `users` (10,000 rows)

One row per registered customer.

| column | meaning |
|---|---|
| `user_id` | Primary key, format `U000001`. |
| `name` | Customer full name. |
| `email` | Unique per customer. |
| `gender` | `Male`, `Female`, or `Other`. |
| `city` | Customer's city — the basis for "which city has the most customers." |
| `signup_date` | Date only, 2024-01-01 to 2025-11-14. |

### `products` (2,000 rows)

The product catalog.

| column | meaning |
|---|---|
| `product_id` | Primary key, format `P000001`. |
| `product_name` | Display name. |
| `category` | One of 10 categories (Clothing, Groceries, Sports, Electronics, Home & Kitchen, Toys, Pet Supplies, Books, Automotive, Beauty). |
| `brand` | One of 12 brands. |
| `price` | Catalog list price. **Not** the transaction price — see `order_items.item_price` for that. |
| `rating` | Pre-aggregated catalog rating, 2.50–4.90. This is a summary number on the product itself, distinct from the individual `reviews.rating` values. |

### `orders` (20,000 rows)

One row per order placed.

| column | meaning |
|---|---|
| `order_id` | Primary key, format `O00000001`. |
| `user_id` | FK to `users`. |
| `order_date` | Timestamp, 2024-01-01 to 2025-11-14. |
| `order_status` | `completed`, `shipped`, `processing`, `cancelled`, or `returned` — roughly 4,000 orders in each status. |
| `total_amount` | Order total. Verified to exactly equal `SUM(order_items.item_total)` for that order across all 20,000 orders. |

### `order_items` (43,525 rows)

One row per product per order — the line-item detail behind each order. This
is the table to use for **product-level** revenue and units-sold questions.

| column | meaning |
|---|---|
| `order_item_id` | Primary key, format `I00000001`. |
| `order_id` | FK to `orders`. |
| `product_id` | FK to `products`. |
| `user_id` | FK to `users` — a denormalised copy of `orders.user_id` for convenience; either path reaches the same customer. |
| `quantity` | Units on this line: 1, 2, or 3. |
| `item_price` | Unit price actually paid (may differ from `products.price`). |
| `item_total` | Line revenue = `quantity * item_price`. |

### `reviews` (15,000 rows)

Customer reviews, each tied to a specific order and product.

| column | meaning |
|---|---|
| `review_id` | Primary key, format `R00000001`. |
| `order_id` | FK to `orders`. |
| `product_id` | FK to `products`. |
| `user_id` | FK to `users`. |
| `rating` | Integer 1–5, the individual customer's rating. |
| `review_text` | Free text — but only **10 distinct canned sentences** appear across all 15,000 rows, so it carries no real analytical signal. Count/aggregate reviews rather than treating this as free-text data. |
| `review_date` | Timestamp of the review. |

Note: a given `(order_id, product_id)` pair can appear in more than one
review (6 such pairs exist in this dataset), so `review_id` alone is the key —
don't assume `(order_id, product_id)` is unique.

### `events` (80,000 rows)

Behavioural clickstream log: what a user did with a product.

| column | meaning |
|---|---|
| `event_id` | Primary key, format `E00000001`. |
| `user_id` | FK to `users`. |
| `product_id` | FK to `products`. |
| `event_type` | `view` (56,013), `cart` (12,035), `wishlist` (7,946), `purchase` (4,006). |
| `event_timestamp` | Timestamp, 2024-01-01 to 2025-11-14. |

**`events` has no `order_id` and cannot be joined to `orders` or
`order_items`.** A `purchase` event is a behavioural signal, not a financial
record — it does not correspond 1:1 with a row in `orders`. This means:

- Funnel questions (view → cart → purchase, conversion rate, "viewed but not
  bought") must be answered from `events` alone.
- Revenue and monetary questions must be answered from `order_items`/`orders`.
- The two subgraphs meet only through `user_id` and `product_id`.

## Relationships

```
users (user_id PK)
 ├──< orders (order_id PK, user_id FK)
 │      └──< order_items (order_item_id PK, order_id FK, product_id FK, user_id FK)
 ├──< order_items (user_id FK — denormalised copy of orders.user_id)
 ├──< reviews (review_id PK, order_id FK, product_id FK, user_id FK)
 └──< events (event_id PK, user_id FK, product_id FK)

products (product_id PK)
 ├──< order_items (product_id FK)
 ├──< reviews (product_id FK)
 └──< events (product_id FK)
```

Referential integrity was measured directly against the CSVs before loading:
every foreign key above resolves with **zero orphan rows**, and every
`orders.total_amount` matches `SUM(order_items.item_total)` exactly.

## Business definitions

- **Revenue** = `SUM(order_items.item_total)`. Never add `orders.total_amount`
  to item totals — the former is already the sum of the latter, so combining
  them double-counts.
- **Units sold** = `SUM(order_items.quantity)`.
- **Average order value (AOV)** = `AVG(orders.total_amount)`, equivalently
  `SUM(order_items.item_total) / COUNT(DISTINCT order_items.order_id)`.
- **Realised vs. gross revenue**: `orders.order_status` includes `cancelled`
  and `returned` orders, which still carry a `total_amount`. A question about
  revenue that was actually collected should filter to `completed`/`shipped`;
  a question about order volume or gross bookings generally should not filter.
- **Catalog rating vs. review rating**: `products.rating` is one pre-computed
  number per product; `reviews.rating` is the raw 1–5 per-review data. Prefer
  `products.rating` for "which products are rated highest" (it is already the
  catalog's answer to that question); use `AVG(reviews.rating)` when a
  question is explicitly about review behaviour.
- **Funnel / conversion**: computed entirely within `events`, e.g. view-to-
  purchase rate = users with a `purchase` event for a product ÷ users with a
  `view` event for that product.
