-- Screening runs. Every candidate of every step is stored with its tested value(s) and outcome,
-- so "why was X eliminated?" is a simple lookup.

CREATE TABLE screen_run (
    run_id          INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,             -- profile name
    created_at      TEXT NOT NULL,
    asof_date       TEXT,                      -- item_metrics as-of date used
    profile_path    TEXT,
    profile_yaml    TEXT NOT NULL,             -- profile text exactly as run
    parent_run_id   INTEGER REFERENCES screen_run(run_id) ON DELETE SET NULL,
    rerun_from_step INTEGER,                   -- steps before this were copied from parent_run_id
    n_start         INTEGER,                   -- candidates entering step 1
    n_survivors     INTEGER
);

CREATE TABLE screen_step (
    run_id          INTEGER NOT NULL REFERENCES screen_run(run_id) ON DELETE CASCADE,
    step_no         INTEGER NOT NULL,          -- 1-based
    definition_json TEXT NOT NULL,             -- normalized step definition (used to validate re-runs)
    description     TEXT NOT NULL,             -- human readable, e.g. "perf_3m between [-0.05, 0.05]"
    n_in            INTEGER NOT NULL,
    n_passed        INTEGER NOT NULL,
    PRIMARY KEY (run_id, step_no)
) WITHOUT ROWID;

CREATE TABLE screen_step_item (
    run_id     INTEGER NOT NULL,
    step_no    INTEGER NOT NULL,
    item_id    INTEGER NOT NULL REFERENCES item(item_id) ON DELETE CASCADE,
    value_json TEXT,                           -- tested value(s), e.g. {"perf_1y": -0.41, "perf_3y": null}
    passed     INTEGER NOT NULL,               -- 1 / 0
    reason     TEXT,                           -- NULL when passed; "missing data: ..." or what failed
    PRIMARY KEY (run_id, step_no, item_id),
    FOREIGN KEY (run_id, step_no) REFERENCES screen_step(run_id, step_no) ON DELETE CASCADE
) WITHOUT ROWID;

CREATE INDEX screen_step_item_item ON screen_step_item (run_id, item_id);
