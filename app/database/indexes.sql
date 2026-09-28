-- ---------------------------------------------------------------------------
-- Indexes, created AFTER bulk loading so the COPY is not slowed by index
-- maintenance. These cover the columns the analyst actually filters, groups
-- and joins on: foreign keys, timestamps and the low-cardinality categoricals.
-- ---------------------------------------------------------------------------

-- Foreign keys (PostgreSQL does not index these automatically).
CREATE INDEX IF NOT EXISTS idx_orders_user_id           ON orders (user_id);
CREATE INDEX IF NOT EXISTS idx_order_items_order_id     ON order_items (order_id);
CREATE INDEX IF NOT EXISTS idx_order_items_product_id   ON order_items (product_id);
CREATE INDEX IF NOT EXISTS idx_order_items_user_id      ON order_items (user_id);
CREATE INDEX IF NOT EXISTS idx_reviews_order_id         ON reviews (order_id);
CREATE INDEX IF NOT EXISTS idx_reviews_product_id       ON reviews (product_id);
CREATE INDEX IF NOT EXISTS idx_reviews_user_id          ON reviews (user_id);
CREATE INDEX IF NOT EXISTS idx_events_user_id           ON events (user_id);
CREATE INDEX IF NOT EXISTS idx_events_product_id        ON events (product_id);

-- Time columns (monthly trends, date filters).
CREATE INDEX IF NOT EXISTS idx_orders_order_date        ON orders (order_date);
CREATE INDEX IF NOT EXISTS idx_reviews_review_date      ON reviews (review_date);
CREATE INDEX IF NOT EXISTS idx_events_event_timestamp   ON events (event_timestamp);
CREATE INDEX IF NOT EXISTS idx_users_signup_date        ON users (signup_date);

-- Categoricals used in WHERE / GROUP BY.
CREATE INDEX IF NOT EXISTS idx_orders_order_status      ON orders (order_status);
CREATE INDEX IF NOT EXISTS idx_events_event_type        ON events (event_type);
CREATE INDEX IF NOT EXISTS idx_products_category        ON products (category);
CREATE INDEX IF NOT EXISTS idx_products_brand           ON products (brand);
CREATE INDEX IF NOT EXISTS idx_users_city              ON users (city);

-- Composite: the funnel queries scan events by (product, type) and (user, type).
CREATE INDEX IF NOT EXISTS idx_events_product_type      ON events (product_id, event_type);
CREATE INDEX IF NOT EXISTS idx_events_user_type         ON events (user_id, event_type);
