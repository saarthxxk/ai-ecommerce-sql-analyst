-- ---------------------------------------------------------------------------
-- Schema for the AI E-commerce SQL Analyst.
--
-- Every column below was derived from the actual CSV headers and measured
-- value ranges in ecommerce_dataset/*.csv. Nothing here is invented.
--
-- The COMMENT statements are not decoration: app/database/schema_inspector.py
-- reads them back out of the catalog and feeds them to the LLM, so the model
-- learns business meaning from the database itself rather than from a
-- hardcoded prompt string.
-- ---------------------------------------------------------------------------

DROP TABLE IF EXISTS events CASCADE;
DROP TABLE IF EXISTS reviews CASCADE;
DROP TABLE IF EXISTS order_items CASCADE;
DROP TABLE IF EXISTS orders CASCADE;
DROP TABLE IF EXISTS products CASCADE;
DROP TABLE IF EXISTS users CASCADE;

-- --------------------------------------------------------------------- users
CREATE TABLE users (
    user_id     VARCHAR(16) PRIMARY KEY,
    name        TEXT        NOT NULL,
    email       TEXT        NOT NULL,
    gender      VARCHAR(16),
    city        TEXT,
    signup_date DATE
);

COMMENT ON TABLE  users             IS 'Registered customers. One row per customer. 10,000 rows.';
COMMENT ON COLUMN users.user_id     IS 'Primary key, format U000001. Referenced by orders, order_items, reviews and events.';
COMMENT ON COLUMN users.name        IS 'Customer full name.';
COMMENT ON COLUMN users.email       IS 'Customer email address, unique per customer.';
COMMENT ON COLUMN users.gender      IS 'One of: Male, Female, Other.';
COMMENT ON COLUMN users.city        IS 'Customer city. Use this for "which city has the most customers" style questions.';
COMMENT ON COLUMN users.signup_date IS 'Date the customer registered (date only, no time). Range 2024-01-01 to 2025-11-14.';

-- ------------------------------------------------------------------ products
CREATE TABLE products (
    product_id   VARCHAR(16) PRIMARY KEY,
    product_name TEXT        NOT NULL,
    category     TEXT,
    brand        TEXT,
    price        NUMERIC(10, 2),
    rating       NUMERIC(3, 2)
);

COMMENT ON TABLE  products              IS 'Product catalog. One row per product. 2,000 rows.';
COMMENT ON COLUMN products.product_id   IS 'Primary key, format P000001. Referenced by order_items, reviews and events.';
COMMENT ON COLUMN products.product_name IS 'Display name of the product.';
COMMENT ON COLUMN products.category     IS 'Product category. 10 values: Clothing, Groceries, Sports, Electronics, Home & Kitchen, Toys, Pet Supplies, Books, Automotive, Beauty.';
COMMENT ON COLUMN products.brand        IS 'Product brand. 12 values including Acme, Astra, NeoTech, Nimbus, Orion, Zenith.';
COMMENT ON COLUMN products.price        IS 'Catalog list price. Note: order_items.item_price is the price actually paid and is the correct basis for revenue.';
COMMENT ON COLUMN products.rating       IS 'Catalog-level average rating, 2.50 to 4.90. This is a pre-aggregated product attribute; reviews.rating holds the individual 1-5 customer ratings.';

-- -------------------------------------------------------------------- orders
CREATE TABLE orders (
    order_id     VARCHAR(16) PRIMARY KEY,
    user_id      VARCHAR(16) NOT NULL REFERENCES users (user_id),
    order_date   TIMESTAMP,
    order_status VARCHAR(32),
    total_amount NUMERIC(12, 2)
);

COMMENT ON TABLE  orders              IS 'Order headers. One row per order. 20,000 rows. Join to order_items for line detail.';
COMMENT ON COLUMN orders.order_id     IS 'Primary key, format O00000001.';
COMMENT ON COLUMN orders.user_id      IS 'Customer who placed the order. References users.user_id.';
COMMENT ON COLUMN orders.order_date   IS 'Timestamp the order was placed. Range 2024-01-01 to 2025-11-14. Use date_trunc(''month'', order_date) for monthly trends.';
COMMENT ON COLUMN orders.order_status IS 'One of: completed, shipped, processing, cancelled, returned. Roughly 4,000 orders each. cancelled and returned orders still carry a total_amount, so filter on status when a question is about realised revenue.';
COMMENT ON COLUMN orders.total_amount IS 'Order total. Verified to equal SUM(order_items.item_total) for every order, so never add this to item totals or revenue will be double counted.';

-- --------------------------------------------------------------- order_items
CREATE TABLE order_items (
    order_item_id VARCHAR(16) PRIMARY KEY,
    order_id      VARCHAR(16) NOT NULL REFERENCES orders (order_id),
    product_id    VARCHAR(16) NOT NULL REFERENCES products (product_id),
    user_id       VARCHAR(16) NOT NULL REFERENCES users (user_id),
    quantity      INTEGER,
    item_price    NUMERIC(10, 2),
    item_total    NUMERIC(12, 2)
);

COMMENT ON TABLE  order_items               IS 'Order line items: which products were in which order. One row per product per order. 43,525 rows. This is the table to use for product-level revenue and units sold.';
COMMENT ON COLUMN order_items.order_item_id IS 'Primary key, format I00000001.';
COMMENT ON COLUMN order_items.order_id      IS 'Parent order. References orders.order_id.';
COMMENT ON COLUMN order_items.product_id    IS 'Product sold on this line. References products.product_id.';
COMMENT ON COLUMN order_items.user_id       IS 'Buyer, denormalised copy of orders.user_id for this order. Either column can be used to reach the customer.';
COMMENT ON COLUMN order_items.quantity      IS 'Units of this product on this line. Values 1, 2 or 3.';
COMMENT ON COLUMN order_items.item_price    IS 'Unit price actually paid.';
COMMENT ON COLUMN order_items.item_total    IS 'Line revenue = quantity * item_price. REVENUE IS DEFINED AS SUM(order_items.item_total).';

-- ------------------------------------------------------------------- reviews
CREATE TABLE reviews (
    review_id   VARCHAR(16) PRIMARY KEY,
    order_id    VARCHAR(16) NOT NULL REFERENCES orders (order_id),
    product_id  VARCHAR(16) NOT NULL REFERENCES products (product_id),
    user_id     VARCHAR(16) NOT NULL REFERENCES users (user_id),
    rating      SMALLINT CHECK (rating BETWEEN 1 AND 5),
    review_text TEXT,
    review_date TIMESTAMP
);

COMMENT ON TABLE  reviews             IS 'Customer product reviews tied to a specific order. 15,000 rows. A given (order_id, product_id) pair can appear more than once, so review_id alone is the key.';
COMMENT ON COLUMN reviews.review_id   IS 'Primary key, format R00000001.';
COMMENT ON COLUMN reviews.order_id    IS 'Order the review relates to. References orders.order_id.';
COMMENT ON COLUMN reviews.product_id  IS 'Reviewed product. References products.product_id.';
COMMENT ON COLUMN reviews.user_id     IS 'Review author. References users.user_id.';
COMMENT ON COLUMN reviews.rating      IS 'Customer rating, integer 1 to 5. Use AVG(reviews.rating) for review-based averages; use products.rating for the catalog rating.';
COMMENT ON COLUMN reviews.review_text IS 'Free-text review body, but only 10 distinct canned sentences exist in this dataset, so it carries no analytical signal. Count reviews rather than analysing text.';
COMMENT ON COLUMN reviews.review_date IS 'Timestamp the review was written.';

-- -------------------------------------------------------------------- events
CREATE TABLE events (
    event_id        VARCHAR(16) PRIMARY KEY,
    user_id         VARCHAR(16) NOT NULL REFERENCES users (user_id),
    product_id      VARCHAR(16) NOT NULL REFERENCES products (product_id),
    event_type      VARCHAR(32),
    event_timestamp TIMESTAMP
);

COMMENT ON TABLE  events                 IS 'Clickstream / behavioural event log. 80,000 rows. IMPORTANT: events has no order_id and is NOT joinable to orders. The browse-to-buy funnel must be answered from this table alone; monetary revenue must be answered from order_items.';
COMMENT ON COLUMN events.event_id        IS 'Primary key, format E00000001.';
COMMENT ON COLUMN events.user_id         IS 'User who triggered the event. References users.user_id.';
COMMENT ON COLUMN events.product_id      IS 'Product the event was about. References products.product_id.';
COMMENT ON COLUMN events.event_type      IS 'One of: view (56,013), cart (12,035), wishlist (7,946), purchase (4,006). A purchase event is a funnel signal only; it does not correspond to a row in orders.';
COMMENT ON COLUMN events.event_timestamp IS 'Timestamp of the event. Range 2024-01-01 to 2025-11-14.';
