"""Additive v5 migration. All snapshots and audit entries are append-only."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS kw_domains(id TEXT PRIMARY KEY, data_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kw_topics(id TEXT PRIMARY KEY, data_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kw_domain_topics(domain_id TEXT NOT NULL REFERENCES kw_domains(id),topic_id TEXT NOT NULL REFERENCES kw_topics(id),PRIMARY KEY(domain_id,topic_id));
CREATE TABLE IF NOT EXISTS kw_objects(id TEXT PRIMARY KEY,kind TEXT NOT NULL CHECK(kind IN ('item','page')),topic_id TEXT NOT NULL REFERENCES kw_topics(id),title TEXT NOT NULL,current_version TEXT,draft_head TEXT,edit_revision INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS kw_versions(id TEXT PRIMARY KEY,object_id TEXT NOT NULL REFERENCES kw_objects(id),base_version TEXT,title TEXT NOT NULL,content_json TEXT NOT NULL,content_digest TEXT NOT NULL,baseline_json TEXT NOT NULL,origin TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kw_reviews(id TEXT PRIMARY KEY,version_id TEXT NOT NULL REFERENCES kw_versions(id),actor TEXT NOT NULL,decision TEXT NOT NULL,reason TEXT NOT NULL,content_digest TEXT NOT NULL,dependency_digest TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kw_publications(id TEXT PRIMARY KEY,object_id TEXT NOT NULL REFERENCES kw_objects(id),version_id TEXT NOT NULL REFERENCES kw_versions(id),previous_version TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kw_segments(id TEXT PRIMARY KEY,record_id TEXT NOT NULL REFERENCES documents(id),data_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kw_dependencies(version_id TEXT NOT NULL REFERENCES kw_versions(id),target_id TEXT NOT NULL,kind TEXT NOT NULL,section_id TEXT NOT NULL DEFAULT '',PRIMARY KEY(version_id,target_id,kind,section_id));
CREATE TABLE IF NOT EXISTS kw_jobs(id TEXT PRIMARY KEY,job_type TEXT NOT NULL,state TEXT NOT NULL,package_json TEXT NOT NULL,input_digest TEXT NOT NULL,attempt_id TEXT NOT NULL,result_json TEXT,error_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kw_attempts(id TEXT PRIMARY KEY,job_id TEXT NOT NULL REFERENCES kw_jobs(id),state TEXT NOT NULL,calls INTEGER NOT NULL DEFAULT 0,owner TEXT,usage_json TEXT NOT NULL DEFAULT 'null',created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kw_submissions(id INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT NOT NULL REFERENCES kw_jobs(id),attempt_id TEXT NOT NULL,submission_key TEXT NOT NULL,result_digest TEXT NOT NULL,accepted INTEGER NOT NULL,result_json TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(attempt_id,submission_key));
CREATE TABLE IF NOT EXISTS kw_impacts(id TEXT PRIMARY KEY,source_id TEXT NOT NULL,version_id TEXT,reason TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',data_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kw_observations(id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,data_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kw_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kw_search(generation TEXT NOT NULL,kind TEXT NOT NULL,object_id TEXT NOT NULL,text TEXT NOT NULL,PRIMARY KEY(generation,kind,object_id));
CREATE INDEX IF NOT EXISTS kw_dep_target ON kw_dependencies(target_id,kind);
CREATE INDEX IF NOT EXISTS kw_job_state ON kw_jobs(state,created_at);
CREATE INDEX IF NOT EXISTS kw_versions_object ON kw_versions(object_id,created_at);
PRAGMA user_version=5;
"""
