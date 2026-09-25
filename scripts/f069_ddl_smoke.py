"""DDL smoke test for f069_task_graph_provenance draft (design doc §3.2/§4.4).

Creates a scratch schema `f069_smoke` with the prerequisite tables in the
documented column shapes, applies the design-doc DDL, then asserts the
constraint semantics:
  - self-edge rejected by CHECK
  - duplicate pair rejected by UNIQUE
  - valid edge insert
  - CASCADE: deleting a task removes its edges
  - SET NULL: deleting the analysis_run keeps the task, nulls run col,
    retains the revision_sha snapshot
  - CASCADE: deleting the project deletes its tasks
  - ready/blocked derivation (direct dependencies only)

All work runs in a transaction that is rolled back, then the schema is
dropped. Pure DDL/DML on a scratch schema — touches no product data.
"""

import psycopg

DB = "postgresql://clawith:clawith@localhost:5432/clawith_t46f8c7cf_f069_smoke"

# Prerequisite tables in the documented shapes (f066/f068 + 001 tasks).
PREREQ = """
CREATE SCHEMA f069_smoke AUTHORIZATION clawith;
SET search_path = f069_smoke;
CREATE TABLE tenants (id UUID PRIMARY KEY);
CREATE TABLE users (id UUID PRIMARY KEY);
CREATE TABLE agents (id UUID PRIMARY KEY);
CREATE TABLE projects (id UUID PRIMARY KEY);
CREATE TABLE analysis_runs (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    revision_sha VARCHAR(64) NOT NULL
);
CREATE TABLE analysis_findings (
    id UUID PRIMARY KEY,
    analysis_run_id UUID NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE
);
CREATE TABLE tasks (
    id UUID PRIMARY KEY,
    tenant_id UUID REFERENCES tenants(id),
    agent_id UUID NOT NULL REFERENCES agents(id),
    created_by UUID NOT NULL REFERENCES users(id),
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    type VARCHAR(20) NOT NULL DEFAULT 'todo'
);
"""

# The f069 draft DDL exactly as in docs/PHASE_2D_TASK_GRAPH_PROVENANCE_DESIGN.md
# §3.2 (task_dependencies) and §4.4 (tasks provenance columns).
F069_DDL = """
CREATE TYPE task_created_reason_enum AS ENUM ('MANUAL','ANALYSIS_FINDING','ANALYSIS_PLANNING');

ALTER TABLE tasks ADD COLUMN project_id UUID,
    ADD COLUMN analysis_run_id UUID,
    ADD COLUMN finding_id UUID,
    ADD COLUMN revision_sha VARCHAR(64),
    ADD COLUMN created_reason task_created_reason_enum NOT NULL DEFAULT 'MANUAL';

CREATE INDEX ix_tasks_project_id ON tasks (project_id) WHERE project_id IS NOT NULL;
CREATE INDEX ix_tasks_analysis_run_id ON tasks (analysis_run_id) WHERE analysis_run_id IS NOT NULL;
CREATE INDEX ix_tasks_finding_id ON tasks (finding_id) WHERE finding_id IS NOT NULL;
CREATE INDEX ix_tasks_revision_sha ON tasks (revision_sha) WHERE revision_sha IS NOT NULL;
CREATE INDEX ix_tasks_created_reason ON tasks (created_reason);

ALTER TABLE tasks ADD CONSTRAINT fk_tasks_project
    FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE CASCADE;
ALTER TABLE tasks ADD CONSTRAINT fk_tasks_analysis_run
    FOREIGN KEY (analysis_run_id) REFERENCES analysis_runs (id) ON DELETE SET NULL;
ALTER TABLE tasks ADD CONSTRAINT fk_tasks_finding
    FOREIGN KEY (finding_id) REFERENCES analysis_findings (id) ON DELETE SET NULL;

CREATE TABLE task_dependencies (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    task_id UUID NOT NULL,
    depends_on_task_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_task_depends_pair UNIQUE (task_id, depends_on_task_id),
    CONSTRAINT ck_task_dep_no_self CHECK (task_id <> depends_on_task_id),
    CONSTRAINT fk_task_dep_tenant FOREIGN KEY (tenant_id) REFERENCES tenants (id),
    CONSTRAINT fk_task_dep_task FOREIGN KEY (task_id) REFERENCES tasks (id) ON DELETE CASCADE,
    CONSTRAINT fk_task_dep_depends FOREIGN KEY (depends_on_task_id) REFERENCES tasks (id) ON DELETE CASCADE
);
CREATE INDEX ix_task_dependencies_task_id ON task_dependencies (task_id);
CREATE INDEX ix_task_dependencies_depends_on_task_id ON task_dependencies (depends_on_task_id);
CREATE INDEX ix_task_dependencies_tenant_id ON task_dependencies (tenant_id);
"""

T1 = "11111111-0000-0000-0000-000000000001"
T2 = "11111111-0000-0000-0000-000000000002"
T3 = "11111111-0000-0000-0000-000000000003"
T4 = "11111111-0000-0000-0000-000000000004"
TEN = "22222222-0000-0000-0000-000000000001"
PROJ = "33333333-0000-0000-0000-000000000001"
AGT = "44444444-0000-0000-0000-000000000001"
USR = "55555555-0000-0000-0000-000000000001"
RUN = "66666666-0000-0000-0000-000000000001"
FIND = "77777777-0000-0000-0000-000000000001"

SEED = f"""
INSERT INTO tenants(id) VALUES ('{TEN}');
INSERT INTO users(id) VALUES ('{USR}');
INSERT INTO agents(id) VALUES ('{AGT}');
INSERT INTO projects(id) VALUES ('{PROJ}');
INSERT INTO analysis_runs(id, project_id, revision_sha) VALUES ('{RUN}', '{PROJ}', 'abc123');
INSERT INTO analysis_findings(id, analysis_run_id) VALUES ('{FIND}', '{RUN}');
INSERT INTO tasks(id, tenant_id, agent_id, created_by, status, type,
                  project_id, analysis_run_id, finding_id, revision_sha, created_reason)
VALUES
 ('{T1}', '{TEN}', '{AGT}', '{USR}', 'pending', 'todo', '{PROJ}', NULL, NULL, NULL, 'MANUAL'),
 ('{T2}', '{TEN}', '{AGT}', '{USR}', 'done',    'todo', '{PROJ}', '{RUN}', '{FIND}', 'abc123', 'ANALYSIS_FINDING'),
 ('{T3}', '{TEN}', '{AGT}', '{USR}', 'pending', 'todo', '{PROJ}', '{RUN}', '{FIND}', 'abc123', 'ANALYSIS_FINDING'),
 ('{T4}', '{TEN}', '{AGT}', '{USR}', 'pending', 'todo', '{PROJ}', NULL, NULL, NULL, 'MANUAL');
"""

# Direct-dependency ready/blocked derivation (design §5.3): ready when no
# direct dependency has status <> 'done'.
def ready_or_blocked(conn, task_id):
    return conn.execute(
        """
        SELECT CASE WHEN COUNT(*) = 0 THEN 'ready' ELSE 'blocked' END
        FROM (
            SELECT t.status
            FROM task_dependencies td
            JOIN tasks t ON t.id = td.depends_on_task_id
            WHERE td.task_id = %(tid)s AND t.status <> 'done'
        ) x
        """,
        {"tid": task_id},
    ).fetchone()[0]


def expect_reject(conn, sql, err_substr, label):
    # Isolate the violating statement in a savepoint so the surrounding
    # transaction stays usable after the rejected DML.
    conn.execute("SAVEPOINT f069_reject")
    try:
        conn.execute(sql)
        conn.execute("ROLLBACK TO SAVEPOINT f069_reject")
        raise AssertionError(f"{label}: expected rejection but DML succeeded")
    except psycopg.errors.Error as e:
        conn.execute("ROLLBACK TO SAVEPOINT f069_reject")
        msg = (e.diag.message_primary or "") + str(e)
        if err_substr not in msg:
            raise AssertionError(f"{label}: wrong error ({msg!r})")
        print(f"  ok - {label}: {type(e).__name__} ({err_substr})")


def main():
    conn = psycopg.connect(DB, autocommit=False)
    try:
        conn.execute("DROP SCHEMA IF EXISTS f069_smoke CASCADE")
        conn.execute(PREREQ)
        conn.execute(F069_DDL)
        conn.execute(SEED)
        print("DDL + seed applied")

        # Seed one valid edge: T3 depends on T2 (T2 is done => T3 ready).
        conn.execute(
            f"INSERT INTO task_dependencies(id, tenant_id, task_id, depends_on_task_id)"
            f" VALUES ('{T4}', '{TEN}', '{T3}', '{T2}')"
        )
        print("  ok - valid edge inserted (T3 -> T2)")

        # T3's only dependency is T2 (done) => ready. T1 has no deps => ready.
        assert ready_or_blocked(conn, T3) == "ready", "T3 should be ready (dep done)"
        assert ready_or_blocked(conn, T1) == "ready", "T1 should be ready (no deps)"
        print("  ok - ready/blocked derivation: no-dep => ready; all-deps-done => ready")

        # Add a second edge T4 -> T3 (T3 pending) => T4 now blocked.
        conn.execute(
            f"INSERT INTO task_dependencies(id, tenant_id, task_id, depends_on_task_id)"
            f" VALUES (gen_random_uuid(), '{TEN}', '{T4}', '{T3}')"
        )
        assert ready_or_blocked(conn, T4) == "blocked", "T4 should be blocked (dep T3 not done)"
        print("  ok - blocked derivation: any non-done direct dep => blocked")

        # Schema floors: self-edge + duplicate pair.
        expect_reject(
            conn,
            f"INSERT INTO task_dependencies(id, tenant_id, task_id, depends_on_task_id)"
            f" VALUES (gen_random_uuid(), '{TEN}', '{T1}', '{T1}')",
            "ck_task_dep_no_self",
            "self-edge",
        )
        expect_reject(
            conn,
            f"INSERT INTO task_dependencies(id, tenant_id, task_id, depends_on_task_id)"
            f" VALUES (gen_random_uuid(), '{TEN}', '{T3}', '{T2}')",
            "uq_task_depends_pair",
            "duplicate edge",
        )

        # CASCADE: deleting a task removes every edge that references it.
        # T3 owns edge (T3->T2) and is referenced by (T4->T3) => both die.
        before = conn.execute("SELECT COUNT(*) FROM task_dependencies").fetchone()[0]
        conn.execute(f"DELETE FROM tasks WHERE id = '{T3}'")
        after = conn.execute("SELECT COUNT(*) FROM task_dependencies").fetchone()[0]
        assert before == 2 and after == 0, (before, after)
        print("  ok - CASCADE: edges removed with their referencing task")

        # SET NULL: deleting the analysis_run keeps T2, nulls analysis_run_id,
        # retains the revision_sha snapshot.
        conn.execute(f"DELETE FROM analysis_runs WHERE id = '{RUN}'")
        row = conn.execute(
            f"SELECT analysis_run_id, revision_sha, created_reason FROM tasks WHERE id = '{T2}'"
        ).fetchone()
        assert row[0] is None, row
        assert row[1] == "abc123", row
        assert row[2] == "ANALYSIS_FINDING", row
        print("  ok - SET NULL: run deleted, task kept, revision_sha snapshot retained")

        # CASCADE: deleting the project deletes its tasks.
        conn.execute(f"DELETE FROM projects WHERE id = '{PROJ}'")
        n = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        assert n == 0, n
        print("  ok - CASCADE: project deletion removed its tasks")

        print("ALL DDL SMOKE CHECKS PASSED")
    finally:
        conn.rollback()
        with psycopg.connect(DB, autocommit=True) as c2:
            c2.execute("DROP SCHEMA IF EXISTS f069_smoke CASCADE")
        conn.close()
        print("ROLLBACK + schema cleanup done")


main()
