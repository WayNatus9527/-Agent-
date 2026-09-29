-- Reference DDL for v0.4.1 (migration 4). Apply runtime upgrades through app.migrations.migrate.

-- Migration and regression tested on isolated PostgreSQL 16.2.

CREATE TABLE admin_audit (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR,
	actor_id VARCHAR,
	action VARCHAR,
	target_id VARCHAR,
	before JSON,
	after JSON,
	created_at VARCHAR,
	PRIMARY KEY (id)
);

CREATE TABLE agent_action_audit (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	actor_id VARCHAR NOT NULL,
	proposal_id VARCHAR NOT NULL,
	warehouse_id VARCHAR NOT NULL,
	event VARCHAR NOT NULL,
	code VARCHAR,
	detail JSON NOT NULL,
	created_at VARCHAR NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE agent_proposals (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	actor_id VARCHAR NOT NULL,
	request_key VARCHAR NOT NULL,
	fingerprint VARCHAR NOT NULL,
	action VARCHAR NOT NULL,
	payload JSON NOT NULL,
	preview JSON NOT NULL,
	auth_version INTEGER NOT NULL,
	stock_version INTEGER NOT NULL,
	token VARCHAR NOT NULL,
	status VARCHAR NOT NULL,
	result JSON,
	created_at VARCHAR,
	expires_at VARCHAR,
	finished_at VARCHAR,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, actor_id, request_key)
);

CREATE TABLE approval_requests (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	warehouse_id VARCHAR,
	sku_id VARCHAR,
	kind VARCHAR,
	original_id VARCHAR,
	delta JSON,
	expected_version INTEGER,
	reason VARCHAR,
	requester_id VARCHAR,
	reviewer_id VARCHAR,
	review_note VARCHAR,
	status VARCHAR,
	document_id VARCHAR,
	created_at VARCHAR,
	reviewed_at VARCHAR,
	PRIMARY KEY (id)
);

CREATE TABLE balances (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	owner_id VARCHAR NOT NULL,
	warehouse_id VARCHAR NOT NULL,
	sku_id VARCHAR NOT NULL,
	g INTEGER NOT NULL,
	q INTEGER NOT NULL,
	d INTEGER NOT NULL,
	r INTEGER NOT NULL,
	t INTEGER NOT NULL,
	h INTEGER NOT NULL,
	b INTEGER NOT NULL,
	offline INTEGER NOT NULL,
	online INTEGER NOT NULL,
	pending INTEGER NOT NULL,
	withdrawing INTEGER NOT NULL,
	version INTEGER NOT NULL,
	updated_at VARCHAR,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, owner_id, warehouse_id, sku_id),
	CHECK (g >= 0),
	CHECK (q >= 0),
	CHECK (d >= 0),
	CHECK (r >= 0),
	CHECK (t >= 0),
	CHECK (h >= 0),
	CHECK (b >= 0),
	CHECK (offline >= 0),
	CHECK (online >= 0),
	CHECK (pending >= 0),
	CHECK (withdrawing >= 0)
);

CREATE TABLE documents (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	kind VARCHAR,
	warehouse_id VARCHAR,
	sku_id VARCHAR,
	quantity INTEGER,
	source VARCHAR,
	note VARCHAR,
	actor_id VARCHAR,
	created_at VARCHAR,
	status VARCHAR,
	PRIMARY KEY (id)
);

CREATE TABLE idempotency (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR,
	actor_id VARCHAR,
	key VARCHAR,
	fingerprint VARCHAR,
	response JSON,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, actor_id, key)
);

CREATE TABLE import_batches (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	actor_id VARCHAR,
	kind VARCHAR,
	digest VARCHAR,
	rows JSON,
	errors JSON,
	status VARCHAR,
	created_at VARCHAR,
	applied_at VARCHAR,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, kind, digest)
);

CREATE TABLE ledger (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	owner_id VARCHAR,
	warehouse_id VARCHAR,
	sku_id VARCHAR,
	document_id VARCHAR,
	kind VARCHAR,
	delta JSON,
	before JSON,
	after JSON,
	actor_id VARCHAR,
	created_at VARCHAR,
	PRIMARY KEY (id),
	UNIQUE (document_id)
);

CREATE TABLE outbox (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR,
	document_id VARCHAR,
	event_type VARCHAR,
	payload JSON,
	status VARCHAR,
	created_at VARCHAR,
	PRIMARY KEY (id),
	UNIQUE (document_id)
);

CREATE TABLE purchase_lines (
	id VARCHAR NOT NULL,
	order_id VARCHAR NOT NULL,
	sku_id VARCHAR,
	ordered INTEGER NOT NULL,
	received INTEGER NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (order_id, sku_id),
	CHECK (ordered > 0),
	CHECK (received >= 0 AND received <= ordered)
);

CREATE TABLE purchase_orders (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	warehouse_id VARCHAR,
	code VARCHAR,
	supplier VARCHAR,
	note VARCHAR,
	status VARCHAR,
	version INTEGER,
	actor_id VARCHAR,
	created_at VARCHAR,
	close_reason VARCHAR,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, code)
);

CREATE TABLE purchase_receipts (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	order_id VARCHAR,
	reference VARCHAR,
	document_ids JSON,
	created_at VARCHAR,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, order_id, reference)
);

CREATE TABLE quality_events (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR,
	warehouse_id VARCHAR,
	sku_id VARCHAR,
	return_id VARCHAR,
	reference VARCHAR,
	good INTEGER,
	bad INTEGER,
	reason VARCHAR,
	document_id VARCHAR,
	actor_id VARCHAR,
	created_at VARCHAR,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, reference)
);

CREATE TABLE return_orders (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	warehouse_id VARCHAR,
	sku_id VARCHAR,
	kind VARCHAR,
	reference VARCHAR,
	original_id VARCHAR,
	quantity INTEGER,
	good INTEGER,
	bad INTEGER,
	status VARCHAR,
	version INTEGER,
	reason VARCHAR,
	actor_id VARCHAR,
	document_id VARCHAR,
	created_at VARCHAR,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, kind, reference),
	CHECK (quantity > 0 AND good >= 0 AND bad >= 0 AND good + bad <= quantity)
);

CREATE TABLE reversal_links (
	original_id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	reversal_id VARCHAR,
	approval_id VARCHAR,
	PRIMARY KEY (original_id),
	UNIQUE (reversal_id)
);

CREATE TABLE schema_migrations (
	version SERIAL NOT NULL,
	name VARCHAR NOT NULL,
	PRIMARY KEY (version)
);

CREATE TABLE sessions (
	token_hash VARCHAR NOT NULL,
	user_id VARCHAR,
	csrf VARCHAR,
	expires INTEGER,
	PRIMARY KEY (token_hash)
);

CREATE TABLE skus (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	code VARCHAR,
	name VARCHAR,
	spec VARCHAR,
	unit VARCHAR,
	barcode VARCHAR,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, code)
);

CREATE TABLE tenants (
	id VARCHAR NOT NULL,
	name VARCHAR NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE transfer_claims (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR,
	transfer_id VARCHAR,
	expected_version INTEGER,
	reason VARCHAR,
	requester_id VARCHAR,
	reviewer_id VARCHAR,
	note VARCHAR,
	status VARCHAR,
	created_at VARCHAR,
	reviewed_at VARCHAR,
	PRIMARY KEY (id)
);

CREATE TABLE transfer_events (
	id VARCHAR NOT NULL,
	transfer_id VARCHAR NOT NULL,
	kind VARCHAR,
	reference VARCHAR,
	items JSON,
	document_ids JSON,
	actor_id VARCHAR,
	created_at VARCHAR,
	PRIMARY KEY (id),
	UNIQUE (transfer_id, kind, reference)
);

CREATE TABLE transfer_lines (
	id VARCHAR NOT NULL,
	transfer_id VARCHAR NOT NULL,
	sku_id VARCHAR,
	quantity INTEGER,
	sent INTEGER,
	received INTEGER,
	lost INTEGER,
	PRIMARY KEY (id),
	UNIQUE (transfer_id, sku_id),
	CHECK (quantity > 0 AND sent >= 0 AND received >= 0 AND lost >= 0 AND sent <= quantity AND received + lost <= sent)
);

CREATE TABLE transfers (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	code VARCHAR,
	warehouse_id VARCHAR,
	destination_id VARCHAR,
	status VARCHAR,
	version INTEGER,
	reason VARCHAR,
	actor_id VARCHAR,
	created_at VARCHAR,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, code)
);

CREATE TABLE user_settings (
	user_id VARCHAR NOT NULL,
	active INTEGER NOT NULL,
	version INTEGER NOT NULL,
	PRIMARY KEY (user_id)
);

CREATE TABLE users (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	username VARCHAR NOT NULL,
	name VARCHAR,
	password_hash VARCHAR,
	role VARCHAR,
	warehouse_ids JSON,
	PRIMARY KEY (id),
	UNIQUE (username)
);

CREATE TABLE warehouses (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	code VARCHAR,
	name VARCHAR,
	country VARCHAR,
	timezone VARCHAR,
	authority VARCHAR,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, code)
);
