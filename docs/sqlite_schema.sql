-- Streamlit MVP 数据结构草案，2026-11-12 增加历史成功数据、初审路由结果与专家复审字段。
-- 用于初始化空数据库。该脚本不升级已有数据库结构，已有库由 db.migrate 补列。
-- 应用每次打开连接后均须设置 foreign_keys，并检查 json_valid 可用。
PRAGMA foreign_keys = ON;

BEGIN;

CREATE TABLE import_batches (
    id INTEGER PRIMARY KEY,
    original_filename TEXT NOT NULL,
    file_sha256 TEXT NOT NULL CHECK (length(file_sha256) = 64),
    stored_file_path TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    import_profile_hash TEXT NOT NULL CHECK (length(import_profile_hash) = 64),
    sheet_metadata_json TEXT NOT NULL
        CHECK (json_valid(sheet_metadata_json) AND json_type(sheet_metadata_json) = 'object'),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (file_sha256, parser_version, import_profile_hash)
);

CREATE TABLE source_rows (
    id INTEGER PRIMARY KEY,
    batch_id INTEGER NOT NULL,
    record_type TEXT NOT NULL CHECK (record_type IN ('declaration', 'packing')),
    sheet_name TEXT NOT NULL,
    excel_row INTEGER NOT NULL CHECK (excel_row > 0),
    contract_no TEXT,
    item_ref TEXT,
    product_name TEXT,
    specification TEXT,
    material_code TEXT,
    tax_category_code TEXT,
    supplier_hs_code TEXT,
    declaration_text TEXT,
    raw_cells_json TEXT NOT NULL
        CHECK (json_valid(raw_cells_json) AND json_type(raw_cells_json) = 'object'),
    FOREIGN KEY (batch_id) REFERENCES import_batches(id),
    UNIQUE (batch_id, sheet_name, excel_row),
    UNIQUE (id, batch_id)
);

CREATE INDEX source_rows_match_idx
    ON source_rows(batch_id, record_type, contract_no, item_ref);

CREATE TABLE row_links (
    id INTEGER PRIMARY KEY,
    batch_id INTEGER NOT NULL,
    declaration_row_id INTEGER NOT NULL,
    packing_row_id INTEGER NOT NULL,
    match_method TEXT NOT NULL CHECK (match_method IN ('exact', 'range', 'manual')),
    status TEXT NOT NULL
        CHECK (status IN ('matched', 'needs_confirmation', 'rejected')),
    note TEXT,
    updated_by TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK (declaration_row_id <> packing_row_id),
    FOREIGN KEY (declaration_row_id, batch_id) REFERENCES source_rows(id, batch_id),
    FOREIGN KEY (packing_row_id, batch_id) REFERENCES source_rows(id, batch_id),
    UNIQUE (declaration_row_id, packing_row_id)
);

CREATE INDEX row_links_packing_idx ON row_links(packing_row_id);

CREATE UNIQUE INDEX row_links_one_group_per_packing_idx
    ON row_links(packing_row_id) WHERE status = 'matched';

CREATE TABLE audit_runs (
    id INTEGER PRIMARY KEY,
    batch_id INTEGER NOT NULL,
    declaration_row_id INTEGER NOT NULL,
    input_hash TEXT NOT NULL CHECK (length(input_hash) = 64),
    input_snapshot_json TEXT NOT NULL
        CHECK (json_valid(input_snapshot_json) AND json_type(input_snapshot_json) = 'object'),
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    output_schema_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    raw_response_text TEXT,
    attributes_json TEXT CHECK (
        attributes_json IS NULL OR
        (json_valid(attributes_json) AND json_type(attributes_json) = 'array')
    ),
    issues_json TEXT CHECK (
        issues_json IS NULL OR
        (json_valid(issues_json) AND json_type(issues_json) = 'array')
    ),
    error_type TEXT,
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    finished_at TEXT,
    CHECK (
        (status = 'running' AND finished_at IS NULL AND
            attributes_json IS NULL AND issues_json IS NULL AND error_type IS NULL)
        OR
        (status = 'succeeded' AND finished_at IS NOT NULL AND
            attributes_json IS NOT NULL AND issues_json IS NOT NULL AND error_type IS NULL)
        OR
        (status = 'failed' AND finished_at IS NOT NULL AND
            attributes_json IS NULL AND issues_json IS NULL AND error_type IS NOT NULL)
    ),
    FOREIGN KEY (declaration_row_id, batch_id) REFERENCES source_rows(id, batch_id)
);

CREATE INDEX audit_runs_current_idx
    ON audit_runs(declaration_row_id, input_hash, model, prompt_version, output_schema_version, status, id);

-- 历史成功申报数据，初审命中后作为参考，不替代归类依据。
CREATE TABLE history_success (
    id INTEGER PRIMARY KEY,
    product_name TEXT NOT NULL,
    brand TEXT,
    model TEXT,
    specification TEXT,
    hs_code TEXT NOT NULL,
    declaration_elements TEXT,
    key_attributes_json TEXT CHECK (
        key_attributes_json IS NULL OR
        (json_valid(key_attributes_json) AND json_type(key_attributes_json) = 'object')
    ),
    source_type TEXT NOT NULL DEFAULT 'enterprise_history'
        CHECK (source_type IN ('enterprise_history', 'verified_case', 'seed_example')),
    source_ref TEXT,
    tariff_version TEXT,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (product_name, brand, model)
);

CREATE INDEX history_success_name_idx ON history_success(product_name);

-- 初审路由结果：每个申报行在当前批次的一条当前结论。
-- route 记录分流来源，skill 名称为后续 planner agent 预留。
CREATE TABLE review_results (
    id INTEGER PRIMARY KEY,
    batch_id INTEGER NOT NULL,
    declaration_row_id INTEGER NOT NULL,
    route TEXT NOT NULL CHECK (route IN ('history_hit', 'skill')),
    skill_name TEXT,
    history_id INTEGER,
    audit_run_id INTEGER,
    status TEXT NOT NULL CHECK (status IN ('succeeded', 'failed')),
    summary TEXT,
    pre_hs_code TEXT,
    expert_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (expert_status IN ('pending', 'approved', 'returned')),
    expert_hs_code TEXT,
    expert_note TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (batch_id, declaration_row_id),
    FOREIGN KEY (declaration_row_id, batch_id) REFERENCES source_rows(id, batch_id),
    FOREIGN KEY (history_id) REFERENCES history_success(id),
    FOREIGN KEY (audit_run_id) REFERENCES audit_runs(id)
);

COMMIT;
