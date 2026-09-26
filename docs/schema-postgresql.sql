-- Generated schema draft; PostgreSQL runtime not yet tested.

CREATE TABLE tenants (
	id VARCHAR NOT NULL,
	name VARCHAR NOT NULL,
	PRIMARY KEY (id)
)

;

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
)

;

CREATE TABLE sessions (
	token_hash VARCHAR NOT NULL,
	user_id VARCHAR,
	csrf VARCHAR,
	expires INTEGER,
	PRIMARY KEY (token_hash)
)

;

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
)

;

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
)

;

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
)

;

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
)

;

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
)

;

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
)

;

CREATE TABLE idempotency (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR,
	actor_id VARCHAR,
	key VARCHAR,
	fingerprint VARCHAR,
	response JSON,
	PRIMARY KEY (id),
	UNIQUE (tenant_id, actor_id, key)
)

;
